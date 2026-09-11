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

    @mock.patch("oacp.cli.os.execvp")
    @mock.patch("oacp.cli.shutil.which", return_value="/opt/bin/agent-memory")
    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_memory_namespace_delegates_to_the_memory_tool(self, run_script, which, execvp) -> None:
        code, stdout, stderr = self._run(["memory", "archive", "demo", "notes.md", "--oacp-dir", "/tmp/home"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_not_called()
        which.assert_called_once_with("agent-memory")
        execvp.assert_called_once_with(
            "/opt/bin/agent-memory",
            ["agent-memory", "archive", "demo", "notes.md", "--home", "/tmp/home"],
        )

    @mock.patch("oacp.cli.os.execvp")
    @mock.patch("oacp.cli.shutil.which", return_value="/opt/bin/agent-memory")
    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_org_memory_init_delegates_to_the_org_tier_verb(self, run_script, which, execvp) -> None:
        code, _stdout, _stderr = self._run(["org-memory", "init"])
        self.assertEqual(code, 0)
        run_script.assert_not_called()
        execvp.assert_called_once_with("/opt/bin/agent-memory", ["agent-memory", "org", "init"])

    @mock.patch("oacp.cli.os.execvp")
    @mock.patch("oacp.cli.shutil.which", return_value=None)
    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_memory_namespace_without_the_tool_exits_127(self, run_script, which, execvp) -> None:
        code, stdout, stderr = self._run(["memory", "pull"])
        self.assertEqual(code, 127)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr.count("\n"), 1)
        self.assertIn("agent-memory", stderr)
        self.assertIn("pip install agent-memory-cli", stderr)
        run_script.assert_not_called()
        execvp.assert_not_called()

    @mock.patch("oacp.cli.os.execvp")
    @mock.patch("oacp.cli.subprocess.run")
    @mock.patch("oacp.cli.shutil.which", return_value="/opt/bin/agent-memory")
    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_write_event_execs_the_event_verb_when_the_tool_serves_it(self, run_script, which, run, execvp) -> None:
        run.return_value = mock.Mock(returncode=0)
        argv = [
            "--agent", "alice", "--project", "demo", "--type", "decision", "--slug", "api-convention",
            "--body", "Use REST", "--oacp-dir", "/tmp/home", "--json",
        ]
        code, stdout, stderr = self._run(["write-event", *argv])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_not_called()
        which.assert_called_once_with("agent-memory")
        self.assertEqual(run.call_args.args[0], ["/opt/bin/agent-memory", "event", "write", "--help"])
        execvp.assert_called_once_with(
            "/opt/bin/agent-memory",
            [
                "agent-memory", "event", "write",
                "--agent", "alice", "--project", "demo", "--type", "decision", "--slug", "api-convention",
                "--body", "Use REST", "--home", "/tmp/home", "--json",
            ],
        )

    @mock.patch("oacp.cli.os.execvp")
    @mock.patch("oacp.cli.subprocess.run")
    @mock.patch("oacp.cli.shutil.which", return_value=None)
    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_write_event_runs_the_script_without_the_tool(self, run_script, which, run, execvp) -> None:
        argv = ["--agent", "alice", "--project", "demo", "--type", "event", "--slug", "x", "--body", "b"]
        code, stdout, stderr = self._run(["write-event", *argv])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with("write_event.py", argv)
        run.assert_not_called()
        execvp.assert_not_called()

    @mock.patch("oacp.cli.os.execvp")
    @mock.patch("oacp.cli.subprocess.run")
    @mock.patch("oacp.cli.shutil.which", return_value="/opt/bin/agent-memory")
    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_write_event_runs_the_script_when_the_tool_predates_the_verb(self, run_script, which, run, execvp) -> None:
        run.return_value = mock.Mock(returncode=2)  # argparse: invalid choice 'event'
        argv = ["--agent", "alice", "--project", "demo", "--type", "event", "--slug", "x", "--body", "b"]
        code, stdout, stderr = self._run(["write-event", *argv])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with("write_event.py", argv)
        execvp.assert_not_called()

    @mock.patch("oacp.cli.os.execvp")
    @mock.patch("oacp.cli.subprocess.run", side_effect=OSError("exec format error"))
    @mock.patch("oacp.cli.shutil.which", return_value="/opt/bin/agent-memory")
    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_write_event_runs_the_script_when_the_probe_fails(self, run_script, which, run, execvp) -> None:
        argv = ["--agent", "alice", "--project", "demo", "--type", "rule", "--slug", "x", "--body", "b"]
        code, _stdout, stderr = self._run(["write-event", *argv])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with("write_event.py", argv)
        execvp.assert_not_called()

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_session_init(self, run_script) -> None:
        code, stdout, stderr = self._run(
            ["session-init", "--hook", "--project", "demo"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with(
            "codex_session_init.py",
            ["--hook", "--project", "demo"],
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
    def test_dispatches_watch(self, run_script) -> None:
        code, stdout, stderr = self._run(
            ["watch", "--agent", "codex", "--project", "demo", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with(
            "oacp_watch.py", ["--agent", "codex", "--project", "demo", "--json"]
        )

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_setup(self, run_script) -> None:
        code, stdout, stderr = self._run(["setup", "claude", "--project", "demo"])
        self.assertEqual(code, 0)
        run_script.assert_called_once_with(
            "setup_runtime.py", ["claude", "--project", "demo"]
        )

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_autonomy_outcome(self, run_script) -> None:
        code, stdout, stderr = self._run([
            "autonomy-outcome",
            "/tmp/audit.yaml",
            "--decision",
            "approved",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with(
            "record_autonomy_outcome.py",
            ["/tmp/audit.yaml", "--decision", "approved"],
        )

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_dispatches_envelope(self, run_script) -> None:
        code, stdout, stderr = self._run([
            "envelope",
            "compile",
            "/tmp/msg.yaml",
            "--receiver",
            "claude",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with(
            "envelope_compiler.py",
            ["compile", "/tmp/msg.yaml", "--receiver", "claude"],
        )

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_help_for_subcommand(self, run_script) -> None:
        code, stdout, stderr = self._run(["help", "send"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        run_script.assert_called_once_with("send_inbox_message.py", ["--help"])

    @mock.patch("oacp.cli._run_script", return_value=0)
    def test_retention_is_parked(self, run_script) -> None:
        code, stdout, stderr = self._run(["retention", "demo", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("parked", stderr)
        self.assertIn("shared/archive/oacp-retention/retention.py", stderr)
        self.assertIn("Nothing was pruned", stderr)
        run_script.assert_not_called()
        self.assertNotIn("retention", cli.SCRIPT_NAMES)
        self.assertIn("retention", cli.HELP_TEXT)

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
        self.assertIn("watch", stdout)
        self.assertIn("memory", stdout)
        self.assertIn("setup", stdout)
        self.assertIn("autonomy-outcome", stdout)
        self.assertIn("envelope", stdout)
        self.assertIn("key", stdout)
        self.assertIn("verify", stdout)
        self.assertIn("trust", stdout)
        self.assertEqual(cli.SCRIPT_NAMES["trust"], "trust_cli.py")

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
