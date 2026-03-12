# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for mcp_servers/oacp_coordinator.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_servers"))

from oacp_coordinator import (  # noqa: E402
    CoordinatorError,
    OACPCoordinator,
    handle_mcp_request,
)


def _setup_project(hub_dir: Path, project: str = "demo") -> Path:
    project_dir = hub_dir / "projects" / project
    (project_dir / "state").mkdir(parents=True, exist_ok=True)
    return project_dir


class TestOACPCoordinator(unittest.TestCase):
    def test_claim_packet_creates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)

            out = c.claim_packet(project="demo", packet_id="packet-1", agent="codex", lease_sec=120)
            self.assertEqual(out["packet_id"], "packet-1")
            self.assertEqual(out["claimed_by"], "codex")
            self.assertTrue(out["claim_token"].startswith("claim-"))

            state_file = hub / "projects" / "demo" / "state" / "oacp_coordinator_state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            pkt = state["packets"]["packet-1"]
            self.assertEqual(pkt["claimed_by"], "codex")
            self.assertEqual(pkt["claim_status"], "claimed")

    def test_claim_conflict_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)

            c.claim_packet(project="demo", packet_id="packet-1", agent="gemini", lease_sec=120)
            with self.assertRaisesRegex(CoordinatorError, "already claimed"):
                c.claim_packet(project="demo", packet_id="packet-1", agent="codex", lease_sec=120)

    def test_claim_force_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)

            c.claim_packet(project="demo", packet_id="packet-1", agent="gemini", lease_sec=120)
            out = c.claim_packet(
                project="demo",
                packet_id="packet-1",
                agent="codex",
                lease_sec=120,
                force=True,
            )
            self.assertEqual(out["claimed_by"], "codex")

    def test_claim_allows_override_when_existing_expiry_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            project_dir = _setup_project(hub, "demo")
            state_file = project_dir / "state" / "oacp_coordinator_state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": "0.1.0",
                        "updated_at_utc": "2026-02-12T00:00:00Z",
                        "packets": {
                            "packet-1": {
                                "claim_status": "claimed",
                                "claimed_by": "gemini",
                                "claim_token": "claim-old",
                                "claimed_at_utc": "2026-02-12T00:00:00Z",
                                "claim_expires_at_utc": "not-a-timestamp",
                                "findings": {},
                                "findings_updated_at_utc": "",
                                "updated_at_utc": "2026-02-12T00:00:00Z",
                            }
                        },
                        "agents": {},
                    }
                ),
                encoding="utf-8",
            )
            c = OACPCoordinator(oacp_dir=hub)

            out = c.claim_packet(project="demo", packet_id="packet-1", agent="codex", lease_sec=120)
            self.assertEqual(out["claimed_by"], "codex")

    def test_update_findings_requires_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)

            with self.assertRaisesRegex(CoordinatorError, "does not hold an active claim"):
                c.update_findings(
                    project="demo",
                    packet_id="packet-1",
                    agent="codex",
                    findings=[{"id": "F-1", "severity": "P1", "status": "open"}],
                )

    def test_update_findings_with_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)
            c.claim_packet(project="demo", packet_id="packet-1", agent="codex", lease_sec=120)

            out = c.update_findings(
                project="demo",
                packet_id="packet-1",
                agent="codex",
                findings=[
                    {"id": "F-1", "severity": "P1", "blocking": True, "status": "open"},
                    {"id": "F-2", "severity": "P2", "blocking": False, "status": "open"},
                ],
            )
            self.assertEqual(out["count"], 2)
            self.assertEqual(out["added"], 2)
            self.assertEqual(out["total_findings"], 2)

    def test_get_agent_state_includes_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            project_dir = _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)
            c.claim_packet(project="demo", packet_id="packet-1", agent="gemini", lease_sec=120)

            session_state = {
                "active_sessions": {
                    "sess-1": {
                        "agent": "gemini",
                        "runtime": "gemini",
                        "role": "orchestrator",
                        "packet_id": "packet-1",
                        "started_at_utc": "2026-02-12T00:00:00Z",
                    }
                },
                "packets": {
                    "packet-1": {
                        "state": "in_review",
                        "session_open": True,
                        "last_session_agent": "gemini",
                        "updated_at_utc": "2026-02-12T00:10:00Z",
                    }
                },
            }
            (project_dir / "state" / "session_lifecycle_state.json").write_text(
                json.dumps(session_state),
                encoding="utf-8",
            )

            out = c.get_agent_state(project="demo", agent="gemini")
            self.assertEqual(len(out["active_claims"]), 1)
            self.assertEqual(out["active_claims"][0]["packet_id"], "packet-1")
            self.assertEqual(len(out["active_sessions"]), 1)
            self.assertEqual(out["active_sessions"][0]["session_id"], "sess-1")

    def test_mcp_tools_call_claim_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)

            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "claim_packet",
                    "arguments": {
                        "project": "demo",
                        "packet_id": "packet-1",
                        "agent": "codex",
                    },
                },
            }
            resp = handle_mcp_request(c, req)
            self.assertIsNotNone(resp)
            self.assertIn("result", resp)
            structured = resp["result"]["structuredContent"]
            self.assertEqual(structured["packet_id"], "packet-1")

    def test_mcp_tools_call_claim_packet_bad_lease_uses_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)

            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "claim_packet",
                    "arguments": {
                        "project": "demo",
                        "packet_id": "packet-1",
                        "agent": "codex",
                        "lease_sec": "abc",
                    },
                },
            }
            resp = handle_mcp_request(c, req)
            self.assertIsNotNone(resp)
            self.assertIn("result", resp)
            self.assertNotIn("error", resp)

    def test_mcp_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            _setup_project(hub, "demo")
            c = OACPCoordinator(oacp_dir=hub)

            req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            resp = handle_mcp_request(c, req)
            self.assertIsNotNone(resp)
            tools = resp["result"]["tools"]
            names = {tool["name"] for tool in tools}
            self.assertIn("claim_packet", names)
            self.assertIn("update_findings", names)
            self.assertIn("get_agent_state", names)


if __name__ == "__main__":
    unittest.main()
