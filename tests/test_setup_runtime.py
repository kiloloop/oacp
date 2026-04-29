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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

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
            push_hook = repo_dir / ".claude" / "hooks" / "oacp-memory-push.sh"
            settings_file = repo_dir / ".claude" / "settings.json"
            self.assertTrue(agent_file.is_file())
            self.assertTrue((repo_dir / ".claude" / "skills").is_dir())
            self.assertTrue(pull_hook.is_file())
            self.assertTrue(push_hook.is_file())
            self.assertTrue(settings_file.is_file())
            pull_content = pull_hook.read_text(encoding="utf-8")
            push_content = push_hook.read_text(encoding="utf-8")
            self.assertIn("Claude hook event: SessionStart", pull_content)
            self.assertIn("oacp memory pull", pull_content)
            self.assertIn("Claude hook event: SessionEnd", push_content)
            self.assertIn("oacp memory push", push_content)
            self.assertIn("|| true", push_content)
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertIn("SessionStart", settings["hooks"])
            self.assertIn("SessionEnd", settings["hooks"])
            self.assertIn(".claude/agents/myproj.md", result["created_files"])
            self.assertIn(".claude/skills/", result["created_files"])
            self.assertIn(".claude/hooks/oacp-memory-pull.sh", result["created_files"])
            self.assertIn(".claude/hooks/oacp-memory-push.sh", result["created_files"])
            self.assertIn(".claude/settings.json", result["created_files"])

    def test_codex_creates_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            result = setup_runtime("codex", repo_dir=repo_dir)

            agents_md = repo_dir / "AGENTS.md"
            self.assertTrue(agents_md.is_file())
            content = agents_md.read_text(encoding="utf-8")
            self.assertIn("OACP", content)
            self.assertIn("oacp send", content)
            self.assertIn("AGENTS.md", result["created_files"])

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
            self.assertIn("SessionEnd", data["hooks"])
            self.assertIn(".claude/settings.json", result["created_files"])

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
