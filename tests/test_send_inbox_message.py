# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for send_inbox_message.py."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from send_inbox_message import (  # noqa: E402
    FIELD_ORDER,
    build_message_dict,
    generate_filename,
    generate_message_id,
    generate_timestamp,
    infer_current_runtime,
    infer_sender,
    render_yaml,
    resolve_body,
    send_message,
    write_message_files,
    main,
)

from validate_message import validate_message_dict  # noqa: E402


def _write_agent_card(
    root: Path,
    project: str,
    agent: str,
    runtime: str,
    name: Optional[str] = None,
) -> None:
    card_path = root / "projects" / project / "agents" / agent / "agent_card.yaml"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(
        "\n".join(
            [
                'version: "0.2.0"',
                f'name: "{name or agent}"',
                f'runtime: "{runtime}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


class TestGenerateMessageId(unittest.TestCase):
    def test_format(self):
        mid = generate_message_id("claude")
        self.assertTrue(mid.startswith("msg-"))
        parts = mid.split("-")
        # msg-<ts>-<sender>-<rand4>
        self.assertEqual(parts[0], "msg")
        self.assertEqual(len(parts[1]), 14)  # YYYYMMDDHHmmss
        self.assertEqual(parts[2], "claude")
        self.assertEqual(len(parts[3]), 4)

    def test_uniqueness(self):
        ids = {generate_message_id("claude") for _ in range(50)}
        # Random suffix should make IDs unique (extremely unlikely collision)
        self.assertGreater(len(ids), 45)


class TestGenerateTimestamp(unittest.TestCase):
    def test_format(self):
        ts = generate_timestamp()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestGenerateFilename(unittest.TestCase):
    def test_basic(self):
        fn = generate_filename("claude", "task_request")
        self.assertRegex(fn, r"^\d{14}_claude_task_request_[0-9a-f]{4}\.yaml$")

    def test_with_suffix(self):
        fn = generate_filename("codex", "notification", suffix="issue42")
        self.assertRegex(fn, r"^\d{14}_codex_notification_[0-9a-f]{4}_issue42\.yaml$")

    def test_no_suffix(self):
        fn = generate_filename("gemini", "handoff")
        self.assertNotIn("None", fn)
        self.assertTrue(fn.endswith(".yaml"))

    def test_uniqueness(self):
        """Filenames should differ even when generated in the same second."""
        fns = {generate_filename("claude", "task_request") for _ in range(20)}
        self.assertGreater(len(fns), 15)


class TestResolveBody(unittest.TestCase):
    def test_inline_body(self):
        body = resolve_body("Hello world", None)
        self.assertEqual(body, "Hello world")

    def test_body_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("File body content")
            f.flush()
            try:
                body = resolve_body(None, f.name)
                self.assertEqual(body, "File body content")
            finally:
                os.unlink(f.name)

    def test_body_file_overrides_inline(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("From file")
            f.flush()
            try:
                body = resolve_body("From inline", f.name)
                self.assertEqual(body, "From file")
            finally:
                os.unlink(f.name)

    def test_stdin_pipe(self):
        with mock.patch("sys.stdin", io.StringIO("Piped body")):
            with mock.patch("sys.stdin.isatty", return_value=False):
                body = resolve_body(None, "-")
                self.assertEqual(body, "Piped body")

    def test_missing_body_raises(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                resolve_body(None, None)
            self.assertIn("no message body provided", str(ctx.exception))

    def test_missing_file_raises(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_body(None, "/nonexistent/path/body.txt")
        self.assertIn("body file not found", str(ctx.exception))


class TestBuildMessageDict(unittest.TestCase):
    def test_required_fields(self):
        msg = build_message_dict(
            sender="claude",
            recipient="codex",
            msg_type="task_request",
            subject="Test subject",
            body="Test body",
        )
        self.assertIn("id", msg)
        self.assertEqual(msg["from"], "claude")
        self.assertEqual(msg["to"], "codex")
        self.assertEqual(msg["type"], "task_request")
        self.assertEqual(msg["priority"], "P2")
        self.assertIn("created_at_utc", msg)
        self.assertEqual(msg["subject"], "Test subject")
        self.assertEqual(msg["body"], "Test body")

    def test_optional_fields_included(self):
        msg = build_message_dict(
            sender="claude",
            recipient="codex",
            msg_type="task_request",
            subject="Test",
            body="Body",
            related_pr="42",
            related_packet="20260212_test_claude_r1",
            conversation_id="conv-20260212-claude-001",
            parent_message_id="msg-20260212-codex-abcd",
            context_keys="key context here",
        )
        self.assertEqual(msg["related_pr"], "42")
        self.assertEqual(msg["related_packet"], "20260212_test_claude_r1")
        self.assertEqual(msg["conversation_id"], "conv-20260212-claude-001")
        self.assertEqual(msg["parent_message_id"], "msg-20260212-codex-abcd")
        self.assertEqual(msg["context_keys"], "key context here")

    def test_optional_fields_omitted_when_empty(self):
        msg = build_message_dict(
            sender="claude",
            recipient="codex",
            msg_type="notification",
            subject="Notify",
            body="Body text",
        )
        self.assertNotIn("related_pr", msg)
        self.assertNotIn("related_packet", msg)
        self.assertNotIn("conversation_id", msg)
        self.assertNotIn("parent_message_id", msg)
        self.assertNotIn("context_keys", msg)

    def test_auto_generated_id(self):
        msg = build_message_dict(
            sender="gemini", recipient="claude",
            msg_type="question", subject="Q", body="B",
        )
        self.assertTrue(msg["id"].startswith("msg-"))
        self.assertIn("gemini", msg["id"])

    def test_body_trailing_newline_stripped(self):
        msg = build_message_dict(
            sender="claude", recipient="codex",
            msg_type="notification", subject="S", body="Body\n\n",
        )
        self.assertEqual(msg["body"], "Body")

    def test_custom_priority(self):
        msg = build_message_dict(
            sender="claude", recipient="codex",
            msg_type="task_request", subject="S", body="B",
            priority="P0",
        )
        self.assertEqual(msg["priority"], "P0")


class TestRenderYaml(unittest.TestCase):
    def _make_msg(self, **overrides):
        defaults = {
            "id": "msg-20260212120000-claude-ab12",
            "from": "claude",
            "to": "codex",
            "type": "task_request",
            "priority": "P2",
            "created_at_utc": "2026-02-12T12:00:00Z",
            "subject": "Test subject",
            "body": "Single line body",
        }
        defaults.update(overrides)
        return defaults

    def test_field_order(self):
        msg = self._make_msg()
        yaml_str = render_yaml(msg)
        lines = yaml_str.strip().splitlines()
        keys = [line.split(":")[0] for line in lines if ":" in line and not line.startswith("  ")]
        # Verify keys appear in FIELD_ORDER order
        order_idx = [FIELD_ORDER.index(k) for k in keys]
        self.assertEqual(order_idx, sorted(order_idx))

    def test_block_scalar_multiline_body(self):
        msg = self._make_msg(body="Line 1\nLine 2\nLine 3")
        yaml_str = render_yaml(msg)
        self.assertIn("body: |", yaml_str)
        self.assertIn("  Line 1", yaml_str)
        self.assertIn("  Line 2", yaml_str)

    def test_single_line_body_no_block_scalar(self):
        msg = self._make_msg(body="Just one line")
        yaml_str = render_yaml(msg)
        self.assertNotIn("body: |", yaml_str)
        self.assertIn("body:", yaml_str)

    def test_omitted_optional_fields(self):
        msg = self._make_msg()
        yaml_str = render_yaml(msg)
        self.assertNotIn("related_pr:", yaml_str)
        self.assertNotIn("conversation_id:", yaml_str)

    def test_roundtrip_validates(self):
        """Rendered YAML should pass validation when parsed back."""
        msg = self._make_msg(
            related_pr="42",
            context_keys="Key context\nSecond line",
        )
        yaml_str = render_yaml(msg)
        # Parse back using validate_message's parser
        from validate_message import _parse_simple_yaml
        parsed = _parse_simple_yaml(yaml_str)
        errors = validate_message_dict(parsed)
        self.assertEqual(errors, [], f"Validation errors: {errors}")

    def test_special_characters_in_subject(self):
        msg = self._make_msg(subject="Issue #24: fix auth & add tests")
        yaml_str = render_yaml(msg)
        self.assertIn("subject:", yaml_str)

    def test_trailing_newline(self):
        msg = self._make_msg()
        yaml_str = render_yaml(msg)
        self.assertTrue(yaml_str.endswith("\n"))
        self.assertFalse(yaml_str.endswith("\n\n"))

    def test_newline_in_subject_escaped(self):
        """P0 fix: newlines in non-block-scalar fields must be escaped."""
        msg = self._make_msg(subject="Line1\nLine2")
        yaml_str = render_yaml(msg)
        # Should be on one line with escaped newline
        self.assertIn("subject:", yaml_str)
        self.assertNotIn("subject: |", yaml_str)
        # Roundtrip should still parse
        from validate_message import _parse_simple_yaml
        parsed = _parse_simple_yaml(yaml_str)
        self.assertIn("Line1", parsed["subject"])

    def test_yaml_reserved_words_quoted(self):
        """P2 fix: YAML reserved bare words should be quoted."""
        msg = self._make_msg(subject="true")
        yaml_str = render_yaml(msg)
        self.assertIn('subject: "true"', yaml_str)

    def test_yaml_null_quoted(self):
        msg = self._make_msg(subject="null")
        yaml_str = render_yaml(msg)
        self.assertIn('subject: "null"', yaml_str)


class TestWriteMessageFiles(unittest.TestCase):
    def test_creates_inbox_and_outbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            yaml_content = "id: test\n"
            inbox_path, outbox_path = write_message_files(
                project_dir=project_dir,
                sender="claude",
                recipient="codex",
                msg_type="task_request",
                yaml_content=yaml_content,
            )
            self.assertTrue(inbox_path.is_file())
            self.assertTrue(outbox_path.is_file())
            self.assertEqual(inbox_path.read_text(), yaml_content)
            self.assertEqual(outbox_path.read_text(), yaml_content)

    def test_directory_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            inbox_path, outbox_path = write_message_files(
                project_dir=project_dir,
                sender="gemini",
                recipient="claude",
                msg_type="notification",
                yaml_content="id: test\n",
            )
            self.assertTrue((project_dir / "agents" / "claude" / "inbox").is_dir())
            self.assertTrue((project_dir / "agents" / "gemini" / "outbox").is_dir())

    def test_file_in_correct_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            inbox_path, outbox_path = write_message_files(
                project_dir=project_dir,
                sender="claude",
                recipient="codex",
                msg_type="handoff",
                yaml_content="id: test\n",
            )
            self.assertEqual(inbox_path.parent.name, "inbox")
            self.assertEqual(inbox_path.parent.parent.name, "codex")
            self.assertEqual(outbox_path.parent.name, "outbox")
            self.assertEqual(outbox_path.parent.parent.name, "claude")

    def test_suffix_in_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            inbox_path, _ = write_message_files(
                project_dir=project_dir,
                sender="claude",
                recipient="codex",
                msg_type="task_request",
                yaml_content="id: test\n",
                suffix="issue24",
            )
            self.assertIn("issue24", inbox_path.name)


class TestSendMessage(unittest.TestCase):
    def test_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="task_request",
                subject="Issue #24",
                body="Please implement the feature",
                oacp_dir=hub_dir,
            )
            self.assertIn("message_id", report)
            self.assertIn("inbox_path", report)
            self.assertIn("outbox_path", report)
            self.assertFalse(report["dry_run"])
            # Verify files exist
            self.assertTrue(Path(report["inbox_path"]).is_file())
            self.assertTrue(Path(report["outbox_path"]).is_file())

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="notification",
                subject="FYI",
                body="Just letting you know",
                oacp_dir=hub_dir,
                dry_run=True,
            )
            self.assertTrue(report["dry_run"])
            self.assertIn("yaml", report)
            self.assertNotIn("inbox_path", report)
            # No files should be created
            project_dir = hub_dir / "projects" / "test-project" / "agents"
            self.assertFalse(project_dir.exists())

    def test_validation_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender="invalid sender!",
                    recipient="codex",
                    msg_type="task_request",
                    subject="Test",
                    body="Body",
                    oacp_dir=hub_dir,
                )
            self.assertIn("from", str(ctx.exception))

    def test_invalid_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            with self.assertRaises(ValueError):
                send_message(
                    project="test-project",
                    sender="claude",
                    recipient="codex",
                    msg_type="INVALID",
                    subject="Test",
                    body="Body",
                    oacp_dir=hub_dir,
                )

    def test_handoff_complete_body_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            body = """\
issue: "#77"
pr: "84"
branch: "codex/issue-77-handoff-protocol"
tests_run: "make test"
next_owner: "claude"
"""
            report = send_message(
                project="test-project",
                sender="codex",
                recipient="claude",
                msg_type="handoff_complete",
                subject="Handoff complete: #77",
                body=body,
                oacp_dir=hub_dir,
                dry_run=True,
            )
            self.assertEqual(report["type"], "handoff_complete")

    def test_handoff_partial_body_rejected(self):
        """Partial structured handoff body (missing required fields) must fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            partial_body = """\
source_agent: "codex"
target_agent: "claude"
intent: "handoff"
"""
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender="codex",
                    recipient="claude",
                    msg_type="handoff",
                    subject="Handoff packet",
                    body=partial_body,
                    oacp_dir=hub_dir,
                    dry_run=True,
                )
            self.assertIn("artifacts_to_review", str(ctx.exception))

    def test_handoff_body_validation_error(self):
        """Handoff body with invalid agent name pattern should still fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            invalid_body = """\
source_agent: "_invalid"
target_agent: "claude"
intent: "handoff"
"""
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender="codex",
                    recipient="claude",
                    msg_type="handoff",
                    subject="Handoff packet",
                    body=invalid_body,
                    oacp_dir=hub_dir,
                    dry_run=True,
                )
            self.assertIn("handoff body", str(ctx.exception))

    def test_threading_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="task_request",
                subject="Threaded message",
                body="Follow-up",
                conversation_id="conv-20260212-claude-001",
                parent_message_id="msg-20260212120000-codex-ab12",
                oacp_dir=hub_dir,
            )
            # Verify the file contains threading fields
            content = Path(report["inbox_path"]).read_text()
            self.assertIn("conversation_id:", content)
            self.assertIn("parent_message_id:", content)

    def test_written_file_passes_validation(self):
        """End-to-end: written file should pass validate_message_dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="task_request",
                subject="E2E test",
                body="End to end body",
                related_pr="99",
                oacp_dir=hub_dir,
            )
            from validate_message import _parse_simple_yaml
            content = Path(report["inbox_path"]).read_text()
            parsed = _parse_simple_yaml(content)
            errors = validate_message_dict(parsed)
            self.assertEqual(errors, [], f"Validation errors: {errors}")

    def test_suffix_path_traversal_rejected(self):
        """P1 fix: suffix with path separators must be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender="claude",
                    recipient="codex",
                    msg_type="task_request",
                    subject="Test",
                    body="Body",
                    suffix="../../etc",
                    oacp_dir=hub_dir,
                )
            self.assertIn("suffix", str(ctx.exception))

    def test_dotdot_agent_name_rejected(self):
        """P1 fix: '..' as agent name must be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender="claude",
                    recipient="..",
                    msg_type="task_request",
                    subject="Test",
                    body="Body",
                    oacp_dir=hub_dir,
                )
            self.assertIn("path component", str(ctx.exception))

    def test_dot_agent_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender=".",
                    recipient="codex",
                    msg_type="task_request",
                    subject="Test",
                    body="Body",
                    oacp_dir=hub_dir,
                )
            self.assertIn("path component", str(ctx.exception))


class TestMainCli(unittest.TestCase):
    def _run_main(self, args, stdin_text=None):
        """Run main() with given args, capturing stdout/stderr."""
        old_argv = sys.argv
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            sys.argv = ["send_inbox_message.py"] + args
            if stdin_text is not None:
                sys.stdin = io.StringIO(stdin_text)
                sys.stdin.isatty = lambda: False
            code = main()
            stdout = sys.stdout.getvalue()
            stderr = sys.stderr.getvalue()
        finally:
            sys.argv = old_argv
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            sys.stdin = sys.__stdin__
        return code, stdout, stderr

    def test_dry_run_prints_yaml(self):
        code, stdout, stderr = self._run_main([
            "test-project",
            "--from", "claude", "--to", "codex",
            "--type", "task_request",
            "--subject", "Test dry run",
            "--body", "Dry run body",
            "--dry-run",
        ])
        self.assertEqual(code, 0)
        self.assertIn("id:", stdout)
        self.assertIn("from: claude", stdout)
        self.assertIn("to: codex", stdout)
        self.assertIn("type: task_request", stdout)

    def test_json_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code, stdout, stderr = self._run_main([
                "test-project",
                "--from", "claude", "--to", "codex",
                "--type", "notification",
                "--subject", "JSON test",
                "--body", "JSON body",
                "--oacp-dir", tmpdir,
                "--json",
            ])
            self.assertEqual(code, 0)
            data = json.loads(stdout)
            self.assertIn("message_id", data)
            self.assertEqual(data["from"], "claude")
            self.assertEqual(data["to"], "codex")

    def test_quiet_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code, stdout, stderr = self._run_main([
                "test-project",
                "--from", "claude", "--to", "codex",
                "--type", "task_request",
                "--subject", "Quiet test",
                "--body", "Body",
                "--oacp-dir", tmpdir,
                "--quiet",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")

    def test_missing_body_exits_2(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            code, stdout, stderr = self._run_main([
                "test-project",
                "--from", "claude", "--to", "codex",
                "--type", "task_request",
                "--subject", "No body",
            ])
            self.assertEqual(code, 2)
            self.assertIn("no message body", stderr)

    def test_body_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Body from file")
            f.flush()
            try:
                code, stdout, stderr = self._run_main([
                    "test-project",
                    "--from", "claude", "--to", "codex",
                    "--type", "task_request",
                    "--subject", "File body",
                    "--body-file", f.name,
                    "--dry-run",
                ])
                self.assertEqual(code, 0)
                self.assertIn("Body from file", stdout)
            finally:
                os.unlink(f.name)

    def test_json_error_output(self):
        """Validation errors in --json mode should produce JSON error."""
        code, stdout, stderr = self._run_main([
            "test-project",
            "--from", "bad sender!", "--to", "codex",
            "--type", "task_request",
            "--subject", "Test",
            "--body", "Body",
            "--dry-run",
            "--json",
        ])
        self.assertEqual(code, 1)
        data = json.loads(stdout)
        self.assertIn("error", data)

    def test_context_keys_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Key context from file\nSecond line")
            f.flush()
            try:
                code, stdout, stderr = self._run_main([
                    "test-project",
                    "--from", "claude", "--to", "codex",
                    "--type", "task_request",
                    "--subject", "Context test",
                    "--body", "Body",
                    "--context-keys-file", f.name,
                    "--dry-run",
                ])
                self.assertEqual(code, 0)
                self.assertIn("context_keys:", stdout)
                self.assertIn("Key context from file", stdout)
            finally:
                os.unlink(f.name)

    def test_piped_stdin_body(self):
        code, stdout, stderr = self._run_main(
            [
                "test-project",
                "--from", "claude", "--to", "codex",
                "--type", "task_request",
                "--subject", "Piped",
                "--body-file", "-",
                "--dry-run",
            ],
            stdin_text="Piped stdin body",
        )
        self.assertEqual(code, 0)
        self.assertIn("Piped stdin body", stdout)

    def test_sender_inferred_from_oacp_agent_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"OACP_AGENT": "claude"}, clear=False):
                code, stdout, stderr = self._run_main([
                    "test-project",
                    "--to", "codex",
                    "--type", "task_request",
                    "--subject", "Env sender",
                    "--body", "Body",
                    "--oacp-dir", tmpdir,
                    "--dry-run",
                ])
        self.assertEqual(code, 0)
        self.assertIn("from: claude", stdout)

    def test_sender_inferred_from_agent_name_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"AGENT_NAME": "iris"}, clear=False):
                code, stdout, stderr = self._run_main([
                    "test-project",
                    "--to", "codex",
                    "--type", "task_request",
                    "--subject", "Agent name sender",
                    "--body", "Body",
                    "--oacp-dir", tmpdir,
                    "--dry-run",
                ])
        self.assertEqual(code, 0)
        self.assertIn("from: iris", stdout)

    def test_explicit_sender_wins_over_oacp_agent_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"OACP_AGENT": "claude"}, clear=False):
                code, stdout, stderr = self._run_main([
                    "test-project",
                    "--from", "codex",
                    "--to", "claude",
                    "--type", "notification",
                    "--subject", "Explicit wins",
                    "--body", "Body",
                    "--oacp-dir", tmpdir,
                    "--dry-run",
                ])
        self.assertEqual(code, 0)
        self.assertIn("from: codex", stdout)

    def test_sender_inferred_from_single_matching_agent_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_agent_card(root, "test-project", "iris", "codex")
            _write_agent_card(root, "test-project", "claude", "claude")
            with mock.patch.dict(os.environ, {"OACP_RUNTIME": "codex"}, clear=False):
                code, stdout, stderr = self._run_main([
                    "test-project",
                    "--to", "claude",
                    "--type", "question",
                    "--subject", "Card sender",
                    "--body", "Body",
                    "--oacp-dir", tmpdir,
                    "--dry-run",
                ])
        self.assertEqual(code, 0)
        self.assertIn("from: iris", stdout)

    def test_missing_sender_inference_exits_2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {}, clear=True):
                code, stdout, stderr = self._run_main([
                    "test-project",
                    "--to", "codex",
                    "--type", "task_request",
                    "--subject", "No sender",
                    "--body", "Body",
                    "--oacp-dir", tmpdir,
                ])
        self.assertEqual(code, 2)
        self.assertIn("Cannot infer sender", stderr)

    def test_missing_sender_inference_json_error_exits_2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {}, clear=True):
                code, stdout, stderr = self._run_main([
                    "test-project",
                    "--to", "codex",
                    "--type", "task_request",
                    "--subject", "No sender",
                    "--body", "Body",
                    "--oacp-dir", tmpdir,
                    "--json",
                ])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        data = json.loads(stdout)
        self.assertIn("Cannot infer sender", data["error"])


class TestSenderInference(unittest.TestCase):
    def test_infer_current_runtime_prefers_oacp_runtime(self):
        env = {"OACP_RUNTIME": "claude", "CODEX_SHELL": "1"}
        self.assertEqual(infer_current_runtime(env), "claude")

    def test_infer_sender_from_single_matching_agent_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_agent_card(root, "demo", "iris", "codex")
            _write_agent_card(root, "demo", "gemini", "gemini")
            sender = infer_sender(
                "demo",
                oacp_dir=root,
                env={"OACP_RUNTIME": "codex"},
            )
        self.assertEqual(sender, "iris")

    def test_infer_sender_from_agent_name_env(self):
        sender = infer_sender(
            "demo",
            oacp_dir=Path("/unused"),
            env={"AGENT_NAME": "iris"},
        )
        self.assertEqual(sender, "iris")

    def test_infer_sender_prefers_oacp_agent_over_agent_name(self):
        sender = infer_sender(
            "demo",
            oacp_dir=Path("/unused"),
            env={"OACP_AGENT": "codex", "AGENT_NAME": "iris"},
        )
        self.assertEqual(sender, "codex")

    def test_infer_sender_rejects_ambiguous_matching_agent_cards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_agent_card(root, "demo", "iris", "codex")
            _write_agent_card(root, "demo", "codex2", "codex")
            with self.assertRaises(ValueError) as ctx:
                infer_sender(
                    "demo",
                    oacp_dir=root,
                    env={"OACP_RUNTIME": "codex"},
                )
        self.assertIn("multiple agent cards match", str(ctx.exception))


class TestBroadcast(unittest.TestCase):
    def test_broadcast_to_two_recipients(self):
        """Broadcast: message delivered to both inboxes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex,gemini",
                msg_type="notification",
                subject="Broadcast test",
                body="Hello everyone",
                oacp_dir=hub_dir,
            )
            self.assertIn("inbox_paths", report)
            self.assertEqual(len(report["inbox_paths"]), 2)
            for p in report["inbox_paths"]:
                self.assertTrue(Path(p).is_file())
            # Verify different directories
            parents = {Path(p).parent.parent.name for p in report["inbox_paths"]}
            self.assertEqual(parents, {"codex", "gemini"})

    def test_broadcast_outbox_has_full_list(self):
        """Outbox copy should have to: [codex, gemini]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex,gemini",
                msg_type="notification",
                subject="Outbox test",
                body="Hello all",
                oacp_dir=hub_dir,
            )
            outbox_content = Path(report["outbox_path"]).read_text()
            self.assertIn("codex", outbox_content)
            self.assertIn("gemini", outbox_content)
            # Should be a list format
            self.assertIn("[", outbox_content)

    def test_broadcast_max_recipients(self):
        """More than 10 recipients should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            recipients = ",".join(f"agent{i}" for i in range(11))
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender="claude",
                    recipient=recipients,
                    msg_type="notification",
                    subject="Too many",
                    body="Body",
                    oacp_dir=hub_dir,
                )
            self.assertIn("exceeds maximum", str(ctx.exception))

    def test_broadcast_no_self_send(self):
        """Sender in to list should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender="claude",
                    recipient="claude,codex",
                    msg_type="notification",
                    subject="Self send",
                    body="Body",
                    oacp_dir=hub_dir,
                )
            self.assertIn("must not include the sender", str(ctx.exception))

    def test_broadcast_handoff_rejected(self):
        """type:handoff + broadcast should error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                send_message(
                    project="test-project",
                    sender="claude",
                    recipient="codex,gemini",
                    msg_type="handoff",
                    subject="Handoff broadcast",
                    body="source_agent: claude\ntarget_agent: codex\nintent: test\n"
                         "artifacts_to_review:\n  - file.py\ndefinition_of_done:\n  - done\n"
                         "context_bundle.files_touched:\n  - f\ncontext_bundle.decisions_made:\n  - d\n"
                         "context_bundle.blockers_hit:\n  - b\ncontext_bundle.suggested_next_steps:\n  - s",
                    oacp_dir=hub_dir,
                )
            self.assertIn("does not support broadcast", str(ctx.exception))


