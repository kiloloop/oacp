#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""oacp_watch.py — Emit inbox delta events for Monitor-friendly polling.

Usage:
    oacp_watch.py --agent <name> --project <project> [--project <project> ...] [--json]
    oacp_watch.py --agent <name> --all-projects [--json]

Exit codes:
    0 — scan completed successfully
    1 — validation or scan failure
    2 — command-line usage error (argparse)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


STATE_VERSION = 1

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_STATE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _parse_since(spec: str, *, now: float) -> float:
    """Parse a --since spec to an epoch timestamp.

    Accepts: 'now', 'epoch', duration like '30s' / '5m' / '2h' / '7d',
    or an ISO 8601 timestamp (trailing 'Z' allowed; naive timestamps treated as UTC).
    """
    if spec == "now":
        return now
    if spec == "epoch":
        return 0.0
    if (
        len(spec) >= 2
        and spec[-1] in _DURATION_UNITS
        and spec[:-1].isdigit()
    ):
        return now - int(spec[:-1]) * _DURATION_UNITS[spec[-1]]
    try:
        normalized = spec[:-1] + "+00:00" if spec.endswith("Z") else spec
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since: cannot parse {spec!r} "
            "(use 'now', 'epoch', a duration like '30s'/'5m'/'2h'/'7d', or an ISO 8601 timestamp)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass(frozen=True)
class WatchTarget:
    project: str
    agent: str
    inbox_dir: Path
    state_file: Path
    state_id: Optional[str] = None


def _resolve_oacp_home(explicit: Optional[str] = None) -> Path:
    from _oacp_env import resolve_oacp_home

    return resolve_oacp_home(explicit)


def _parse_yaml_mapping_bytes(raw: bytes, path: Path) -> Dict[str, Any]:
    text = raw.decode("utf-8")
    if yaml is not None:
        loaded = yaml.load(text, Loader=yaml.BaseLoader)
    else:
        from validate_message import _parse_simple_yaml

        loaded = _parse_simple_yaml(text)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"top-level YAML must be a mapping: {path}")
    return loaded


def _message_metadata(
    project: str,
    agent: str,
    path: Path,
    verification: Dict[str, Any],
) -> Dict[str, str]:
    """Event metadata for one inbox message, verify-before-parse.

    The watcher surfaces from/type/subject/priority into events consumed
    by agent runtimes, so it honors the same receive contract as the
    lister: under ``enforce`` an unverified message becomes a HELD event
    built from the filename only — none of its (attacker-controlled)
    fields are parsed or surfaced.
    """
    from message_verify import read_verified_inbox_message

    read = read_verified_inbox_message(
        path, verification, _parse_yaml_mapping_bytes
    )
    if read["raw"] is None or (read["data"] is None and not read["held"]):
        raise ValueError(read["error"] or f"unreadable message: {path}")
    metadata = {
        "event": "new_message",
        "project": project,
        "agent": agent,
        "file": path.name,
    }
    if read["held"]:
        metadata.update(
            {
                "from": "?",
                "type": "?",
                "subject": "(held: unverified under enforce)",
                "priority": "?",
                "auth": read["auth"]["status"],
            }
        )
        return metadata
    data = read["data"]
    metadata.update(
        {
            "from": str(data.get("from", "")).strip() or "?",
            "type": str(data.get("type", "")).strip() or "?",
            "subject": str(data.get("subject", "")).strip() or "(no subject)",
            "priority": str(data.get("priority", "")).strip() or "?",
        }
    )
    if read["auth"] is not None:
        metadata["auth"] = read["auth"]["status"]
    return metadata


def _error_event(
    project: Optional[str],
    agent: str,
    *,
    error: str,
    file: Optional[str] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "event": "error",
        "project": project,
        "agent": agent,
        "error": error,
    }
    if file is not None:
        payload["file"] = file
    if path is not None:
        payload["path"] = str(path)
    return payload


def _emit_event(event: Dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(event, sort_keys=True))
    else:
        subject = event.get("subject", "")
        print(
            " ".join(
                str(part)
                for part in (
                    event["event"].upper(),
                    f"project={event.get('project')}",
                    f"agent={event.get('agent')}",
                    f"file={event.get('file', '-')}",
                    f"type={event.get('type', '-')}",
                    f"priority={event.get('priority', '-')}",
                    subject,
                )
                if part
            )
        )


def _emit_error(event: Dict[str, Any], *, json_output: bool) -> None:
    location = event.get("path") or event.get("file") or "(unknown)"
    print(f"ERROR: {location}: {event['error']}", file=sys.stderr)
    if json_output:
        print(json.dumps(event, sort_keys=True))


