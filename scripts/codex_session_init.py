#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Verify Codex startup inputs and render bounded SessionStart context."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # type: ignore[import-untyped]

from _oacp_constants import CODEX_SESSION_START_CONTEXT_LIMIT, utc_now_iso
from _oacp_env import resolve_oacp_home


PROTOCOL_FILES = (
    "agent_safety_defaults.md",
    "dispatch_states.yaml",
    "session_init.md",
)

MEMORY_FILES = (
    "project_facts.md",
    "decision_log.md",
    "open_threads.md",
    "known_debt.md",
)

HOOK_CONTEXT_CHAR_LIMIT = CODEX_SESSION_START_CONTEXT_LIMIT

DEFAULT_CAPABILITIES = [
    "headless",
    "mcp_tools",
    "shell_access",
    "git_ops",
    "github_cli",
    "web_search",
    "session_memory",
    "notifications",
    "async_tasks",
]


def utc_now() -> str:
    return utc_now_iso(datetime.now(timezone.utc).replace(microsecond=0))


def _project_name_from_json(path: Path) -> Optional[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    project = str(payload.get("project_name", "")).strip()
    return project or None


def _project_name_from_workspace_marker(cwd: Path, name: str) -> Optional[str]:
    path = cwd / name
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_file() or path.is_symlink():
        return _project_name_from_json(path)
    return None


def _detect_project_name(cwd: Path) -> Optional[str]:
    for resolver in (
        lambda: _project_name_from_workspace_marker(cwd, "workspace.json"),
        lambda: _project_name_from_workspace_marker(cwd, ".oacp"),
    ):
        project = resolver()
        if project:
            return project
    return None


def _candidate_protocol_roots(cwd: Path) -> List[Path]:
    roots: List[Path] = []
    seen = set()
    for root in [*Path(__file__).resolve().parents, cwd, *cwd.parents]:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _resolve_protocol_dir(cwd: Path) -> Path:
    for root in _candidate_protocol_roots(cwd):
        candidate = root / "docs" / "protocol"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / "_protocol"


def _verify_file_status(path: Path) -> Dict[str, Any]:
    item: Dict[str, Any] = {"path": str(path), "state": "missing", "bytes": 0}
    if not path.exists():
        return item

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        item["state"] = "error"
        item["error"] = str(exc)
        return item

    # Reading here verifies that the file is available to the runtime. The
    # contents are deliberately not returned or described as model context.
    item["state"] = "verified"
    item["bytes"] = len(raw.encode("utf-8"))
    return item


def _parse_capabilities_from_status(raw: str) -> Optional[List[str]]:
    """Return configured capabilities without depending on YAML indentation."""
    payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise ValueError("status.yaml must contain a top-level mapping")
    if "capabilities" not in payload:
        return None

    raw_capabilities = payload["capabilities"]
    if not isinstance(raw_capabilities, list):
        raise ValueError("status.yaml capabilities must be a list")

    capabilities: List[str] = []
    for capability in raw_capabilities:
        if (
            not isinstance(capability, str)
            or not capability
            or not all(
                char.isascii() and (char.isalnum() or char == "_")
                for char in capability
            )
        ):
            raise ValueError(
                "status.yaml capabilities must use non-empty alphanumeric or underscore keys"
            )
        capabilities.append(capability)
    return capabilities


def _render_status_yaml(
    *,
    model: str,
    status: str,
    current_task: str,
    capabilities: List[str],
) -> str:
    data = {
        "runtime": "codex",
        "model": model,
        "status": status,
        "current_task": current_task,
        "capabilities": list(capabilities),
        "updated_at": utc_now(),
    }
    return yaml.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def _upsert_status_yaml(
    *,
    status_path: Path,
    model: str,
    status: str,
    current_task: str,
    dry_run: bool,
) -> Dict[str, Any]:
    existing_caps: Optional[List[str]] = None
    existed = status_path.exists()

    if existed:
        try:
            existing_raw = status_path.read_text(encoding="utf-8")
            existing_caps = _parse_capabilities_from_status(existing_raw)
        except Exception as exc:
            # An unreadable or invalid existing file proves nothing about what
            # was configured. Defaults may substitute only for an absent
            # capabilities key — never for a present value that failed
            # validation — so refuse the rewrite and keep the file byte-exact.
            return {
                "path": str(status_path),
                "state": "error",
                "used_existing_capabilities": False,
                "warning": (
                    f"existing status.yaml is invalid; left unchanged: {exc}"
                ),
            }

    capabilities = (
        existing_caps if existing_caps is not None else list(DEFAULT_CAPABILITIES)
    )
    rendered = _render_status_yaml(
        model=model,
        status=status,
        current_task=current_task,
        capabilities=capabilities,
    )

    result: Dict[str, Any] = {
        "path": str(status_path),
        "state": "dry-run" if dry_run else ("updated" if existed else "created"),
        "capabilities_count": len(capabilities),
        "used_existing_capabilities": existing_caps is not None,
    }

    if not dry_run:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(rendered, encoding="utf-8")

    return result


def _ack_state(value: str) -> str:
    mapping = {
        "verified": "ok",
        "missing": "missing",
        "error": "error",
        "skipped": "skipped",
        "created": "created",
        "updated": "updated",
        "dry-run": "dry-run",
        "project-missing": "project-missing",
        "no-project": "no-project",
    }
    return mapping.get(value, value)


def run_session_init(
    *,
    project: Optional[str],
    hub_dir: Path,
    cwd: Path,
    model: str,
    status: str,
    current_task: str,
    dry_run: bool,
    protocol_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    resolved_project = (project or "").strip() or _detect_project_name(cwd)
    resolved_protocol_dir = protocol_dir or _resolve_protocol_dir(cwd)

    protocol_results: Dict[str, Dict[str, Any]] = {}
    for name in PROTOCOL_FILES:
        protocol_results[name] = _verify_file_status(resolved_protocol_dir / name)

    memory_results: Dict[str, Dict[str, Any]] = {}
    status_result: Dict[str, Any]
    warnings: List[str] = []

    if resolved_project:
        project_dir = hub_dir / "projects" / resolved_project
        memory_dir = project_dir / "memory"
        for name in MEMORY_FILES:
            memory_results[name] = _verify_file_status(memory_dir / name)

        if project_dir.exists():
            status_result = _upsert_status_yaml(
                status_path=project_dir / "agents" / "codex" / "status.yaml",
                model=model,
                status=status,
                current_task=current_task,
                dry_run=dry_run,
            )
        else:
            status_result = {
                "path": str(project_dir / "agents" / "codex" / "status.yaml"),
                "state": "project-missing",
            }
            warnings.append(f"project directory not found: {project_dir}")
    else:
        for name in MEMORY_FILES:
            memory_results[name] = {"path": "", "state": "skipped", "bytes": 0}
        status_result = {"path": "", "state": "no-project"}
        warnings.append(
            "no project detected from workspace.json or .oacp, and --project not provided"
        )

    for name in PROTOCOL_FILES:
        state = protocol_results[name]["state"]
        if state in {"missing", "error"}:
            warnings.append(f"protocol file {name}: {state}")

    for name in MEMORY_FILES:
        state = memory_results[name]["state"]
        if state in {"missing", "error"}:
            warnings.append(f"memory file {name}: {state}")

    if "warning" in status_result:
        warnings.append(str(status_result["warning"]))

    protocol_ack = ",".join(
        f"{name}:{_ack_state(protocol_results[name]['state'])}" for name in PROTOCOL_FILES
    )
    memory_ack = ",".join(
        f"{name}:{_ack_state(memory_results[name]['state'])}" for name in MEMORY_FILES
    )
    status_ack = _ack_state(str(status_result.get("state", "unknown")))
    project_ack = resolved_project or "none"
    ack = (
        f"project={project_ack};"
        f"protocol={protocol_ack};"
        f"memory={memory_ack};"
        f"status_yaml={status_ack};"
        "context=manifest"
    )

    return {
        "project": resolved_project or "",
        "hub_dir": str(hub_dir),
        "protocol": protocol_results,
        "memory": memory_results,
        "status_yaml": status_result,
        "warnings": warnings,
        "ack": ack,
    }


def _pull_memory_report(*, hub_dir: Path, dry_run: bool) -> Dict[str, Any]:
    """Run the advisory startup pull and retain a compact result for hook context."""
    marker = hub_dir / ".oacp-memory-repo"
    if not marker.is_file():
        return {
            "state": "disabled",
            "messages": ["OACP memory sync is not initialized; startup pull skipped."],
        }
    if dry_run:
        return {
            "state": "dry-run",
            "messages": ["OACP memory pull skipped in dry-run mode."],
        }

    try:
        from memory_sync import MemorySyncError, pull_memory

        messages = pull_memory(hub_dir)
    except (MemorySyncError, OSError, ValueError) as exc:
        return {"state": "failed", "messages": [str(exc)]}

    state = "warning" if any(line.startswith("WARNING:") for line in messages) else "ok"
    return {"state": state, "messages": messages}


def _bounded_hook_context(text: str) -> str:
    if len(text) <= HOOK_CONTEXT_CHAR_LIMIT:
        return text
    suffix = (
        "\n[OACP startup context truncated; run `oacp session-init --json` "
        "for the complete verification report.]"
    )
    return text[: HOOK_CONTEXT_CHAR_LIMIT - len(suffix)] + suffix


def build_session_start_hook_output(
    report: Dict[str, Any],
    *,
    memory_sync: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Render concise developer context for the Codex ``SessionStart`` hook."""
    protocol_paths = report["protocol"]
    memory_paths = report["memory"]
    protocol_root = str(Path(protocol_paths[PROTOCOL_FILES[0]]["path"]).parent)
    readable_memory_paths = [
        str(item["path"])
        for item in memory_paths.values()
        if str(item.get("path", "")).strip()
    ]
    memory_root = (
        str(Path(readable_memory_paths[0]).parent) if readable_memory_paths else "(none)"
    )
    sync_state = str((memory_sync or {}).get("state", "not-requested"))
    verified_items = [
        item
        for item in [*protocol_paths.values(), *memory_paths.values()]
        if item.get("state") == "verified"
    ]
    verified_bytes = sum(int(item.get("bytes", 0)) for item in verified_items)

    lines = [
        "OACP SessionStart verification completed.",
        f"SESSION_INIT_ACK: {report['ack']}",
        f"Memory sync: {sync_state}.",
        f"Verified inputs: {len(verified_items)} files / {verified_bytes} bytes.",
        "The hook verified readability; it did not inject the full file contents.",
        "Before normal task execution, read these files in this exact order:",
        f"Protocol root: {protocol_root}",
        *[f"- {name}" for name in PROTOCOL_FILES],
        f"Project memory root: {memory_root}",
        *[f"- {name}" for name in MEMORY_FILES],
        "Then read org-memory/recent.md when present, plus relevant decisions.md "
        "and rules.md, as required by the active AGENTS.md.",
        "Include the exact SESSION_INIT_ACK line above in the first response after startup.",
    ]
    if report["warnings"]:
        lines.append("Startup is degraded; report these verification warnings:")
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if sync_state in {"failed", "warning"}:
        lines.append(
            "Memory pull did not complete cleanly; treat local memory as potentially stale "
            "and surface the failure before relying on it."
        )

    output: Dict[str, Any] = {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _bounded_hook_context("\n".join(lines)),
        },
    }
    if report["warnings"] or sync_state in {"failed", "warning"}:
        output["systemMessage"] = "OACP SessionStart completed in degraded mode."
    return output


def _read_hook_input() -> Dict[str, Any]:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid SessionStart hook JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("SessionStart hook input must be a JSON object")
    return payload


def _hook_input_error_output(message: str) -> Dict[str, Any]:
    return {
        "continue": True,
        "systemMessage": "OACP SessionStart could not parse its hook input.",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                f"OACP startup verification did not run: {message}. "
                "Before substantial work, run `oacp session-init --pull-memory` "
                "manually and report the resulting SESSION_INIT_ACK."
            ),
        },
    }


def _hook_runtime_error_output(message: str) -> Dict[str, Any]:
    return {
        "continue": True,
        "systemMessage": "OACP SessionStart completed in degraded mode.",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _bounded_hook_context(
                f"OACP startup verification failed after hook input was accepted: "
                f"{message}. Before substantial work, run "
                "`oacp session-init --pull-memory` manually and report the "
                "resulting SESSION_INIT_ACK."
            ),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Codex startup inputs and update status.yaml.",
    )
    parser.add_argument("--project", help="Project name under <oacp>/projects/")
    parser.add_argument(
        "--hub-dir",
        default=None,
        help="OACP home root path (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CODEX_MODEL") or "unknown",
        help="Model identifier written to status.yaml",
    )
    parser.add_argument(
        "--status",
        choices=("available", "busy", "offline"),
        default="available",
        help="Runtime availability status (default: available)",
    )
    parser.add_argument(
        "--current-task",
        default="",
        help="Optional current task string to write into status.yaml",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json", dest="json_output", action="store_true", help="JSON output"
    )
    output.add_argument(
        "--hook",
        action="store_true",
        help="Read Codex SessionStart JSON from stdin and emit hook JSON",
    )
    parser.add_argument(
        "--pull-memory",
        action="store_true",
        help="Run advisory OACP memory pull before startup verification",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute init report without writing status.yaml",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    hook_input: Dict[str, Any] = {}
    if args.hook:
        try:
            hook_input = _read_hook_input()
        except ValueError as exc:
            print(json.dumps(_hook_input_error_output(str(exc))))
            return 0
    try:
        hub_dir = resolve_oacp_home(args.hub_dir).resolve()
        cwd = Path(str(hook_input.get("cwd") or Path.cwd())).expanduser().resolve()
        hook_model = str(hook_input.get("model") or "").strip()
        model = hook_model or str(args.model).strip() or "unknown"
        model_warning = ""
        if args.hook and not hook_model:
            model = "unknown"
            model_warning = (
                "SessionStart hook input omitted the active model; "
                "status.yaml records model: unknown"
            )

        memory_sync = (
            _pull_memory_report(hub_dir=hub_dir, dry_run=args.dry_run)
            if args.pull_memory
            else None
        )

        report = run_session_init(
            project=args.project,
            hub_dir=hub_dir,
            cwd=cwd,
            model=model,
            status=args.status,
            current_task=args.current_task,
            dry_run=args.dry_run,
        )
        if model_warning:
            report["warnings"].append(model_warning)
    except Exception as exc:
        if not args.hook:
            raise
        detail = f"{type(exc).__name__}: {exc}"
        print(json.dumps(_hook_runtime_error_output(detail)))
        return 0

    if args.hook:
        print(json.dumps(build_session_start_hook_output(report, memory_sync=memory_sync)))
        return 0

    if args.json_output:
        if memory_sync is not None:
            report["memory_sync"] = memory_sync
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print("=== CODEX SESSION INIT ===")
    print(f"Project: {report['project'] or '(none)'}")
    print("Protocol files:")
    for name in PROTOCOL_FILES:
        item = report["protocol"][name]
        print(f"- {name}: {item['state']}")
    print("Memory files:")
    for name in MEMORY_FILES:
        item = report["memory"][name]
        print(f"- {name}: {item['state']}")
    print(f"status.yaml: {report['status_yaml'].get('state', 'unknown')}")
    if memory_sync is not None:
        print(f"memory pull: {memory_sync['state']}")
        for message in memory_sync["messages"]:
            print(f"- {message}")
    for warning in report["warnings"]:
        print(f"WARN: {warning}")
    print(f"SESSION_INIT_ACK: {report['ack']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
