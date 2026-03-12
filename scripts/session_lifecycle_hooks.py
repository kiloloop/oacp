#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Persist init/close session hooks into OACP workspace state."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from _oacp_env import resolve_oacp_home
from validate_message import AGENT_RE


PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PACKET_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
SESSION_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")

VALID_RUNTIMES = {"gemini", "claude", "codex", "human", "unknown"}
VALID_ROLES = {"orchestrator", "qa", "reviewer", "implementer", "deploy", "human"}
VALID_CLOSE_STATUS = {"completed", "blocked", "aborted"}
VALID_PACKET_STATES = {
    "submitted",
    "in_review",
    "findings_returned",
    "fixing",
    "merge_decision",
    "merged",
    "escalated",
}

STATE_VERSION = "0.1.0"


class SessionLifecycleError(Exception):
    """Raised for invalid CLI input or state transitions."""


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_compact() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")


def parse_utc_timestamp(raw: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except Exception:
        return None


def ensure_project_name(project: str) -> str:
    if not PROJECT_RE.fullmatch(project):
        raise SessionLifecycleError(f"invalid project name: {project!r}")
    return project


def ensure_agent_name(agent: str) -> str:
    value = (agent or "").strip()
    if not AGENT_RE.fullmatch(value):
        raise SessionLifecycleError(f"invalid agent name: {agent!r}")
    return value


def ensure_packet_id(packet_id: Optional[str]) -> Optional[str]:
    if packet_id is None:
        return None
    value = packet_id.strip()
    if not value:
        return None
    if not PACKET_ID_RE.fullmatch(value):
        raise SessionLifecycleError(f"invalid packet_id: {packet_id!r}")
    return value


def ensure_packet_state(packet_state: Optional[str]) -> Optional[str]:
    if packet_state is None:
        return None
    value = packet_state.strip()
    if not value:
        return None
    if value not in VALID_PACKET_STATES:
        raise SessionLifecycleError(
            f"packet_state must be one of {sorted(VALID_PACKET_STATES)}, got: {packet_state!r}"
        )
    return value


def generate_session_id(agent: str) -> str:
    return f"sess-{now_compact()}-{agent}-{secrets.token_hex(3)}"


def default_packet_state_for_init(role: str, runtime: str) -> str:
    if role in {"orchestrator", "qa", "reviewer", "deploy"} or runtime == "gemini":
        return "in_review"
    if role == "implementer" or runtime in {"claude", "codex"}:
        return "fixing"
    return "in_review"


def default_packet_state_for_close(
    status: str,
    role: str,
    runtime: str,
) -> Optional[str]:
    if status == "blocked":
        return "escalated"
    if status == "aborted":
        return None
    if role in {"orchestrator", "qa", "reviewer", "deploy"} or runtime == "gemini":
        return "findings_returned"
    if role == "implementer" or runtime in {"claude", "codex"}:
        return "merge_decision"
    return "merge_decision"


def default_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "updated_at_utc": "",
        "active_sessions": {},
        "packets": {},
    }


