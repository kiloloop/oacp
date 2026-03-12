#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""oacp_coordinator.py - MCP state coordinator for OACP projects.

Exposes three MCP tools:
- claim_packet(project, packet_id, agent, lease_sec?, force?)
- update_findings(project, packet_id, agent, findings, require_claim?, force?)
- get_agent_state(project, agent)

State is persisted under:
  projects/<project>/state/oacp_coordinator_state.json
  projects/<project>/state/oacp_coordinator_events.jsonl
  projects/<project>/state/oacp_coordinator_state.lock

This module supports:
1) MCP stdio server mode (default): JSON-RPC with Content-Length framing
2) CLI helper mode: direct command execution for local debugging

Exit codes:
  0 - success
  2 - invalid input / coordinator error
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import random
import re
import string
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


STATE_VERSION = "0.1.0"
SERVER_NAME = "oacp-coordinator"
SERVER_VERSION = "0.1.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

STATE_FILE_NAME = "oacp_coordinator_state.json"
EVENTS_FILE_NAME = "oacp_coordinator_events.jsonl"
LOCK_FILE_NAME = "oacp_coordinator_state.lock"
SESSION_STATE_FILE_NAME = "session_lifecycle_state.json"

PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PACKET_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
AGENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DEFAULT_LEASE_SEC = 1800
MIN_LEASE_SEC = 1
MAX_LEASE_SEC = 86400