class TestInReplyTo(unittest.TestCase):
    def test_in_reply_to_inherits_conversation(self):
        """--in-reply-to should find parent and copy conversation_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            project_dir = hub_dir / "projects" / "test-project"
            # Create a parent message in sender's outbox
            outbox_dir = project_dir / "agents" / "claude" / "outbox"
            outbox_dir.mkdir(parents=True)
            parent_yaml = (
                'id: msg-20260213-codex-parent\n'
                'from: codex\n'
                'to: claude\n'
                'type: question\n'
                'priority: P2\n'
                'created_at_utc: 2026-02-13T10:00:00Z\n'
                'conversation_id: conv-20260213-codex-001\n'
                'subject: Original question\n'
                'body: What about X?\n'
            )
            # Also put it in claude's inbox (where claude would have received it)
            inbox_dir = project_dir / "agents" / "claude" / "inbox"
            inbox_dir.mkdir(parents=True)
            (inbox_dir / "parent.yaml").write_text(parent_yaml)

            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="question",
                subject="Reply",
                body="Here is my answer",
                oacp_dir=hub_dir,
                in_reply_to="msg-20260213-codex-parent",
            )
            content = Path(report["inbox_path"]).read_text()
            self.assertIn("conv-20260213-codex-001", content)
            self.assertIn("msg-20260213-codex-parent", content)

    def test_in_reply_to_parent_not_found(self):
        """When parent not found, should still succeed with a new conversation_id and a warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="question",
                subject="Reply to missing",
                body="Body",
                oacp_dir=hub_dir,
                in_reply_to="msg-nonexistent-id",
            )
            self.assertIn("warnings", report)
            self.assertTrue(any("not found" in w for w in report["warnings"]))
            # Should still have generated a conversation_id
            content = Path(report["inbox_path"]).read_text()
            self.assertIn("conversation_id:", content)