def _discover_all_projects(oacp_root: Path, agent: str) -> List[str]:
    projects_dir = oacp_root / "projects"
    if not projects_dir.is_dir():
        return []
    return sorted(
        project_dir.name
        for project_dir in projects_dir.iterdir()
        if (
            project_dir.is_dir()
            and (project_dir / "agents" / agent / "inbox").is_dir()
        )
    )


def _dedupe_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _watch_state_file(project_dir: Path, agent: str, state_id: Optional[str]) -> Path:
    state_name = f"{agent}.json" if state_id is None else f"{agent}.{state_id}.json"
    return project_dir / "state" / "watch" / state_name


def _resolve_targets(
    *,
    projects: Optional[List[str]],
    all_projects: bool,
    agent: str,
    oacp_root: Path,
    state_id: Optional[str] = None,
) -> tuple[List[WatchTarget], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    if all_projects:
        project_names = _discover_all_projects(oacp_root, agent)
        if not project_names:
            errors.append(
                _error_event(
                    None,
                    agent,
                    error=(
                        f"no projects found with inbox path "
                        f"{oacp_root / 'projects' / '*' / 'agents' / agent / 'inbox'}"
                    ),
                )
            )
            return [], errors
    else:
        project_names = _dedupe_keep_order(projects or [])

    targets: List[WatchTarget] = []
    for project in project_names:
        project_dir = oacp_root / "projects" / project
        if not project_dir.is_dir():
            errors.append(
                _error_event(
                    project,
                    agent,
                    error=f"project '{project}' not found under {oacp_root / 'projects'}",
                    path=project_dir,
                )
            )
            continue
        agent_dir = project_dir / "agents" / agent
        if not agent_dir.is_dir():
            errors.append(
                _error_event(
                    project,
                    agent,
                    error=f"agent '{agent}' not found in project '{project}'",
                    path=agent_dir,
                )
            )
            continue
        inbox_dir = agent_dir / "inbox"
        if not inbox_dir.is_dir():
            errors.append(
                _error_event(
                    project,
                    agent,
                    error=f"inbox directory not found: {inbox_dir}",
                    path=inbox_dir,
                )
            )
            continue
        state_file = _watch_state_file(project_dir, agent, state_id)
        targets.append(
            WatchTarget(
                project=project,
                agent=agent,
                inbox_dir=inbox_dir,
                state_file=state_file,
                state_id=state_id,
            )
        )
    return targets, errors


def _load_state(state_file: Path) -> tuple[Dict[str, Any], bool]:
    """Return (state, existed). `existed` is False for first-run targets."""
    if not state_file.is_file():
        return {"version": STATE_VERSION, "messages": {}}, False
    data = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("state file root must be a JSON object")
    messages = data.get("messages", {})
    if not isinstance(messages, dict):
        raise ValueError("state file 'messages' must be a JSON object")
    normalized: Dict[str, Dict[str, str]] = {}
    for file_name, metadata in messages.items():
        if isinstance(metadata, dict):
            normalized[str(file_name)] = {
                key: str(value) for key, value in metadata.items()
            }
    return (
        {"version": int(data.get("version", STATE_VERSION)), "messages": normalized},
        True,
    )


def _write_state(state_file: Path, payload: Dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_file.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(state_file)


def _scan_target(
    target: WatchTarget,
    oacp_dir: Optional[str] = None,
) -> tuple[Dict[str, Dict[str, str]], Dict[str, float], List[Dict[str, Any]]]:
    from message_verify import receiver_intake_context

    errors: List[Dict[str, Any]] = []
    current_messages: Dict[str, Dict[str, str]] = {}
    mtimes: Dict[str, float] = {}
    # Per-scan context: config edits (mode flips, re-signs) apply on the
    # next cycle, and an unauthorized config fails the scan closed to
    # enforce (held events) rather than parsing anything.
    verification = receiver_intake_context(
        target.inbox_dir.parent,
        receiver=target.agent,
        project=target.project,
        oacp_dir=oacp_dir,
    )
    for path in sorted(
        candidate
        for candidate in target.inbox_dir.iterdir()
        if candidate.is_file() and candidate.suffix == ".yaml"
    ):
        try:
            metadata = _message_metadata(
                target.project, target.agent, path, verification
            )
            mtime = path.stat().st_mtime
        except Exception as exc:
            errors.append(
                _error_event(
                    target.project,
                    target.agent,
                    error=str(exc),
                    file=path.name,
                    path=path,
                )
            )
            continue
        current_messages[path.name] = {
            "file": metadata["file"],
            "from": metadata["from"],
            "type": metadata["type"],
            "subject": metadata["subject"],
            "priority": metadata["priority"],
        }
        if "auth" in metadata:
            current_messages[path.name]["auth"] = metadata["auth"]
        mtimes[path.name] = mtime
    return current_messages, mtimes, errors


def _build_delta_events(
    target: WatchTarget,
    previous_messages: Dict[str, Dict[str, str]],
    current_messages: Dict[str, Dict[str, str]],
    *,
    show_archived: bool,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for file_name in sorted(current_messages.keys() - previous_messages.keys()):
        event = {
            "event": "new_message",
            "project": target.project,
            "agent": target.agent,
        }
        event.update(current_messages[file_name])
        events.append(event)
    if show_archived:
        for file_name in sorted(previous_messages.keys() - current_messages.keys()):
            event = {
                "event": "message_archived",
                "project": target.project,
                "agent": target.agent,
            }
            event.update(previous_messages[file_name])
            events.append(event)
    return events


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Emit inbox delta events for a single agent. Designed for Claude Monitor "
            "or shell loops that re-run the command."
        ),
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--project",
        action="append",
        dest="projects",
        help="Project to scan (repeatable)",
    )
    selector.add_argument(
        "--all-projects",
        action="store_true",
        help="Auto-discover all projects that contain this agent inbox",
    )
    parser.add_argument("--agent", required=True, help="Agent inbox name to watch")
    parser.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON Lines")
    parser.add_argument(
        "--state-id",
        default=None,
        help=(
            "Use a per-subscriber cursor file state/watch/<agent>.<state-id>.json. "
            "Omit for the legacy shared state/watch/<agent>.json cursor."
        ),
    )
    parser.add_argument(
        "--since",
        default="now",
        help=(
            "Cutoff for first-run baseline: 'now' (default) suppresses replay of "
            "existing inbox messages; 'epoch' replays everything; '30s'/'5m'/'2h'/'7d' "
            "for relative windows; ISO 8601 ('2026-04-26T00:00:00Z') for absolute. "
            "Only applies on first run for a target (no state file yet)."
        ),
    )
    parser.add_argument(
        "--show-archived",
        action="store_true",
        help=(
            "Emit 'message_archived' events when messages disappear from the inbox. "
            "Off by default — when the watching agent is also the inbox owner, "
            "these events are self-loops (the agent already knows it deleted them)."
        ),
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.state_id is not None and not _STATE_ID_RE.fullmatch(args.state_id):
        parser.error("--state-id must match [A-Za-z0-9._-]{1,64}")
    try:
        since_epoch = _parse_since(args.since, now=time.time())
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    oacp_root = _resolve_oacp_home(args.oacp_dir)

    targets, target_errors = _resolve_targets(
        projects=args.projects,
        all_projects=args.all_projects,
        agent=args.agent,
        oacp_root=oacp_root,
        state_id=args.state_id,
    )
    had_errors = False
    if target_errors:
        had_errors = True
        for event in target_errors:
            _emit_error(event, json_output=args.json_output)
    if not targets:
        return 1 if had_errors else 0

    events: List[Dict[str, Any]] = []

    for target in targets:
        try:
            previous_state, state_existed = _load_state(target.state_file)
        except Exception as exc:
            had_errors = True
            _emit_error(
                _error_event(
                    target.project,
                    target.agent,
                    error=f"invalid state file: {exc}",
                    path=target.state_file,
                ),
                json_output=args.json_output,
            )
            continue
        current_messages, mtimes, scan_errors = _scan_target(target, args.oacp_dir)
        if scan_errors:
            had_errors = True
            for event in scan_errors:
                _emit_error(event, json_output=args.json_output)
        previous_messages = previous_state["messages"]
        if not state_existed:
            # First run for this target: seed baseline with messages whose mtime <= since.
            # NEW_MESSAGE events fire only for files newer than the cutoff.
            previous_messages = {
                file_name: current_messages[file_name]
                for file_name in current_messages
                if mtimes.get(file_name, 0.0) <= since_epoch
            }
        target_events = _build_delta_events(
            target,
            previous_messages,
            current_messages,
            show_archived=args.show_archived,
        )
        payload = {
            "version": STATE_VERSION,
            "project": target.project,
            "agent": target.agent,
            "messages": current_messages,
        }
        if target.state_id is not None:
            payload["state_id"] = target.state_id
        try:
            _write_state(target.state_file, payload)
        except Exception as exc:
            had_errors = True
            _emit_error(
                _error_event(
                    target.project,
                    target.agent,
                    error=f"failed to write state file: {exc}",
                    path=target.state_file,
                ),
                json_output=args.json_output,
            )
            continue
        events.extend(target_events)

    for event in events:
        _emit_event(event, json_output=args.json_output)
    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
