# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for create_handoff_packet.py."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from create_handoff_packet import main, render_packet  # noqa: E402
from handoff_schema import validate_handoff_packet_text  # noqa: E402


class TestRenderPacket(unittest.TestCase):
    def test_rendered_packet_matches_schema(self) -> None:
        text = render_packet(
            {
                "source_agent": "codex",
                "target_agent": "claude",
                "intent": "Transfer #77 context",
                "artifacts_to_review": ["PR #83"],
                "definition_of_done": ["Open merge-ready PR"],
                "suggested_next_steps": ["Continue implementation"],
            }
        )
        self.assertEqual(validate_handoff_packet_text(text), [])


class TestCreateHandoffPacketMain(unittest.TestCase):
    def test_missing_project_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                rc = main(
                    [
                        "missing-project",
                        "--from",
                        "codex",
                        "--to",
                        "claude",
                        "--intent",
                        "test",
                        "--oacp-dir",
                        tmpdir,
                    ]
                )
            self.assertEqual(rc, 2)
            self.assertIn("project directory not found", stderr.getvalue())

    def test_dry_run_writes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "projects" / "test-project"
            project_dir.mkdir(parents=True)
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                rc = main(
                    [
                        "test-project",
                        "--from",
                        "codex",
                        "--to",
                        "claude",
                        "--intent",
                        "handoff",
                        "--oacp-dir",
                        tmpdir,
                        "--dry-run",
                    ]
                )
            self.assertEqual(rc, 0)
            output = stdout.getvalue()
            self.assertIn("source_agent: \"codex\"", output)
            self.assertIn("target_agent: \"claude\"", output)

    def test_json_dry_run_outputs_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "projects" / "test-project"
            project_dir.mkdir(parents=True)
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                rc = main(
                    [
                        "test-project",
                        "--from",
                        "codex",
                        "--to",
                        "claude",
                        "--intent",
                        "handoff",
                        "--oacp-dir",
                        tmpdir,
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertIn("packet", payload)

    def test_write_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "projects" / "test-project"
            project_dir.mkdir(parents=True)
            out = Path(tmpdir) / "handoff.yaml"
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                rc = main(
                    [
                        "test-project",
                        "--from",
                        "codex",
                        "--to",
                        "claude",
                        "--intent",
                        "handoff",
                        "--oacp-dir",
                        tmpdir,
                        "--output",
                        str(out),
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            self.assertIn("OK:", stdout.getvalue())

    def test_invalid_packet_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "projects" / "test-project"
            project_dir.mkdir(parents=True)
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                rc = main(
                    [
                        "test-project",
                        "--from",
                        "codex",
                        "--to",
                        "codex",
                        "--intent",
                        "handoff",
                        "--oacp-dir",
                        tmpdir,
                    ]
                )
            self.assertEqual(rc, 1)
            self.assertIn("must differ", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