class TestExpires(unittest.TestCase):
    def test_expires_1h(self):
        """--expires 1h should set expires_at ~1 hour from now."""
        from send_inbox_message import parse_duration_to_expires
        import datetime as _dt

        base = _dt.datetime(2026, 2, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
        result = parse_duration_to_expires("1h", base)
        self.assertEqual(result, "2026-02-13T13:00:00Z")

    def test_expires_2d(self):
        """--expires 2d should set expires_at ~2 days from now."""
        from send_inbox_message import parse_duration_to_expires
        import datetime as _dt

        base = _dt.datetime(2026, 2, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
        result = parse_duration_to_expires("2d", base)
        self.assertEqual(result, "2026-02-15T12:00:00Z")

    def test_expires_30m(self):
        from send_inbox_message import parse_duration_to_expires
        import datetime as _dt

        base = _dt.datetime(2026, 2, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
        result = parse_duration_to_expires("30m", base)
        self.assertEqual(result, "2026-02-13T12:30:00Z")

    def test_expires_invalid(self):
        """Invalid --expires should raise ValueError."""
        from send_inbox_message import parse_duration_to_expires

        with self.assertRaises(ValueError):
            parse_duration_to_expires("abc")

    def test_expires_field_in_message(self):
        """expires_at should appear in the rendered YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="notification",
                subject="Expiring message",
                body="Body",
                oacp_dir=hub_dir,
                expires_at="2026-02-14T12:00:00Z",
                dry_run=True,
            )
            self.assertIn("expires_at:", report["yaml"])


class TestChannel(unittest.TestCase):
    def test_channel_set(self):
        """--channel should appear in the YAML output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="notification",
                subject="Review discussion",
                body="Body",
                oacp_dir=hub_dir,
                channel="review",
                dry_run=True,
            )
            self.assertIn("channel: review", report["yaml"])


class TestBackwardCompatSingleTo(unittest.TestCase):
    def test_backward_compat_single_to(self):
        """Single --to should still work as before."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_dir = Path(tmpdir)
            report = send_message(
                project="test-project",
                sender="claude",
                recipient="codex",
                msg_type="task_request",
                subject="Single recipient",
                body="Body",
                oacp_dir=hub_dir,
            )
            self.assertEqual(report["to"], "codex")
            self.assertIn("inbox_path", report)
            self.assertNotIn("inbox_paths", report)


if __name__ == "__main__":
    unittest.main()
