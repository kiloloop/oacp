# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/setup_runtime.py."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from add_agent import (  # noqa: E402
    CLAUDE_SUPPORTED_MESSAGE_TYPES,
    CODEX_SUPPORTED_MESSAGE_TYPES,
)
from setup_runtime import setup_runtime  # noqa: E402


class TestSetupRuntime(unittest.TestCase):
    def test_claude_creates_agent_file_and_skills_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            result = setup_runtime(
                "claude", repo_dir=repo_dir, project_name="myproj"
            )

            agent_file = repo_dir / ".claude" / "agents" / "myproj.md"
            pull_hook = repo_dir / ".claude" / "hooks" / "oacp-memory-pull.sh"
            settings_file = repo_dir / ".claude" / "settings.json"
            self.assertTrue(agent_file.is_file())
            agent_content = agent_file.read_text(encoding="utf-8")
            project_files = (
                "project_facts.md", "decision_log.md", "open_threads.md", "known_debt.md"
            )
            positions = [agent_content.index(name) for name in project_files]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("org memory on demand", agent_content)
            self.assertTrue((repo_dir / ".claude" / "skills").is_dir())
            self.assertTrue(pull_hook.is_file())
            self.assertFalse(
                (repo_dir / ".claude" / "hooks" / "oacp-memory-push.sh").exists()
            )
            self.assertTrue(settings_file.is_file())
            pull_content = pull_hook.read_text(encoding="utf-8")
            self.assertIn("Claude hook event: SessionStart", pull_content)
            self.assertIn("oacp memory pull", pull_content)
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertIn("SessionStart", settings["hooks"])
            self.assertNotIn("SessionEnd", settings["hooks"])
            self.assertIn("PreToolUse", settings["hooks"])
            envelope_entry = settings["hooks"]["PreToolUse"][0]
            self.assertEqual(envelope_entry["matcher"], "Bash|Edit|Write|NotebookEdit")
            self.assertEqual(
                envelope_entry["hooks"][0]["command"], "oacp-envelope-hook"
            )
            self.assertIn(".claude/agents/myproj.md", result["created_files"])
            self.assertIn(".claude/skills/", result["created_files"])
            self.assertIn(".claude/hooks/oacp-memory-pull.sh", result["created_files"])
            self.assertIn(".claude/settings.json", result["created_files"])

    def test_claude_envelope_hook_registration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            setup_runtime("claude", repo_dir=repo_dir, project_name="myproj")
            setup_runtime("claude", repo_dir=repo_dir, project_name="myproj")

            settings_file = repo_dir / ".claude" / "settings.json"
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            envelope_entries = [
                entry
                for entry in settings["hooks"]["PreToolUse"]
                for hook in entry.get("hooks", [])
                if hook.get("command") == "oacp-envelope-hook"
            ]
            self.assertEqual(len(envelope_entries), 1)

    def test_codex_creates_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            oacp_root = repo_dir / "oacp-home"
            result = setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )

            agents_md = repo_dir / "AGENTS.md"
            hooks_file = repo_dir / ".codex" / "hooks.json"
            self.assertTrue(agents_md.is_file())
            self.assertTrue(hooks_file.is_file())
            content = agents_md.read_text(encoding="utf-8")
            self.assertIn("OACP", content)
            self.assertIn("oacp send", content)
            self.assertIn("oacp session-init --pull-memory", content)
            self.assertIn("project memory files", content)
            self.assertIn("Org memory is retrieved on demand", content)
            hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
            entry = hooks["hooks"]["SessionStart"][0]
            self.assertEqual(entry["matcher"], "^startup$")
            self.assertEqual(len(entry["hooks"]), 1)
            handler = entry["hooks"][0]
            self.assertEqual(handler["type"], "command")
            self.assertIn("oacp session-init --hook --pull-memory", handler["command"])
            self.assertIn("--project demo", handler["command"])
            self.assertIn(f"--hub-dir {oacp_root}", handler["command"])
            self.assertEqual(handler["additionalContextLimit"], 2500)
            self.assertEqual(handler["timeout"], 60)
            self.assertIn("AGENTS.md", result["created_files"])
            self.assertIn(".codex/hooks.json", result["created_files"])

    def test_claude_setup_refreshes_only_missing_agent_card_message_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "repo"
            oacp_root = root / "oacp-home"
            card_path = (
                oacp_root
                / "projects"
                / "demo"
                / "agents"
                / "claude"
                / "agent_card.yaml"
            )
            card_path.parent.mkdir(parents=True)
            card_path.write_text(
                """# preserve this comment
version: "0.2.0"
name: claude
runtime: claude
protocol:
  inbox_path: agents/claude/inbox/
  outbox_path: agents/claude/outbox/
  supported_message_types:
    - task_request
    - review_request
""",
                encoding="utf-8",
            )

            first = setup_runtime(
                "claude",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )
            second = setup_runtime(
                "claude",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )

            raw = card_path.read_text(encoding="utf-8")
            card = yaml.safe_load(raw)
            self.assertIn("# preserve this comment", raw)
            message_types = card["protocol"]["supported_message_types"]
            self.assertEqual(set(message_types), set(CLAUDE_SUPPORTED_MESSAGE_TYPES))
            self.assertEqual(len(message_types), len(CLAUDE_SUPPORTED_MESSAGE_TYPES))
            self.assertIn(
                "agents/claude/agent_card.yaml", first["project_created_files"]
            )
            self.assertIn(
                "agents/claude/agent_card.yaml", second["project_skipped_files"]
            )

    def test_claude_setup_without_project_leaves_agent_cards_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"

            result = setup_runtime("claude", repo_dir=repo_dir)

            self.assertEqual(result["project_created_files"], [])
            self.assertEqual(result["project_skipped_files"], [])

    def test_codex_setup_refreshes_only_missing_agent_card_message_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "repo"
            oacp_root = root / "oacp-home"
            card_path = (
                oacp_root / "projects" / "demo" / "agents" / "codex" / "agent_card.yaml"
            )
            card_path.parent.mkdir(parents=True)
            card_path.write_text(
                """# preserve this comment
version: "0.2.0"
name: codex
runtime: codex
protocol:
  inbox_path: agents/codex/inbox/
  outbox_path: agents/codex/outbox/
  supported_message_types:
    - task_request
""",
                encoding="utf-8",
            )

            first = setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )
            second = setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )

            raw = card_path.read_text(encoding="utf-8")
            card = yaml.safe_load(raw)
            self.assertIn("# preserve this comment", raw)
            message_types = card["protocol"]["supported_message_types"]
            self.assertEqual(set(message_types), set(CODEX_SUPPORTED_MESSAGE_TYPES))
            self.assertEqual(len(message_types), len(CODEX_SUPPORTED_MESSAGE_TYPES))
            self.assertIn(
                "agents/codex/agent_card.yaml", first["project_created_files"]
            )
            self.assertIn(
                "agents/codex/agent_card.yaml", second["project_skipped_files"]
            )

    def test_agent_card_refresh_handles_valid_formatting_variants(self) -> None:
        variants = {
            "indentationless-sequence": (
                'version: "0.2.0"\n'
                "name: {agent}\n"
                "runtime: {agent}\n"
                "protocol:\n"
                "  inbox_path: agents/{agent}/inbox/\n"
                "  outbox_path: agents/{agent}/outbox/\n"
                "  supported_message_types:\n"
                "  - task_request\n"
                "  - review_request\n"
            ),
            "no-final-newline": (
                'version: "0.2.0"\n'
                "name: {agent}\n"
                "runtime: {agent}\n"
                "protocol:\n"
                "  inbox_path: agents/{agent}/inbox/\n"
                "  outbox_path: agents/{agent}/outbox/\n"
                "  supported_message_types:\n"
                "    - task_request\n"
                "    - review_request"
            ),
        }
        required = {
            "claude": CLAUDE_SUPPORTED_MESSAGE_TYPES,
            "codex": CODEX_SUPPORTED_MESSAGE_TYPES,
        }
        for runtime, required_types in required.items():
            for label, template in variants.items():
                with self.subTest(runtime=runtime, variant=label):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        repo_dir = root / "repo"
                        oacp_root = root / "oacp-home"
                        card_path = (
                            oacp_root
                            / "projects"
                            / "demo"
                            / "agents"
                            / runtime
                            / "agent_card.yaml"
                        )
                        card_path.parent.mkdir(parents=True)
                        card_path.write_text(
                            template.format(agent=runtime), encoding="utf-8"
                        )

                        setup_runtime(
                            runtime,
                            repo_dir=repo_dir,
                            project_name="demo",
                            oacp_root=oacp_root,
                        )
                        after_first = card_path.read_text(encoding="utf-8")
                        setup_runtime(
                            runtime,
                            repo_dir=repo_dir,
                            project_name="demo",
                            oacp_root=oacp_root,
                        )
                        after_second = card_path.read_text(encoding="utf-8")

                        existing = ["task_request", "review_request"]
                        expected = existing + [
                            item
                            for item in required_types
                            if item not in existing
                        ]
                        card = yaml.safe_load(after_first)
                        self.assertEqual(
                            card["protocol"]["supported_message_types"], expected
                        )
                        self.assertEqual(after_second, after_first)

    def test_gemini_creates_rules_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            result = setup_runtime("gemini", repo_dir=repo_dir)

            rules_file = repo_dir / ".agent" / "rules" / "oacp.md"
            self.assertTrue(rules_file.is_file())
            content = rules_file.read_text(encoding="utf-8")
            self.assertIn("OACP", content)
            self.assertIn("oacp send", content)
            self.assertIn(".agent/rules/oacp.md", result["created_files"])

    def test_cursor_creates_todo_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            result = setup_runtime("cursor", repo_dir=repo_dir)

            todo_file = repo_dir / ".cursor" / "rules" / "oacp.todo.mdc"
            self.assertTrue(todo_file.is_file())
            content = todo_file.read_text(encoding="utf-8")
            self.assertIn("TODO", content)
            self.assertIn("check-inbox rules and memory hooks", content)
            self.assertIn("OACP_RUNTIME=cursor", content)
            self.assertIn(".cursor/rules/oacp.todo.mdc", result["created_files"])

    def test_cursor_setup_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            setup_runtime("cursor", repo_dir=repo_dir)

            todo_file = repo_dir / ".cursor" / "rules" / "oacp.todo.mdc"
            todo_file.write_text("custom", encoding="utf-8")

            result = setup_runtime("cursor", repo_dir=repo_dir)
            self.assertEqual(result["created_files"], [])
            self.assertIn(".cursor/rules/oacp.todo.mdc", result["skipped_files"])
            self.assertEqual(todo_file.read_text(encoding="utf-8"), "custom")

    def test_cursor_setup_provisions_project_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "repo"
            oacp_root = root / "oacp"
            repo_dir.mkdir()
            (oacp_root / "projects" / "demo").mkdir(parents=True)

            result = setup_runtime(
                "cursor",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )

            agent_dir = oacp_root / "projects" / "demo" / "agents" / "cursor"
            self.assertTrue((agent_dir / "inbox").is_dir())
            self.assertTrue((agent_dir / "outbox").is_dir())
            self.assertTrue((agent_dir / "dead_letter").is_dir())
            self.assertTrue((agent_dir / "audit" / "autonomy_decisions").is_dir())
            self.assertTrue((agent_dir / "config.yaml").is_file())
            self.assertTrue((agent_dir / "status.yaml").is_file())
            self.assertTrue((agent_dir / "agent_card.yaml").is_file())
            self.assertIn("agents/cursor/status.yaml", result["project_created_files"])
            status = (agent_dir / "status.yaml").read_text(encoding="utf-8")
            self.assertIn("runtime: cursor", status)
            card = (agent_dir / "agent_card.yaml").read_text(encoding="utf-8")
            self.assertIn('runtime: "cursor"', card)

            result2 = setup_runtime(
                "cursor",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )
            self.assertIn(".cursor/rules/oacp.todo.mdc", result2["skipped_files"])
            self.assertIn("agents/cursor/status.yaml", result2["project_skipped_files"])
            self.assertIn("agents/cursor/agent_card.yaml", result2["project_skipped_files"])

    def test_cursor_setup_missing_project_does_not_write_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "repo"
            oacp_root = root / "oacp"
            repo_dir.mkdir()

            with self.assertRaises(ValueError) as ctx:
                setup_runtime(
                    "cursor",
                    repo_dir=repo_dir,
                    project_name="missing",
                    oacp_root=oacp_root,
                )

            self.assertIn("oacp init missing", str(ctx.exception))
            self.assertFalse((repo_dir / ".cursor" / "rules" / "oacp.todo.mdc").exists())

    def test_does_not_overwrite_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            # First run
            setup_runtime("codex", repo_dir=repo_dir)

            # Write custom content
            (repo_dir / "AGENTS.md").write_text("custom", encoding="utf-8")

            # Second run — should skip
            result = setup_runtime("codex", repo_dir=repo_dir)
            self.assertEqual(len(result["created_files"]), 0)
            self.assertIn("AGENTS.md", result["skipped_files"])
            self.assertEqual(
                (repo_dir / "AGENTS.md").read_text(encoding="utf-8"), "custom"
            )

    def test_rejects_unknown_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                setup_runtime("unknown", repo_dir=repo_dir)
            self.assertIn("Invalid runtime", str(ctx.exception))

    def test_claude_detects_project_from_workspace_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            # Write a bare workspace.json (no .oacp symlink)
            import json
            (repo_dir / "workspace.json").write_text(
                json.dumps({"project_name": "fromjson"}), encoding="utf-8"
            )
            from setup_runtime import _detect_project_name
            self.assertEqual(_detect_project_name(repo_dir), "fromjson")

    def test_detects_project_from_oacp_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / ".oacp").write_text(
                json.dumps({"project_name": "from-oacp"}), encoding="utf-8"
            )
            from setup_runtime import _detect_project_name
            self.assertEqual(_detect_project_name(repo_dir), "from-oacp")

    def test_claude_without_project_uses_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            setup_runtime("claude", repo_dir=repo_dir)

            agent_file = repo_dir / ".claude" / "agents" / "<project>.md"
            self.assertTrue(agent_file.is_file())

    def test_claude_settings_merge_preserves_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            settings = repo_dir / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps({"env": {"EXISTING": "1"}, "hooks": {"Stop": []}}),
                encoding="utf-8",
            )
            result = setup_runtime("claude", repo_dir=repo_dir, project_name="demo")
            data = json.loads(settings.read_text(encoding="utf-8"))

            self.assertEqual(data["env"]["EXISTING"], "1")
            self.assertIn("Stop", data["hooks"])
            self.assertIn("SessionStart", data["hooks"])
            self.assertNotIn("SessionEnd", data["hooks"])
            self.assertIn(".claude/settings.json", result["created_files"])

    def test_claude_removes_only_generated_session_end_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            settings = repo_dir / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionEnd": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": ".claude/hooks/oacp-memory-push.sh",
                                        },
                                        {
                                            "type": "command",
                                            "command": ".claude/hooks/custom-session-end.sh",
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            legacy_hook = repo_dir / ".claude" / "hooks" / "oacp-memory-push.sh"
            legacy_hook.parent.mkdir(parents=True)
            legacy_hook.write_text("user-visible legacy file\n", encoding="utf-8")

            result = setup_runtime("claude", repo_dir=repo_dir, project_name="demo")
            data = json.loads(settings.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for entry in data["hooks"]["SessionEnd"]
                for hook in entry.get("hooks", [])
            ]
            self.assertNotIn(".claude/hooks/oacp-memory-push.sh", commands)
            self.assertIn(".claude/hooks/custom-session-end.sh", commands)
            self.assertEqual(
                legacy_hook.read_text(encoding="utf-8"), "user-visible legacy file\n"
            )
            self.assertIn(".claude/settings.json", result["created_files"])

    def test_codex_hooks_merge_preserves_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            hooks_file = repo_dir / ".codex" / "hooks.json"
            hooks_file.parent.mkdir(parents=True)
            hooks_file.write_text(
                json.dumps(
                    {
                        "description": "custom",
                        "hooks": {"Stop": [{"hooks": []}]},
                    }
                ),
                encoding="utf-8",
            )

            result = setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=repo_dir / "oacp",
            )
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertEqual(data["description"], "custom")
            self.assertIn("Stop", data["hooks"])
            self.assertIn("SessionStart", data["hooks"])
            self.assertIn(".codex/hooks.json", result["created_files"])

    def test_codex_hook_registration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            oacp_root = repo_dir / "oacp"
            setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )
            result = setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="demo",
                oacp_root=oacp_root,
            )

            data = json.loads(
                (repo_dir / ".codex" / "hooks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(data["hooks"]["SessionStart"]), 1)
            self.assertIn(".codex/hooks.json", result["skipped_files"])

    def test_codex_hook_registration_replaces_changed_managed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            first_root = repo_dir / "first-oacp"
            second_root = repo_dir / "second-oacp"
            setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="first",
                oacp_root=first_root,
            )
            result = setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="second",
                oacp_root=second_root,
            )

            data = json.loads(
                (repo_dir / ".codex" / "hooks.json").read_text(encoding="utf-8")
            )
            entries = data["hooks"]["SessionStart"]
            commands = [
                hook["command"]
                for entry in entries
                for hook in entry.get("hooks", [])
                if hook.get("command", "").startswith("oacp session-init --hook")
            ]
            self.assertEqual(len(commands), 1)
            self.assertIn("--project second", commands[0])
            self.assertIn(f"--hub-dir {second_root}", commands[0])
            self.assertNotIn("--project first", commands[0])
            self.assertNotIn(str(first_root), commands[0])
            self.assertIn(".codex/hooks.json", result["created_files"])

    def test_codex_hook_replacement_preserves_custom_session_start_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            hooks_file = repo_dir / ".codex" / "hooks.json"
            hooks_file.parent.mkdir(parents=True)
            hooks_file.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "matcher": "^startup$",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "oacp session-init --hook --pull-memory "
                                                "--project old --hub-dir /old/oacp"
                                            ),
                                        },
                                        {
                                            "type": "command",
                                            "command": ".codex/hooks/custom-startup.sh",
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            setup_runtime(
                "codex",
                repo_dir=repo_dir,
                project_name="new",
                oacp_root=repo_dir / "oacp",
            )

            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for entry in data["hooks"]["SessionStart"]
                for hook in entry.get("hooks", [])
            ]
            self.assertEqual(commands.count(".codex/hooks/custom-startup.sh"), 1)
            managed = [
                command
                for command in commands
                if command.startswith("oacp session-init --hook")
            ]
            self.assertEqual(len(managed), 1)
            self.assertIn("--project new", managed[0])

    def test_codex_hooks_warns_when_existing_file_is_not_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            hooks_file = repo_dir / ".codex" / "hooks.json"
            hooks_file.parent.mkdir(parents=True)
            hooks_file.write_text("[]", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = setup_runtime(
                    "codex", repo_dir=repo_dir, project_name="demo"
                )

            self.assertIn(".codex/hooks.json", result["warning_files"])
            self.assertIn("expected a JSON object", stderr.getvalue())
            self.assertEqual(hooks_file.read_text(encoding="utf-8"), "[]")

    def test_claude_settings_warns_when_existing_settings_is_not_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            settings = repo_dir / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("[]", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = setup_runtime("claude", repo_dir=repo_dir, project_name="demo")

            self.assertIn(".claude/settings.json", result["warning_files"])
            self.assertNotIn(".claude/settings.json", result["skipped_files"])
            self.assertIn("expected a JSON object", stderr.getvalue())
            self.assertEqual(settings.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
