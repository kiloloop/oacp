# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/add_agent.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from add_agent import add_agent  # noqa: E402


class TestAddAgent(unittest.TestCase):
    def _make_project(self, tmpdir: Path, project: str = "demo") -> Path:
        """Create a minimal project workspace for testing."""
        project_dir = tmpdir / "projects" / project
        project_dir.mkdir(parents=True)
        return tmpdir

    def test_creates_agent_dir_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            result = add_agent("demo", "alice", oacp_root=oacp_root)

            agent_dir = result["agent_dir"]
            self.assertTrue((agent_dir / "inbox").is_dir())
            self.assertTrue((agent_dir / "outbox").is_dir())
            self.assertTrue((agent_dir / "dead_letter").is_dir())
            self.assertTrue((agent_dir / "inbox" / ".gitkeep").exists())
            self.assertTrue((agent_dir / "outbox" / ".gitkeep").exists())
            self.assertTrue((agent_dir / "dead_letter" / ".gitkeep").exists())
            self.assertEqual(len(result["created_files"]), 3)
            self.assertEqual(len(result["skipped_files"]), 0)

    def test_creates_status_and_card_with_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            result = add_agent(
                "demo", "bob", oacp_root=oacp_root, runtime="claude"
            )

            agent_dir = result["agent_dir"]
            self.assertTrue((agent_dir / "status.yaml").is_file())
            self.assertTrue((agent_dir / "agent_card.yaml").is_file())

            status = (agent_dir / "status.yaml").read_text(encoding="utf-8")
            self.assertIn("runtime: claude", status)
            self.assertIn("status: available", status)

            card = (agent_dir / "agent_card.yaml").read_text(encoding="utf-8")
            self.assertIn('name: "bob"', card)
            self.assertIn('runtime: "claude"', card)
            self.assertIn("agents/bob/inbox/", card)
            self.assertIn('description: "bob agent (claude runtime)"', card)

            # 3 gitkeeps + status.yaml + agent_card.yaml = 5
            self.assertEqual(len(result["created_files"]), 5)

    def test_no_optional_files_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            result = add_agent("demo", "plain", oacp_root=oacp_root)

            agent_dir = result["agent_dir"]
            self.assertFalse((agent_dir / "status.yaml").exists())
            self.assertFalse((agent_dir / "agent_card.yaml").exists())
            self.assertEqual(len(result["created_files"]), 3)

    def test_rejects_invalid_agent_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            with self.assertRaises(ValueError):
                add_agent("demo", "bad/name", oacp_root=oacp_root)

    def test_rejects_dot_and_dotdot_agent_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            with self.assertRaises(ValueError):
                add_agent("demo", ".", oacp_root=oacp_root)
            with self.assertRaises(ValueError):
                add_agent("demo", "..", oacp_root=oacp_root)
            with self.assertRaises(ValueError):
                add_agent("demo", ".hidden", oacp_root=oacp_root)

    def test_rejects_invalid_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            with self.assertRaises(ValueError) as ctx:
                add_agent(
                    "demo", "agent1", oacp_root=oacp_root, runtime="unknown"
                )
            self.assertIn("Invalid runtime", str(ctx.exception))

    def test_errors_when_project_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                add_agent("nonexistent", "agent1", oacp_root=oacp_root)
            self.assertIn("not found", str(ctx.exception))

    def test_idempotent_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))

            # First run
            result1 = add_agent(
                "demo", "alice", oacp_root=oacp_root, runtime="codex"
            )
            self.assertEqual(len(result1["created_files"]), 5)
            self.assertEqual(len(result1["skipped_files"]), 0)

            # Second run — everything should be skipped
            result2 = add_agent(
                "demo", "alice", oacp_root=oacp_root, runtime="codex"
            )
            self.assertEqual(len(result2["created_files"]), 0)
            self.assertEqual(len(result2["skipped_files"]), 5)

    def test_agent_name_max_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            long_name = "a" * 65
            with self.assertRaises(ValueError):
                add_agent("demo", long_name, oacp_root=oacp_root)

    def test_rejects_path_traversal_project_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = Path(tmpdir)
            with self.assertRaises(ValueError):
                add_agent("../../etc", "agent1", oacp_root=oacp_root)
            with self.assertRaises(ValueError):
                add_agent(".hidden", "agent1", oacp_root=oacp_root)

    def test_agent_name_special_chars(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            # dots, hyphens, underscores are valid
            result = add_agent("demo", "my-agent_v1.0", oacp_root=oacp_root)
            self.assertTrue(result["agent_dir"].is_dir())

    def test_populates_card_from_global_profile(self) -> None:
        """Verify add-agent uses global profile for description/model defaults."""
        import yaml

        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            # Create a global profile
            global_dir = oacp_root / "agents" / "alice"
            global_dir.mkdir(parents=True)
            (global_dir / "profile.yaml").write_text(
                yaml.dump({
                    "version": "0.2.0",
                    "name": "alice",
                    "runtime": "claude",
                    "model": "claude-opus-4-6",
                    "description": "Senior architect agent",
                }),
                encoding="utf-8",
            )

            result = add_agent(
                "demo", "alice", oacp_root=oacp_root, runtime="claude"
            )

            agent_dir = result["agent_dir"]
            card = (agent_dir / "agent_card.yaml").read_text(encoding="utf-8")
            # Global profile's description should be used
            self.assertIn("Senior architect agent", card)
            # Global profile's model should be used
            self.assertIn("claude-opus-4-6", card)


    def test_card_does_not_contain_profile_tier_fields(self) -> None:
        """Scaffolded cards should not include routing_rules, trust_level, quota."""
        with tempfile.TemporaryDirectory() as tmpdir:
            oacp_root = self._make_project(Path(tmpdir))
            result = add_agent(
                "demo", "bob", oacp_root=oacp_root, runtime="claude"
            )
            agent_dir = result["agent_dir"]
            card = (agent_dir / "agent_card.yaml").read_text(encoding="utf-8")
            # Profile-tier fields should be stripped from scaffolded cards
            self.assertNotIn("routing_rules:", card)
            self.assertNotIn("trust_level:", card)
            self.assertNotIn("quota:", card)


if __name__ == "__main__":
    unittest.main()
