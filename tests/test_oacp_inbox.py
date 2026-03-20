# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for oacp_inbox.py."""

from __future__ import annotations

import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from oacp_inbox import list_inbox, main, render_report  # noqa: E402


def _write_message(
    root: Path,
    project: str,
    agent: str,
    filename: str,
    *,
    sender: str,
    msg_type: str,
    priority: str,
    subject: str,
    created_at_utc: str,
) -> None:
    inbox_dir = root / "projects" / project / "agents" / agent / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / filename).write_text(
        "\n".join(
            [
                'id: "msg-1"',
                f'from: "{sender}"',
                f'to: "{agent}"',
                f'type: "{msg_type}"',
                f'priority: "{priority}"',
                f'created_at_utc: "{created_at_utc}"',
                f'subject: "{subject}"',
                'body: "Body"',
                "",
            ]
        ),
        encoding="utf-8",
    )


class TestListInbox(unittest.TestCase):
    def test_lists_single_agent_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "demo",
                "claude",
                "20260319120000_codex_task_request.yaml",
                sender="codex",
                msg_type="task_request",
                priority="P1",
                subject="Implement feature",
                created_at_utc="2026-03-19T12:00:00Z",
            )
            report = list_inbox(
                "demo",
                agent="claude",
                oacp_dir=root,
                now=dt.datetime(2026, 3, 19, 14, 0, 0, tzinfo=dt.timezone.utc),
            )
        self.assertEqual(report["project"], "demo")
        self.assertEqual(report["agents"][0]["agent"], "claude")
        self.assertEqual(report["agents"][0]["message_count"], 1)
        self.assertEqual(report["agents"][0]["messages"][0]["from"], "codex")
        self.assertEqual(report["agents"][0]["messages"][0]["age"], "2h")

    def test_lists_all_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "demo",
                "claude",
                "20260319120000_codex_task_request.yaml",
                sender="codex",
                msg_type="task_request",
                priority="P1",
                subject="One",
                created_at_utc="2026-03-19T12:00:00Z",
            )
            (root / "projects" / "demo" / "agents" / "codex" / "inbox").mkdir(parents=True)
            report = list_inbox("demo", list_all=True, oacp_dir=root)
        self.assertEqual([agent["agent"] for agent in report["agents"]], ["claude", "codex"])
        self.assertEqual(report["agents"][0]["message_count"], 1)
        self.assertEqual(report["agents"][1]["message_count"], 0)

    def test_render_report_empty_inbox_is_clean(self) -> None:
        report = {
            "project": "demo",
            "mode": "agent",
            "agents": [{"agent": "claude", "message_count": 0, "messages": []}],
        }
        rendered = render_report(report)
        self.assertIn("INBOX: claude (demo) — 0 messages", rendered)
        self.assertIn("No pending messages.", rendered)

    def test_malformed_yaml_is_reported_without_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inbox_dir = root / "projects" / "demo" / "agents" / "claude" / "inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            (inbox_dir / "broken.yaml").write_text("subject: [unterminated\n", encoding="utf-8")
            report = list_inbox("demo", agent="claude", oacp_dir=root)
        self.assertEqual(report["agents"][0]["message_count"], 1)
        message = report["agents"][0]["messages"][0]
        self.assertEqual(message["subject"], "(invalid YAML)")
        self.assertIn("load_error", message)


class TestMainCli(unittest.TestCase):
    def _run_main(self, args):
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            code = main(args)
            stdout = sys.stdout.getvalue()
            stderr = sys.stderr.getvalue()
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        return code, stdout, stderr

    def test_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "demo",
                "claude",
                "20260319120000_codex_task_request.yaml",
                sender="codex",
                msg_type="task_request",
                priority="P1",
                subject="Implement feature",
                created_at_utc="2026-03-19T12:00:00Z",
            )
            code, stdout, stderr = self._run_main(
                ["demo", "--agent", "claude", "--oacp-dir", tmpdir, "--json"]
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["project"], "demo")
        self.assertEqual(payload["agents"][0]["messages"][0]["type"], "task_request")

    def test_missing_project_returns_error(self) -> None:
        code, stdout, stderr = self._run_main(["missing", "--all"])
        self.assertEqual(code, 1)
        self.assertIn("project 'missing' not found", stderr)


if __name__ == "__main__":
    unittest.main()
