# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/oacp_watch.py."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from oacp_watch import main  # noqa: E402


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
) -> Path:
    inbox_dir = root / "projects" / project / "agents" / agent / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / filename
    path.write_text(
        "\n".join(
            [
                'id: "msg-1"',
                f'from: "{sender}"',
                f'to: "{agent}"',
                f'type: "{msg_type}"',
                f'priority: "{priority}"',
                'created_at_utc: "2026-04-11T05:00:00Z"',
                f'subject: "{subject}"',
                'body: "Body"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


class TestOacpWatch(unittest.TestCase):
    def _run(self, args):
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

    def test_first_run_emits_existing_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "demo",
                "codex",
                "20260411050000_iris_task_request_abcd.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P2",
                subject="First",
            )
            code, stdout, stderr = self._run(
                ["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "new_message")
        self.assertEqual(events[0]["project"], "demo")
        self.assertEqual(events[0]["subject"], "First")

    def test_second_run_with_same_state_emits_no_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "demo",
                "codex",
                "20260411050000_iris_task_request_abcd.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P2",
                subject="First",
            )
            first_code, _, _ = self._run(
                ["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"]
            )
            second_code, stdout, stderr = self._run(
                ["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"]
            )
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_new_message_after_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "demo",
                "codex",
                "20260411050000_iris_task_request_abcd.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P2",
                subject="First",
            )
            self._run(["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"])
            _write_message(
                root,
                "demo",
                "codex",
                "20260411050100_iris_task_request_dcba.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P1",
                subject="Second",
            )
            code, stdout, stderr = self._run(
                ["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "new_message")
        self.assertEqual(events[0]["subject"], "Second")
        self.assertEqual(events[0]["priority"], "P1")

    def test_removed_message_emits_message_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            msg_path = _write_message(
                root,
                "demo",
                "codex",
                "20260411050000_iris_task_request_abcd.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P2",
                subject="First",
            )
            self._run(["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"])
            msg_path.unlink()
            code, stdout, stderr = self._run(
                ["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "message_archived")
        self.assertEqual(events[0]["subject"], "First")

    def test_repeatable_project_keeps_argument_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "bravo",
                "codex",
                "20260411050000_iris_task_request_abcd.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P2",
                subject="Bravo",
            )
            _write_message(
                root,
                "alpha",
                "codex",
                "20260411050000_iris_task_request_dcba.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P1",
                subject="Alpha",
            )
            code, stdout, stderr = self._run(
                [
                    "--agent",
                    "codex",
                    "--project",
                    "bravo",
                    "--project",
                    "alpha",
                    "--oacp-dir",
                    tmpdir,
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual([event["project"] for event in events], ["bravo", "alpha"])

    def test_all_projects_discovers_inbox_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "bravo",
                "codex",
                "20260411050000_iris_task_request_abcd.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P2",
                subject="Bravo",
            )
            _write_message(
                root,
                "alpha",
                "codex",
                "20260411050000_iris_task_request_dcba.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P1",
                subject="Alpha",
            )
            code, stdout, stderr = self._run(
                ["--agent", "codex", "--all-projects", "--oacp-dir", tmpdir, "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual([event["project"] for event in events], ["alpha", "bravo"])

    def test_malformed_yaml_emits_error_and_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inbox_dir = root / "projects" / "demo" / "agents" / "codex" / "inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            (inbox_dir / "broken.yaml").write_text("subject: [unterminated\n", encoding="utf-8")
            code, stdout, stderr = self._run(
                ["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"]
            )
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", stderr)
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "error")
        self.assertEqual(events[0]["file"], "broken.yaml")

    def test_malformed_yaml_does_not_regress_valid_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "demo",
                "codex",
                "20260411050000_iris_task_request_abcd.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P2",
                subject="Good",
            )
            inbox_dir = root / "projects" / "demo" / "agents" / "codex" / "inbox"
            (inbox_dir / "broken.yaml").write_text("subject: [unterminated\n", encoding="utf-8")

            first_code, first_stdout, first_stderr = self._run(
                ["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"]
            )
            second_code, second_stdout, second_stderr = self._run(
                ["--agent", "codex", "--project", "demo", "--oacp-dir", tmpdir, "--json"]
            )

        self.assertEqual(first_code, 1)
        self.assertIn("ERROR:", first_stderr)
        first_events = [json.loads(line) for line in first_stdout.splitlines() if line.strip()]
        self.assertEqual([event["event"] for event in first_events], ["error", "new_message"])
        self.assertEqual(first_events[1]["subject"], "Good")

        self.assertEqual(second_code, 1)
        self.assertIn("ERROR:", second_stderr)
        second_events = [json.loads(line) for line in second_stdout.splitlines() if line.strip()]
        self.assertEqual(len(second_events), 1)
        self.assertEqual(second_events[0]["event"], "error")

    def test_invalid_project_emits_error_and_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code, stdout, stderr = self._run(
                ["--agent", "codex", "--project", "missing", "--oacp-dir", tmpdir, "--json"]
            )
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", stderr)
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "error")
        self.assertEqual(events[0]["project"], "missing")

    def test_invalid_project_does_not_block_valid_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_message(
                root,
                "valid",
                "codex",
                "20260411050000_iris_task_request_abcd.yaml",
                sender="iris",
                msg_type="task_request",
                priority="P2",
                subject="Good",
            )
            code, stdout, stderr = self._run(
                [
                    "--agent",
                    "codex",
                    "--project",
                    "valid",
                    "--project",
                    "missing",
                    "--oacp-dir",
                    tmpdir,
                    "--json",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", stderr)
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual([event["event"] for event in events], ["error", "new_message"])
        self.assertEqual(events[0]["project"], "missing")
        self.assertEqual(events[1]["project"], "valid")


if __name__ == "__main__":
    unittest.main()
