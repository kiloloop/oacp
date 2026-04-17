#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Codex session startup protocol loader for OACP workspaces."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # type: ignore[import-untyped]

from _oacp_constants import utc_now_iso
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


def _project_name_from_agent_hub(cwd: Path) -> Optional[str]:
    path = cwd / ".agent-hub"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        try:
            target = path.resolve()
        except OSError:
            target = None
        if target is not None and target.is_dir():
            return target.name or None
        if target is not None and target.is_file():
            project = _project_name_from_json(target)
            if project:
                return project
    if path.is_file():
        return _project_name_from_json(path)
    return None


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
        lambda: _project_name_from_agent_hub(cwd),
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
    return Path(__file__).resolve().parent.parent / "docs" / "protocol"


def _load_file_status(path: Path) -> Dict[str, Any]:
    item: Dict[str, Any] = {"path": str(path), "state": "missing", "bytes": 0}
    if not path.exists():
        return item

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        item["state"] = "error"
        item["error"] = str(exc)
        return item

    item["state"] = "loaded"
    item["bytes"] = len(raw.encode("utf-8"))
    return item


def _parse_capabilities_from_status(raw: str) -> List[str]:
    caps: List[str] = []
    in_caps = False

    for line in raw.splitlines():
        if not in_caps:
            if re.match(r"^capabilities:\s*$", line):
                in_caps = True
            continue

        if line and not line.startswith((" ", "\t")):
            break

        match = re.match(r"^\s*-\s*([A-Za-z0-9_]+)\s*(?:#.*)?$", line)
        if match:
            caps.append(match.group(1))

    return caps


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
    existing_caps: List[str] = []
    existed = status_path.exists()
    read_error: Optional[str] = None

    if existed:
        try:
            existing_raw = status_path.read_text(encoding="utf-8")
            existing_caps = _parse_capabilities_from_status(existing_raw)
        except Exception as exc:
            read_error = str(exc)

    capabilities = existing_caps or DEFAULT_CAPABILITIES
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
        "used_existing_capabilities": bool(existing_caps),
    }
    if read_error:
        result["warning"] = f"failed to read existing status.yaml: {read_error}"

    if not dry_run:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(rendered, encoding="utf-8")

    return result


def _ack_state(value: str) -> str:
    mapping = {
        "loaded": "ok",
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
        protocol_results[name] = _load_file_status(resolved_protocol_dir / name)

    memory_results: Dict[str, Dict[str, Any]] = {}
    status_result: Dict[str, Any]
    warnings: List[str] = []

    if resolved_project:
        project_dir = hub_dir / "projects" / resolved_project
        memory_dir = project_dir / "memory"
        for name in MEMORY_FILES:
            memory_results[name] = _load_file_status(memory_dir / name)

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
        warnings.append("no project detected from workspace.json, .oacp, .agent-hub, and --project not provided")

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
        f"status_yaml={status_ack}"
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load Codex startup protocol context and update status.yaml.",
    )
    parser.add_argument("--project", help="Project name under <oacp>/projects/")
    parser.add_argument(
        "--hub-dir",
        default=None,
        help="OACP home root path (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CODEX_MODEL", "codex"),
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
    parser.add_argument("--json", dest="json_output", action="store_true", help="JSON output")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute init report without writing status.yaml",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    hub_dir = resolve_oacp_home(args.hub_dir).resolve()
    cwd = Path.cwd()

    report = run_session_init(
        project=args.project,
        hub_dir=hub_dir,
        cwd=cwd,
        model=args.model,
        status=args.status,
        current_task=args.current_task,
        dry_run=args.dry_run,
    )

    if args.json_output:
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
    for warning in report["warnings"]:
        print(f"WARN: {warning}")
    print(f"SESSION_INIT_ACK: {report['ack']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