class CoordinatorError(Exception):
    """Raised for validation, protocol, or state errors."""


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(raw: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def add_seconds_utc(ts_utc: str, seconds: int) -> str:
    parsed = parse_utc(ts_utc)
    if parsed is None:
        parsed = dt.datetime.now(dt.timezone.utc)
    return (parsed + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def random_token(n: int = 12) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def ensure_project(project: str) -> str:
    value = (project or "").strip()
    if not PROJECT_RE.fullmatch(value):
        raise CoordinatorError(f"invalid project name: {project!r}")
    return value


def ensure_agent(agent: str) -> str:
    value = (agent or "").strip()
    if not AGENT_RE.fullmatch(value):
        raise CoordinatorError(f"invalid agent name: {agent!r}")
    return value


def ensure_packet(packet_id: str) -> str:
    value = (packet_id or "").strip()
    if not PACKET_ID_RE.fullmatch(value):
        raise CoordinatorError(f"invalid packet_id: {packet_id!r}")
    return value


def ensure_lease_sec(lease_sec: int) -> int:
    if not isinstance(lease_sec, int):
        raise CoordinatorError("lease_sec must be an integer")
    if lease_sec < MIN_LEASE_SEC or lease_sec > MAX_LEASE_SEC:
        raise CoordinatorError(f"lease_sec must be in [{MIN_LEASE_SEC}, {MAX_LEASE_SEC}]")
    return lease_sec


def default_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "updated_at_utc": "",
        "packets": {},
        "agents": {},
    }


def default_packet_record() -> Dict[str, Any]:
    return {
        "claim_status": "unclaimed",
        "claimed_by": "",
        "claim_token": "",
        "claimed_at_utc": "",
        "claim_expires_at_utc": "",
        "findings": {},
        "findings_updated_at_utc": "",
        "updated_at_utc": "",
    }


def load_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CoordinatorError(f"malformed state file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CoordinatorError(f"state root must be an object: {path}")
    out = default_state()
    out.update(data)
    if not isinstance(out.get("packets"), dict):
        out["packets"] = {}
    if not isinstance(out.get("agents"), dict):
        out["agents"] = {}
    return out


def write_state_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    temp_name = f".{path.name}.tmp-{os.getpid()}-{random_token(6)}"
    temp_path = path.parent / temp_name
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def append_event(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


@contextmanager
def state_lock(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise CoordinatorError(f"failed to acquire lock {lock_file}: {exc}") from exc
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def is_active_claim(packet: Dict[str, Any], now: dt.datetime) -> bool:
    if packet.get("claim_status") != "claimed":
        return False
    raw = str(packet.get("claim_expires_at_utc", "")).strip()
    if not raw:
        return True
    exp = parse_utc(raw)
    if exp is None:
        # Corrupted expiry values should not create sticky claims.
        return False
    return now < exp


class OACPCoordinator:
    """Coordinator API backing both MCP tools and CLI."""

    def __init__(self, oacp_dir: Optional[Path] = None):
        if oacp_dir is None:
            oacp_home = os.environ.get("OACP_HOME")
            oacp_dir = Path(oacp_home) if oacp_home else Path(os.path.expanduser("~/oacp"))
        self.oacp_dir = oacp_dir.expanduser().resolve()

    def _project_paths(self, project: str) -> Tuple[Path, Path, Path, Path]:
        project_name = ensure_project(project)
        project_dir = self.oacp_dir / "projects" / project_name
        if not project_dir.is_dir():
            raise CoordinatorError(f"project directory not found: {project_dir}")
        state_dir = project_dir / "state"
        return (
            project_dir,
            state_dir / STATE_FILE_NAME,
            state_dir / EVENTS_FILE_NAME,
            state_dir / LOCK_FILE_NAME,
        )

    def claim_packet(
        self,
        *,
        project: str,
        packet_id: str,
        agent: str,
        lease_sec: int = DEFAULT_LEASE_SEC,
        force: bool = False,
    ) -> Dict[str, Any]:
        ensure_agent(agent)
        ensure_packet(packet_id)
        lease = ensure_lease_sec(lease_sec)
        _, state_file, events_file, lock_file = self._project_paths(project)
        now = dt.datetime.now(dt.timezone.utc)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        with state_lock(lock_file):
            state = load_state(state_file)
            packets = state.setdefault("packets", {})
            agents = state.setdefault("agents", {})

            packet = packets.get(packet_id)
            if not isinstance(packet, dict):
                packet = default_packet_record()

            prev_owner = str(packet.get("claimed_by", "")).strip()
            prev_active = is_active_claim(packet, now)
            if prev_active and prev_owner and prev_owner != agent and not force:
                raise CoordinatorError(
                    f"packet {packet_id!r} already claimed by {prev_owner!r}; use force=true to override"
                )

            expires = add_seconds_utc(now_str, lease)
            token = f"claim-{random_token(14)}"
            packet.update(
                {
                    "claim_status": "claimed",
                    "claimed_by": agent,
                    "claim_token": token,
                    "claimed_at_utc": now_str,
                    "claim_expires_at_utc": expires,
                    "updated_at_utc": now_str,
                }
            )
            packets[packet_id] = packet

            agent_rec = agents.get(agent)
            if not isinstance(agent_rec, dict):
                agent_rec = {}
            agent_rec["last_seen_utc"] = now_str
            agent_rec["last_action"] = "claim_packet"
            agents[agent] = agent_rec

            state["updated_at_utc"] = now_str
            write_state_atomic(state_file, state)
            append_event(
                events_file,
                {
                    "timestamp_utc": now_str,
                    "event": "claim_packet",
                    "project": project,
                    "packet_id": packet_id,
                    "agent": agent,
                    "lease_sec": lease,
                    "force": bool(force),
                    "previous_owner": prev_owner,
                    "previous_claim_active": prev_active,
                    "claim_token": token,
                    "claim_expires_at_utc": expires,
                },
            )

        return {
            "project": project,
            "packet_id": packet_id,
            "claimed_by": agent,
            "claim_token": token,
            "claim_expires_at_utc": expires,
            "force": bool(force),
        }

    def update_findings(
        self,
        *,
        project: str,
        packet_id: str,
        agent: str,
        findings: List[Dict[str, Any]],
        require_claim: bool = True,
        force: bool = False,
    ) -> Dict[str, Any]:
        ensure_agent(agent)
        ensure_packet(packet_id)
        if not isinstance(findings, list) or not findings:
            raise CoordinatorError("findings must be a non-empty list")
        for idx, item in enumerate(findings):
            if not isinstance(item, dict):
                raise CoordinatorError(f"findings[{idx}] must be an object")
            fid = str(item.get("id", "")).strip()
            if not fid:
                raise CoordinatorError(f"findings[{idx}] missing required field: id")

        _, state_file, events_file, lock_file = self._project_paths(project)
        now = dt.datetime.now(dt.timezone.utc)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        with state_lock(lock_file):
            state = load_state(state_file)
            packets = state.setdefault("packets", {})
            agents = state.setdefault("agents", {})
            packet = packets.get(packet_id)
            if not isinstance(packet, dict):
                packet = default_packet_record()

            if require_claim:
                owner = str(packet.get("claimed_by", "")).strip()
                active = is_active_claim(packet, now)
                if not (active and owner == agent) and not force:
                    raise CoordinatorError(
                        f"agent {agent!r} does not hold an active claim for packet {packet_id!r}"
                    )

            stored_findings = packet.get("findings")
            if not isinstance(stored_findings, dict):
                stored_findings = {}

            added = 0
            updated = 0
            for raw in findings:
                fid = str(raw.get("id", "")).strip()
                finding = dict(raw)
                finding["id"] = fid
                finding["updated_by"] = agent
                finding["updated_at_utc"] = now_str
                if fid in stored_findings:
                    updated += 1
                else:
                    added += 1
                stored_findings[fid] = finding

            packet["findings"] = stored_findings
            packet["findings_updated_at_utc"] = now_str
            packet["updated_at_utc"] = now_str
            packets[packet_id] = packet

            agent_rec = agents.get(agent)
            if not isinstance(agent_rec, dict):
                agent_rec = {}
            agent_rec["last_seen_utc"] = now_str
            agent_rec["last_action"] = "update_findings"
            agents[agent] = agent_rec

            state["updated_at_utc"] = now_str
            write_state_atomic(state_file, state)
            append_event(
                events_file,
                {
                    "timestamp_utc": now_str,
                    "event": "update_findings",
                    "project": project,
                    "packet_id": packet_id,
                    "agent": agent,
                    "count": len(findings),
                    "added": added,
                    "updated": updated,
                    "require_claim": bool(require_claim),
                    "force": bool(force),
                },
            )

        return {
            "project": project,
            "packet_id": packet_id,
            "updated_by": agent,
            "count": len(findings),
            "added": added,
            "updated": updated,
            "total_findings": len(stored_findings),
        }

    def get_agent_state(self, *, project: str, agent: str) -> Dict[str, Any]:
        ensure_agent(agent)
        project_dir, state_file, _events_file, lock_file = self._project_paths(project)
        now = dt.datetime.now(dt.timezone.utc)

        with state_lock(lock_file):
            state = load_state(state_file)
            packets = state.get("packets", {})
            agents = state.get("agents", {})

            claims: List[Dict[str, Any]] = []
            for packet_id, packet in packets.items():
                if not isinstance(packet, dict):
                    continue
                if str(packet.get("claimed_by", "")).strip() != agent:
                    continue
                if not is_active_claim(packet, now):
                    continue
                claims.append(
                    {
                        "packet_id": packet_id,
                        "claim_expires_at_utc": str(packet.get("claim_expires_at_utc", "")),
                        "claimed_at_utc": str(packet.get("claimed_at_utc", "")),
                    }
                )

            agent_rec = agents.get(agent)
            if not isinstance(agent_rec, dict):
                agent_rec = {}
            last_seen = str(agent_rec.get("last_seen_utc", ""))
            last_action = str(agent_rec.get("last_action", ""))

        session_file = project_dir / "state" / SESSION_STATE_FILE_NAME
        active_sessions: List[Dict[str, Any]] = []
        session_packet_states: List[Dict[str, Any]] = []
        if session_file.is_file():
            try:
                session_state = json.loads(session_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                session_state = {}
            if isinstance(session_state, dict):
                raw_active = session_state.get("active_sessions", {})
                if isinstance(raw_active, dict):
                    for sid, record in raw_active.items():
                        if not isinstance(record, dict):
                            continue
                        if str(record.get("agent", "")) != agent:
                            continue
                        active_sessions.append(
                            {
                                "session_id": sid,
                                "runtime": str(record.get("runtime", "")),
                                "role": str(record.get("role", "")),
                                "packet_id": str(record.get("packet_id", "")),
                                "started_at_utc": str(record.get("started_at_utc", "")),
                            }
                        )
                raw_packets = session_state.get("packets", {})
                if isinstance(raw_packets, dict):
                    for pid, packet in raw_packets.items():
                        if not isinstance(packet, dict):
                            continue
                        if str(packet.get("last_session_agent", "")) != agent:
                            continue
                        session_packet_states.append(
                            {
                                "packet_id": pid,
                                "state": str(packet.get("state", "")),
                                "session_open": bool(packet.get("session_open", False)),
                                "updated_at_utc": str(packet.get("updated_at_utc", "")),
                            }
                        )

        return {
            "project": project,
            "agent": agent,
            "last_seen_utc": last_seen,
            "last_action": last_action,
            "active_claims": sorted(claims, key=lambda x: x["packet_id"]),
            "active_sessions": sorted(active_sessions, key=lambda x: x["session_id"]),
            "session_packet_states": sorted(session_packet_states, key=lambda x: x["packet_id"]),
        }


def tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "claim_packet",
            "description": "Claim a packet for an agent with a time-bounded lease.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "packet_id": {"type": "string"},
                    "agent": {"type": "string"},
                    "lease_sec": {"type": "integer", "minimum": MIN_LEASE_SEC, "maximum": MAX_LEASE_SEC},
                    "force": {"type": "boolean"},
                },
                "required": ["project", "packet_id", "agent"],
                "additionalProperties": False,
            },
        },
        {
            "name": "update_findings",
            "description": "Upsert findings for a packet, optionally requiring an active claim.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "packet_id": {"type": "string"},
                    "agent": {"type": "string"},
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "require_claim": {"type": "boolean"},
                    "force": {"type": "boolean"},
                },
                "required": ["project", "packet_id", "agent", "findings"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_agent_state",
            "description": "Return active claims/sessions and recent packet state for an agent.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "agent": {"type": "string"},
                },
                "required": ["project", "agent"],
                "additionalProperties": False,
            },
        },
    ]


