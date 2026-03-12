# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/session_lifecycle_hooks.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from session_lifecycle_hooks import (  # noqa: E402
    _build_parser,
    SessionLifecycleError,
    close_session,
    init_session,
    main,
)


def _setup_project(hub_dir: Path, project: str = "demo") -> Path:
    project_dir = hub_dir / "projects" / project
    (project_dir / "state").mkdir(parents=True, exist_ok=True)
    return project_dir


class TestSessionLifecycleHooks(unittest.TestCase):
    def test_init_creates_state_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            report = init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="orchestrator",
                packet_id="20260212_demo_gemini_r1",
                packet_state=None,
                conversation_id="conv-20260212-demo-001",
                branch="gemini/qa-loop",
                notes="start",
                session_id=None,
                dry_run=False,
            )

            self.assertEqual(report["event"], "init_session")
            self.assertTrue(report["session_id"].startswith("sess-"))
            self.assertEqual(report["packet_state"], "in_review")

            state_file = Path(report["state_file"])
            events_file = Path(report["events_file"])
            self.assertTrue(state_file.is_file())
            self.assertTrue(events_file.is_file())

            state = json.loads(state_file.read_text(encoding="utf-8"))
            sid = report["session_id"]
            self.assertIn(sid, state["active_sessions"])
            packet = state["packets"]["20260212_demo_gemini_r1"]
            self.assertTrue(packet["session_open"])
            self.assertEqual(packet["state"], "in_review")

            lines = events_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["event"], "init_session")
            self.assertEqual(event["session_id"], sid)

    def test_close_clears_active_and_updates_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            opened = init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="qa",
                packet_id="20260212_demo_gemini_r2",
                packet_state=None,
                conversation_id=None,
                branch=None,
                notes=None,
                session_id=None,
                dry_run=False,
            )

            closed = close_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                packet_id=None,
                packet_state=None,
                close_status="completed",
                session_id=opened["session_id"],
                notes="done",
                dry_run=False,
            )

            self.assertEqual(closed["event"], "close_session")
            self.assertEqual(closed["packet_state"], "findings_returned")

            state = json.loads(Path(closed["state_file"]).read_text(encoding="utf-8"))
            self.assertEqual(state["active_sessions"], {})
            packet = state["packets"]["20260212_demo_gemini_r2"]
            self.assertFalse(packet["session_open"])
            self.assertEqual(packet["state"], "findings_returned")
            self.assertEqual(packet["last_close_status"], "completed")

            lines = Path(closed["events_file"]).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[-1])["event"], "close_session")

    def test_close_uses_single_active_session_without_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="reviewer",
                packet_id="packet-1",
                packet_state=None,
                conversation_id=None,
                branch=None,
                notes=None,
                session_id=None,
                dry_run=False,
            )

            closed = close_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                packet_id=None,
                packet_state=None,
                close_status="completed",
                session_id=None,
                notes=None,
                dry_run=False,
            )
            self.assertEqual(closed["packet_id"], "packet-1")

    def test_close_requires_session_id_when_multiple_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="reviewer",
                packet_id="packet-1",
                packet_state=None,
                conversation_id=None,
                branch=None,
                notes=None,
                session_id=None,
                dry_run=False,
            )
            init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="reviewer",
                packet_id="packet-2",
                packet_state=None,
                conversation_id=None,
                branch=None,
                notes=None,
                session_id=None,
                dry_run=False,
            )

            with self.assertRaisesRegex(SessionLifecycleError, "multiple active sessions"):
                close_session(
                    project="demo",
                    hub_dir=hub_dir,
                    agent="gemini",
                    packet_id=None,
                    packet_state=None,
                    close_status="completed",
                    session_id=None,
                    notes=None,
                    dry_run=False,
                )

    def test_close_rejects_cross_agent_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            opened = init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="qa",
                packet_id="packet-cross-agent",
                packet_state=None,
                conversation_id=None,
                branch=None,
                notes=None,
                session_id=None,
                dry_run=False,
            )

            with self.assertRaisesRegex(SessionLifecycleError, "belongs to agent"):
                close_session(
                    project="demo",
                    hub_dir=hub_dir,
                    agent="codex",
                    packet_id=None,
                    packet_state=None,
                    close_status="completed",
                    session_id=opened["session_id"],
                    notes=None,
                    dry_run=False,
                )

    def test_blocked_close_maps_to_escalated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            opened = init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="qa",
                packet_id="packet-blocked",
                packet_state=None,
                conversation_id=None,
                branch=None,
                notes=None,
                session_id=None,
                dry_run=False,
            )

            closed = close_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                packet_id=None,
                packet_state=None,
                close_status="blocked",
                session_id=opened["session_id"],
                notes=None,
                dry_run=False,
            )
            self.assertEqual(closed["packet_state"], "escalated")

    def test_aborted_close_keeps_prior_packet_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            opened = init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="qa",
                packet_id="packet-abort",
                packet_state="in_review",
                conversation_id=None,
                branch=None,
                notes=None,
                session_id=None,
                dry_run=False,
            )

            closed = close_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                packet_id=None,
                packet_state=None,
                close_status="aborted",
                session_id=opened["session_id"],
                notes=None,
                dry_run=False,
            )
            self.assertEqual(closed["packet_state"], "in_review")

    def test_dry_run_writes_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            init_report = init_session(
                project="demo",
                hub_dir=hub_dir,
                agent="gemini",
                runtime="gemini",
                role="orchestrator",
                packet_id="packet-dry-run",
                packet_state=None,
                conversation_id=None,
                branch=None,
                notes=None,
                session_id=None,
                dry_run=True,
            )
            self.assertEqual(init_report["event"], "init_session")

            state_file = hub_dir / "projects" / "demo" / "state" / "session_lifecycle_state.json"
            events_file = hub_dir / "projects" / "demo" / "state" / "session_lifecycle_events.jsonl"
            self.assertFalse(state_file.exists())
            self.assertFalse(events_file.exists())

    def test_cli_main_json_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_dir = Path(tmp)
            _setup_project(hub_dir, "demo")

            with mock.patch.object(
                sys,
                "argv",
                [
                    "session_lifecycle_hooks.py",
                    "--hub-dir",
                    str(hub_dir),
                    "--json",
                    "init_session",
                    "demo",
                    "--agent",
                    "gemini",
                    "--packet-id",
                    "packet-main",
                ],
            ):
                with mock.patch("sys.stdout") as stdout:
                    rc = main()
                    self.assertEqual(rc, 0)
                    rendered = "".join(call.args[0] for call in stdout.write.call_args_list)
                    payload = json.loads(rendered)
                    self.assertEqual(payload["event"], "init_session")

    def test_parser_defaults_runtime_to_unknown(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["init_session", "demo", "--agent", "claude"])
        self.assertEqual(args.runtime, "unknown")


if __name__ == "__main__":
    unittest.main()
