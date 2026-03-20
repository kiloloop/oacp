# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for the installable oacp CLI."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from oacp import __version__
from oacp import cli


class TestOacpCli(unittest.TestCase):
    def _run(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_help(self) -> None:
        code, stdout, stderr = self._run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Usage: oacp", stdout)
        self.assertEqual(stderr, "")

    def test_version(self) -> None:
        code, stdout, stderr = self._run(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), __version__)
        self.assertEqual(stderr, "")

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_command(self, run_script) -> None:
        code, stdout, stderr = self._run(["doctor", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with("oacp_doctor.py", ["--json"])

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_add_agent(self, run_script) -> None:
        code, stdout, stderr = self._run(["add-agent", "demo", "alice", "--runtime", "claude"])
        self.assertEqual(code, 0)
        run_script.assert_called_once_with(
            "add_agent.py", ["demo", "alice", "--runtime", "claude"]
        )

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_memory_namespace(self, run_script) -> None:
        code, stdout, stderr = self._run(["memory", "archive", "demo", "notes.md"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with(
            "memory_cli.py", ["archive", "demo", "notes.md"]
        )

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_inbox(self, run_script) -> None:
        code, stdout, stderr = self._run(["inbox", "demo", "--agent", "codex"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with(
            "oacp_inbox.py", ["demo", "--agent", "codex"]
        )

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_setup(self, run_script) -> None:
        code, stdout, stderr = self._run(["setup", "claude", "--project", "demo"])
        self.assertEqual(code, 0)
        run_script.assert_called_once_with(
            "setup_runtime.py", ["claude", "--project", "demo"]
        )

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_help_for_subcommand(self, run_script) -> None:
        code, stdout, stderr = self._run(["help", "send"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with("send_inbox_message.py", ["--help"])

    def test_unknown_command(self) -> None:
        code, stdout, stderr = self._run(["unknown"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("unknown command", stderr)

    def test_help_text_includes_new_commands(self) -> None:
        code, stdout, stderr = self._run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("add-agent", stdout)
        self.assertIn("inbox", stdout)
        self.assertIn("memory", stdout)
        self.assertIn("setup", stdout)

    def test_run_script_restores_sys_path_after_nested_mutation(self) -> None:
        script_path = "/tmp/send_inbox_message.py"
        original_sys_path = list(cli.sys.path)

        def mutate_sys_path(path: str, run_name: str) -> None:
            self.assertEqual(path, script_path)
            self.assertEqual(run_name, "__main__")
            cli.sys.path.insert(0, "/nested")
            cli.sys.path.insert(0, "/tmp")

        with (
            mock.patch("oacp.cli._script_path", return_value=cli.nullcontext(script_path)),
            mock.patch("oacp.cli.runpy.run_path", side_effect=mutate_sys_path),
        ):
            code = cli._run_script("send_inbox_message.py", ["demo"])

        self.assertEqual(code, 0)
        self.assertEqual(cli.sys.path, original_sys_path)


if __name__ == "__main__":
    unittest.main()