def call_tool(coordinator: OACPCoordinator, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if tool_name == "claim_packet":
        raw_lease = args.get("lease_sec", DEFAULT_LEASE_SEC)
        if isinstance(raw_lease, bool):
            lease_sec = DEFAULT_LEASE_SEC
        else:
            try:
                lease_sec = int(raw_lease)
            except (TypeError, ValueError):
                lease_sec = DEFAULT_LEASE_SEC
        return coordinator.claim_packet(
            project=str(args.get("project", "")),
            packet_id=str(args.get("packet_id", "")),
            agent=str(args.get("agent", "")),
            lease_sec=lease_sec,
            force=bool(args.get("force", False)),
        )
    if tool_name == "update_findings":
        findings = args.get("findings")
        if not isinstance(findings, list):
            raise CoordinatorError("update_findings requires findings as a list")
        return coordinator.update_findings(
            project=str(args.get("project", "")),
            packet_id=str(args.get("packet_id", "")),
            agent=str(args.get("agent", "")),
            findings=findings,
            require_claim=bool(args.get("require_claim", True)),
            force=bool(args.get("force", False)),
        )
    if tool_name == "get_agent_state":
        return coordinator.get_agent_state(
            project=str(args.get("project", "")),
            agent=str(args.get("agent", "")),
        )
    raise CoordinatorError(f"unknown tool: {tool_name}")


def read_mcp_message(stream: Any) -> Optional[Dict[str, Any]]:
    first = stream.readline()
    if not first:
        return None

    # Fallback for line-delimited JSON input (useful in tests).
    if first.lstrip().startswith(b"{"):
        return json.loads(first.decode("utf-8"))

    headers: Dict[str, str] = {}
    line = first
    while line and line not in (b"\r\n", b"\n"):
        decoded = line.decode("utf-8").strip()
        if ":" not in decoded:
            raise CoordinatorError(f"invalid MCP header line: {decoded!r}")
        key, value = decoded.split(":", 1)
        headers[key.strip().lower()] = value.strip()
        line = stream.readline()

    raw_length = headers.get("content-length")
    if not raw_length:
        raise CoordinatorError("missing Content-Length header")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise CoordinatorError(f"invalid Content-Length value: {raw_length!r}") from exc
    body = stream.read(length)
    if len(body) != length:
        raise CoordinatorError("unexpected EOF while reading MCP body")
    return json.loads(body.decode("utf-8"))


def write_mcp_message(stream: Any, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    stream.write(header)
    stream.write(body)
    stream.flush()


def jsonrpc_result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_mcp_request(coordinator: OACPCoordinator, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = str(request.get("method", ""))
    request_id = request.get("id")
    params = request.get("params", {})
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        if request_id is None:
            return None
        return jsonrpc_result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        if request_id is None:
            return None
        return jsonrpc_result(request_id, {"tools": tool_definitions()})

    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        tool_args = params.get("arguments", {})
        if not isinstance(tool_args, dict):
            tool_args = {}
        try:
            data = call_tool(coordinator, tool_name, tool_args)
        except CoordinatorError as exc:
            if request_id is None:
                return None
            return jsonrpc_error(request_id, -32000, str(exc))
        if request_id is None:
            return None
        return jsonrpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(data, sort_keys=True)}],
                "structuredContent": data,
            },
        )

    if request_id is None:
        return None
    return jsonrpc_error(request_id, -32601, f"method not found: {method}")


