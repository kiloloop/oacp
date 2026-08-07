#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""oacp_inbox.py — List pending inbox messages for one or all agents.

Usage:
    oacp_inbox.py <project> --agent <name>
    oacp_inbox.py <project> --all
    oacp_inbox.py <project> --agent <name> --json

Verify-before-parse: inbox messages are untrusted input, and this lister
is a receiver read path — so it honors the receiver's
``signing.verify_mode`` before parsing anything. Under ``enforce``, a
message that is not signed-verified is listed as HELD from its filename
metadata only: none of its (attacker-controlled) fields are parsed or
surfaced. Under ``warn`` the verification status is attached to each
row; under ``off`` behavior is unchanged. Listing is read-only — the
quarantine/reject mechanism stays with the processing path (the
autonomy gate), which is where a held message must be dispositioned.

Exit codes:
    0 — inbox listed successfully
    1 — project/agent lookup failure
    2 — command-line usage error (argparse)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from _oacp_constants import is_agent_dir


def _resolve_oacp_home(explicit: Optional[str] = None) -> Path:
    from _oacp_env import resolve_oacp_home

    return resolve_oacp_home(explicit)


def _coerce_oacp_home(explicit: Optional[Union[str, Path]]) -> Path:
    return _resolve_oacp_home(str(explicit)) if explicit is not None else _resolve_oacp_home()


def _parse_yaml_mapping_bytes(raw: bytes, path: Path) -> Dict[str, Any]:
    """Parse a mapping from an already-read snapshot (never re-reads *path*)."""
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