def load_state(state_file: Path) -> Dict[str, Any]:
    if not state_file.is_file():
        return default_state()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SessionLifecycleError(
            f"malformed JSON state file {state_file}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise SessionLifecycleError(f"state file root must be an object: {state_file}")
    out = default_state()
    out.update(data)
    if not isinstance(out.get("active_sessions"), dict):
        out["active_sessions"] = {}
    if not isinstance(out.get("packets"), dict):
        out["packets"] = {}
    return out


def write_state(state_file: Path, data: Dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    temp_name = f".{state_file.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}"
    temp_path = state_file.parent / temp_name
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, state_file)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def append_event(events_file: Path, event: Dict[str, Any]) -> None:
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


@contextmanager
def state_lock(lock_file: Path, *, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise SessionLifecycleError(
                f"failed to acquire state lock {lock_file}: {exc}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def resolve_state_paths(hub_dir: Path, project: str) -> Tuple[Path, Path, Path, Path]:
    project_dir = hub_dir / "projects" / project
    if not project_dir.is_dir():
        raise SessionLifecycleError(f"project directory not found: {project_dir}")
    state_dir = project_dir / "state"
    return (
        project_dir,
        state_dir / "session_lifecycle_state.json",
        state_dir / "session_lifecycle_events.jsonl",
        state_dir / "session_lifecycle_state.lock",
    )


def apply_packet_open(
    state: Dict[str, Any],
    packet_id: str,
    *,
    session_id: str,
    session_started_at_utc: str,
    agent: str,
    runtime: str,
    role: str,
    packet_state: str,
) -> None:
    packets = state.setdefault("packets", {})
    packet = packets.get(packet_id, {})
    session_count = int(packet.get("session_count", 0))
    packet.update(
        {
            "state": packet_state,
            "session_open": True,
            "active_session_id": session_id,
            "last_session_id": session_id,
            "last_session_event": "init_session",
            "last_session_agent": agent,
            "last_session_runtime": runtime,
            "last_session_role": role,
            "last_session_started_at_utc": session_started_at_utc,
            "updated_at_utc": session_started_at_utc,
            "session_count": session_count + 1,
        }
    )
    packets[packet_id] = packet


def apply_packet_close(
    state: Dict[str, Any],
    packet_id: str,
    *,
    session_id: str,
    closed_at_utc: str,
    duration_sec: Optional[int],
    agent: str,
    runtime: str,
    role: str,
    packet_state: Optional[str],
    close_status: str,
) -> Optional[str]:
    packets = state.setdefault("packets", {})
    packet = packets.get(packet_id, {})
    previous_state = packet.get("state")
    resolved_state = packet_state if packet_state is not None else previous_state
    packet.update(
        {
            "state": resolved_state,
            "session_open": False,
            "active_session_id": "",
            "last_session_id": session_id,
            "last_session_event": "close_session",
            "last_session_agent": agent,
            "last_session_runtime": runtime,
            "last_session_role": role,
            "last_session_closed_at_utc": closed_at_utc,
            "last_close_status": close_status,
            "updated_at_utc": closed_at_utc,
        }
    )
    if duration_sec is not None:
        packet["last_session_duration_sec"] = duration_sec
    packets[packet_id] = packet
    return resolved_state


def init_session(
    *,
    project: str,
    hub_dir: Path,
    agent: str,
    runtime: str,
    role: str,
    packet_id: Optional[str],
    packet_state: Optional[str],
    conversation_id: Optional[str],
    branch: Optional[str],
    notes: Optional[str],
    session_id: Optional[str],
    dry_run: bool,
) -> Dict[str, Any]:
    ensure_project_name(project)
    agent = ensure_agent_name(agent)
    packet_id = ensure_packet_id(packet_id)
    packet_state = ensure_packet_state(packet_state)

    if runtime not in VALID_RUNTIMES:
        raise SessionLifecycleError(f"runtime must be one of {sorted(VALID_RUNTIMES)}")
    if role not in VALID_ROLES:
        raise SessionLifecycleError(f"role must be one of {sorted(VALID_ROLES)}")

    project_dir, state_file, events_file, lock_file = resolve_state_paths(hub_dir, project)

    with state_lock(lock_file, enabled=not dry_run):
        state = load_state(state_file)

        sid = (session_id or generate_session_id(agent)).strip()
        if session_id and not SESSION_ID_SAFE_RE.fullmatch(sid):
            raise SessionLifecycleError(f"invalid session_id: {session_id!r}")
        if sid in state["active_sessions"]:
            raise SessionLifecycleError(f"session_id already active: {sid}")

        started_at = now_utc()
        resolved_packet_state = packet_state
        if packet_id and resolved_packet_state is None:
            resolved_packet_state = default_packet_state_for_init(role, runtime)

        active_record = {
            "agent": agent,
            "runtime": runtime,
            "role": role,
            "packet_id": packet_id or "",
            "conversation_id": (conversation_id or "").strip(),
            "branch": (branch or "").strip(),
            "notes": (notes or "").strip(),
            "started_at_utc": started_at,
            "last_updated_at_utc": started_at,
        }
        state["active_sessions"][sid] = active_record

        if packet_id and resolved_packet_state:
            apply_packet_open(
                state,
                packet_id,
                session_id=sid,
                session_started_at_utc=started_at,
                agent=agent,
                runtime=runtime,
                role=role,
                packet_state=resolved_packet_state,
            )

        state["updated_at_utc"] = started_at

        event = {
            "timestamp_utc": started_at,
            "event": "init_session",
            "project": project,
            "agent": agent,
            "runtime": runtime,
            "role": role,
            "session_id": sid,
            "packet_id": packet_id or "",
            "packet_state": resolved_packet_state or "",
            "conversation_id": (conversation_id or "").strip(),
            "branch": (branch or "").strip(),
            "notes": (notes or "").strip(),
        }

        if not dry_run:
            write_state(state_file, state)
            append_event(events_file, event)

    return {
        "project": project,
        "project_dir": str(project_dir),
        "event": "init_session",
        "session_id": sid,
        "packet_id": packet_id or "",
        "packet_state": resolved_packet_state or "",
        "state_file": str(state_file),
        "events_file": str(events_file),
        "dry_run": dry_run,
    }


def _pick_session_to_close(
    active_sessions: Dict[str, Any],
    agent: str,
    session_id: Optional[str],
) -> str:
    if session_id:
        if session_id not in active_sessions:
            raise SessionLifecycleError(f"session_id is not active: {session_id}")
        record = active_sessions[session_id]
        owner = str(record.get("agent", ""))
        if owner != agent:
            raise SessionLifecycleError(
                f"session {session_id} belongs to agent {owner!r}, "
                f"not {agent!r}; use the owning agent name or omit --session-id"
            )
        return session_id

    owned = [sid for sid, rec in active_sessions.items() if rec.get("agent") == agent]
    if not owned:
        raise SessionLifecycleError(
            f"no active sessions for agent {agent!r}; provide --session-id"
        )
    if len(owned) > 1:
        raise SessionLifecycleError(
            f"multiple active sessions for agent {agent!r}; provide --session-id explicitly"
        )
    return owned[0]


def close_session(
    *,
    project: str,
    hub_dir: Path,
    agent: str,
    packet_id: Optional[str],
    packet_state: Optional[str],
    close_status: str,
    session_id: Optional[str],
    notes: Optional[str],
    dry_run: bool,
) -> Dict[str, Any]:
    ensure_project_name(project)
    agent = ensure_agent_name(agent)
    packet_id = ensure_packet_id(packet_id)
    packet_state = ensure_packet_state(packet_state)
    if close_status not in VALID_CLOSE_STATUS:
        raise SessionLifecycleError(f"status must be one of {sorted(VALID_CLOSE_STATUS)}")

    project_dir, state_file, events_file, lock_file = resolve_state_paths(hub_dir, project)

    with state_lock(lock_file, enabled=not dry_run):
        state = load_state(state_file)
        active_sessions = state.get("active_sessions", {})
        if not isinstance(active_sessions, dict):
            raise SessionLifecycleError("state.active_sessions must be an object")

        sid = _pick_session_to_close(active_sessions, agent, session_id)
        active_record = active_sessions[sid]

        runtime = str(active_record.get("runtime", "unknown")) or "unknown"
        role = str(active_record.get("role", "orchestrator")) or "orchestrator"
        resolved_packet_id = packet_id or ensure_packet_id(active_record.get("packet_id"))
        started_at_utc = str(active_record.get("started_at_utc", ""))
        closed_at_utc = now_utc()

        started_dt = parse_utc_timestamp(started_at_utc)
        closed_dt = parse_utc_timestamp(closed_at_utc)
        duration_sec: Optional[int] = None
        if started_dt and closed_dt:
            duration_sec = max(0, int((closed_dt - started_dt).total_seconds()))

        if resolved_packet_id and packet_state is None:
            packet_state = default_packet_state_for_close(close_status, role, runtime)

        del active_sessions[sid]

        resolved_packet_state = ""
        if resolved_packet_id:
            pkt_state = apply_packet_close(
                state,
                resolved_packet_id,
                session_id=sid,
                closed_at_utc=closed_at_utc,
                duration_sec=duration_sec,
                agent=agent,
                runtime=runtime,
                role=role,
                packet_state=packet_state,
                close_status=close_status,
            )
            resolved_packet_state = pkt_state or ""

        state["updated_at_utc"] = closed_at_utc
        event = {
            "timestamp_utc": closed_at_utc,
            "event": "close_session",
            "project": project,
            "agent": agent,
            "runtime": runtime,
            "role": role,
            "session_id": sid,
            "packet_id": resolved_packet_id or "",
            "packet_state": resolved_packet_state,
            "status": close_status,
            "duration_sec": duration_sec if duration_sec is not None else "",
            "notes": (notes or "").strip(),
        }

        if not dry_run:
            write_state(state_file, state)
            append_event(events_file, event)

    return {
        "project": project,
        "project_dir": str(project_dir),
        "event": "close_session",
        "session_id": sid,
        "packet_id": resolved_packet_id or "",
        "packet_state": resolved_packet_state,
        "status": close_status,
        "duration_sec": duration_sec,
        "state_file": str(state_file),
        "events_file": str(events_file),
        "dry_run": dry_run,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist init/close session lifecycle hooks into OACP workspace state.",
    )
    parser.add_argument(
        "--hub-dir",
        default=None,
        help="OACP home root path (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output machine-readable JSON",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write state/event files")

    sub = parser.add_subparsers(dest="command", required=True)

    initp = sub.add_parser("init_session", help="Open a session boundary")
    initp.add_argument("project", help="Project name under <oacp>/projects/")
    initp.add_argument("--agent", required=True, help="Agent identity (e.g., gemini)")
    initp.add_argument(
        "--runtime",
        default="unknown",
        choices=sorted(VALID_RUNTIMES),
        help="Runtime identifier (default: unknown)",
    )
    initp.add_argument(
        "--role",
        default="orchestrator",
        choices=sorted(VALID_ROLES),
        help="Agent role for this session (default: orchestrator)",
    )
    initp.add_argument("--session-id", help="Optional explicit session ID")
    initp.add_argument("--packet-id", help="Associated packet ID")
    initp.add_argument(
        "--packet-state",
        choices=sorted(VALID_PACKET_STATES),
        help="Explicit packet state override",
    )
    initp.add_argument("--conversation-id", help="Optional conversation/thread ID")
    initp.add_argument("--branch", help="Optional git branch context")
    initp.add_argument("--notes", help="Optional session notes")

    closep = sub.add_parser("close_session", help="Close a session boundary")
    closep.add_argument("project", help="Project name under <oacp>/projects/")
    closep.add_argument("--agent", required=True, help="Agent identity (e.g., gemini)")
    closep.add_argument(
        "--session-id",
        help="Session ID to close (auto if one active session exists)",
    )
    closep.add_argument("--packet-id", help="Packet ID override (defaults to session packet)")
    closep.add_argument(
        "--packet-state",
        choices=sorted(VALID_PACKET_STATES),
        help="Explicit packet state override",
    )
    closep.add_argument(
        "--status",
        dest="close_status",
        default="completed",
        choices=sorted(VALID_CLOSE_STATUS),
        help="Close outcome (default: completed)",
    )
    closep.add_argument("--notes", help="Optional close notes")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    hub_dir = resolve_oacp_home(args.hub_dir).resolve()

    try:
        if args.command == "init_session":
            report = init_session(
                project=args.project,
                hub_dir=hub_dir,
                agent=args.agent,
                runtime=args.runtime,
                role=args.role,
                packet_id=args.packet_id,
                packet_state=args.packet_state,
                conversation_id=args.conversation_id,
                branch=args.branch,
                notes=args.notes,
                session_id=args.session_id,
                dry_run=args.dry_run,
            )
        else:
            report = close_session(
                project=args.project,
                hub_dir=hub_dir,
                agent=args.agent,
                packet_id=args.packet_id,
                packet_state=args.packet_state,
                close_status=args.close_status,
                session_id=args.session_id,
                notes=args.notes,
                dry_run=args.dry_run,
            )
    except SessionLifecycleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        packet_text = report.get("packet_id") or "-"
        state_text = report.get("packet_state") or "-"
        print(
            f"{report['event']} ok: session={report['session_id']} "
            f"packet={packet_text} state={state_text}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