def serve_stdio(oacp_dir: Optional[Path] = None) -> int:
    coordinator = OACPCoordinator(oacp_dir=oacp_dir)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            request = read_mcp_message(stdin)
        except json.JSONDecodeError as exc:
            write_mcp_message(stdout, jsonrpc_error(None, -32700, f"parse error: {exc}"))
            continue
        except CoordinatorError as exc:
            write_mcp_message(stdout, jsonrpc_error(None, -32000, str(exc)))
            continue

        if request is None:
            break
        if not isinstance(request, dict):
            write_mcp_message(stdout, jsonrpc_error(None, -32600, "invalid request shape"))
            continue

        response = handle_mcp_request(coordinator, request)
        if response is not None:
            write_mcp_message(stdout, response)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OACP MCP state coordinator server.")
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run MCP stdio server (default)")

    claim = sub.add_parser("claim_packet", help="CLI helper: claim a packet")
    claim.add_argument("project")
    claim.add_argument("packet_id")
    claim.add_argument("agent")
    claim.add_argument("--lease-sec", type=int, default=DEFAULT_LEASE_SEC)
    claim.add_argument("--force", action="store_true")

    upd = sub.add_parser("update_findings", help="CLI helper: update findings")
    upd.add_argument("project")
    upd.add_argument("packet_id")
    upd.add_argument("agent")
    upd.add_argument("--findings-json", required=True, help="JSON array of finding objects")
    upd.add_argument("--no-require-claim", action="store_true")
    upd.add_argument("--force", action="store_true")

    ast = sub.add_parser("get_agent_state", help="CLI helper: get agent state")
    ast.add_argument("project")
    ast.add_argument("agent")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    oacp_home = args.oacp_dir or os.environ.get("OACP_HOME") or os.path.expanduser("~/oacp")
    oacp_dir = Path(oacp_home).expanduser().resolve()

    if args.command in (None, "serve"):
        return serve_stdio(oacp_dir=oacp_dir)

    coordinator = OACPCoordinator(oacp_dir=oacp_dir)
    try:
        if args.command == "claim_packet":
            out = coordinator.claim_packet(
                project=args.project,
                packet_id=args.packet_id,
                agent=args.agent,
                lease_sec=args.lease_sec,
                force=bool(args.force),
            )
        elif args.command == "update_findings":
            try:
                findings = json.loads(args.findings_json)
            except json.JSONDecodeError as exc:
                raise CoordinatorError(f"invalid --findings-json: {exc}") from exc
            out = coordinator.update_findings(
                project=args.project,
                packet_id=args.packet_id,
                agent=args.agent,
                findings=findings,
                require_claim=not bool(args.no_require_claim),
                force=bool(args.force),
            )
        elif args.command == "get_agent_state":
            out = coordinator.get_agent_state(project=args.project, agent=args.agent)
        else:
            raise CoordinatorError(f"unknown command: {args.command}")
    except CoordinatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