def _parse_created_at(value: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return None


def _format_age(created_at: dt.datetime, now: Optional[dt.datetime] = None) -> str:
    current = now or dt.datetime.now(dt.timezone.utc)
    delta = current - created_at
    if delta.total_seconds() < 0:
        return "0m"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _mtime_stub(
    path: Path,
    subject: str,
    now: Optional[dt.datetime] = None,
    **extra: str,
) -> Dict[str, str]:
    """Preview row built from filesystem metadata only (nothing parsed)."""
    created_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    stub = {
        "from": "?",
        "type": "?",
        "priority": "?",
        "subject": subject,
        "created_at_utc": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age": _format_age(created_at, now=now),
        "path": str(path),
    }
    stub.update(extra)
    return stub


def _message_preview(
    path: Path,
    now: Optional[dt.datetime] = None,
    *,
    verification: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Preview one message, honoring the receiver's verify mode.

    *verification* is the receiver-level context from
    `message_verify.receiver_intake_context`. Verification and parsing
    consume ONE bounded read of the file; under ``enforce`` a
    non-verified message is never parsed at all.
    """
    from message_verify import read_verified_inbox_message

    read = read_verified_inbox_message(
        path, verification or {"mode": "off"}, _parse_yaml_mapping_bytes
    )
    auth_status: Optional[str] = (
        read["auth"]["status"] if read["auth"] is not None else None
    )
    if read["raw"] is None:
        return _mtime_stub(
            path, "(unreadable)", now=now, load_error=read["error"]
        )
    if read["held"]:
        return _mtime_stub(
            path,
            "(held: unverified under enforce)",
            now=now,
            auth=auth_status,
        )
    if read["data"] is None:
        stub = _mtime_stub(
            path, "(invalid YAML)", now=now, load_error=read["error"]
        )
        if auth_status is not None:
            stub["auth"] = auth_status
        return stub
    data = read["data"]

    created_at_raw = str(data.get("created_at_utc", "")).strip()
    created_at = _parse_created_at(created_at_raw)
    if created_at is None:
        created_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        created_at_raw = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    preview = {
        "from": str(data.get("from", "")).strip() or "?",
        "type": str(data.get("type", "")).strip() or "?",
        "priority": str(data.get("priority", "")).strip() or "?",
        "subject": str(data.get("subject", "")).strip() or "(no subject)",
        "created_at_utc": created_at_raw,
        "age": _format_age(created_at, now=now),
        "path": str(path),
    }
    if auth_status is not None:
        preview["auth"] = auth_status
    return preview


def _list_inbox_files(inbox_dir: Path) -> List[Path]:
    if not inbox_dir.is_dir():
        return []
    return sorted(path for path in inbox_dir.iterdir() if path.is_file() and path.suffix == ".yaml")


def _agent_report(
    project_dir: Path,
    agent: str,
    now: Optional[dt.datetime] = None,
    *,
    oacp_root: Path,
) -> Dict[str, Any]:
    from message_verify import receiver_intake_context

    inbox_dir = project_dir / "agents" / agent / "inbox"
    verification = receiver_intake_context(
        project_dir / "agents" / agent,
        receiver=agent,
        project=project_dir.name,
        oacp_dir=str(oacp_root),
    )
    messages = [
        _message_preview(path, now=now, verification=verification)
        for path in _list_inbox_files(inbox_dir)
    ]
    report = {
        "agent": agent,
        "inbox_path": str(inbox_dir),
        "verify_mode": verification["mode"],
        "message_count": len(messages),
        "messages": messages,
    }
    if verification.get("policy_error"):
        report["policy_error"] = verification["policy_error"]
    return report


def list_inbox(
    project: str,
    *,
    agent: Optional[str] = None,
    list_all: bool = False,
    oacp_dir: Optional[Union[str, Path]] = None,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Return inbox metadata for one agent or all project agents."""
    if agent is None and not list_all:
        raise ValueError("either --agent or --all is required")

    oacp_root = _coerce_oacp_home(oacp_dir)
    project_dir = oacp_root / "projects" / project
    if not project_dir.is_dir():
        raise ValueError(f"project '{project}' not found under {oacp_root / 'projects'}")

    agents_dir = project_dir / "agents"
    if not agents_dir.is_dir():
        raise ValueError(f"project '{project}' has no agents directory")

    if list_all:
        agent_names = sorted(
            path.name for path in agents_dir.iterdir() if is_agent_dir(path)
        )
    else:
        if agent is None:
            raise ValueError("agent name is required when --all is not set")
        if not (agents_dir / agent).is_dir():
            raise ValueError(f"agent '{agent}' not found in project '{project}'")
        agent_names = [agent]

    reports = [
        _agent_report(project_dir, agent_name, now=now, oacp_root=oacp_root)
        for agent_name in agent_names
    ]
    return {
        "project": project,
        "mode": "all" if list_all else "agent",
        "agents": reports,
    }


def _render_table(messages: List[Dict[str, str]]) -> str:
    headers = ["#", "From", "Type", "Priority", "Subject", "Age"]
    rows = [
        [
            str(index),
            message["from"],
            message["type"],
            message["priority"],
            message["subject"],
            message["age"],
        ]
        for index, message in enumerate(messages, start=1)
    ]
    widths = [
        max(len(header), *(len(row[col]) for row in rows))
        for col, header in enumerate(headers)
    ]
    lines = [
        "| " + " | ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)) + " |",
        "|-" + "-|-".join("-" * width for width in widths) + "-|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)) + " |")
    return "\n".join(lines)


def render_report(report: Dict[str, Any]) -> str:
    """Render a human-readable inbox listing."""
    sections: List[str] = []
    project = str(report["project"])
    for agent_report in report["agents"]:
        count = int(agent_report["message_count"])
        header = f"INBOX: {agent_report['agent']} ({project}) — {count} message"
        if count != 1:
            header += "s"
        if count == 0:
            body = "No pending messages."
        else:
            body = _render_table(agent_report["messages"])
        sections.append(f"{header}\n\n{body}")
    return "\n\n".join(sections) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="List pending OACP inbox messages for one or all agents.",
    )
    parser.add_argument("project", help="Project workspace name")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--agent", help="List one agent inbox")
    scope.add_argument("--all", action="store_true", dest="list_all", help="List all agent inboxes")
    parser.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON output")

    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        report = list_inbox(
            args.project,
            agent=args.agent,
            list_all=args.list_all,
            oacp_dir=args.oacp_dir if args.oacp_dir else None,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(report, indent=2))
    else:
        print(render_report(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
