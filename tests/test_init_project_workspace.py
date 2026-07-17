# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/init_project_workspace.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _oacp_constants import SPEC_VERSION  # noqa: E402
from init_project_workspace import initialize_workspace, main  # noqa: E402


class TestInitializeWorkspace(unittest.TestCase):
    def test_workspace_json_stamps_spec_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = initialize_workspace("demo", oacp_root=Path(tmpdir))
            workspace = json.loads(
                Path(result["workspace_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(workspace["spec_version"], SPEC_VERSION)
            self.assertNotIn("standards_version", workspace)

    def test_cli_oacp_dir_targets_explicit_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "scratch_home"
            code = main(["demo", "--agents", "claude", "--oacp-dir", str(home)])
            self.assertEqual(code, 0)
            self.assertTrue(
                (home / "projects" / "demo" / "workspace.json").is_file()
            )

    def test_creates_workspace_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_root = Path(tmpdir)
            result = initialize_workspace("demo", oacp_root=hub_root)

            project_root = Path(result["project_root"])
            self.assertTrue((project_root / "agents" / "codex" / "inbox").is_dir())
            self.assertTrue((project_root / "agents" / "claude" / "inbox").is_dir())
            self.assertTrue((project_root / "agents" / "cursor" / "inbox").is_dir())
            self.assertFalse((project_root / "agents" / "gemini").exists())
            self.assertTrue(
                (
                    project_root
                    / "agents"
                    / "codex"
                    / "audit"
                    / "autonomy_decisions"
                ).is_dir()
            )
            self.assertTrue((project_root / "agents" / "codex" / "config.yaml").is_file())
            self.assertTrue((project_root / "packets" / "review").is_dir())
            self.assertTrue((project_root / "memory" / "project_facts.md").is_file())
            self.assertTrue((project_root / "memory" / "known_debt.md").is_file())
            self.assertTrue((project_root / "memory" / "archive").is_dir())

            workspace = json.loads((project_root / "workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(workspace["project_name"], "demo")
            self.assertIsNone(workspace["repo_path"])

    def test_custom_agents_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_root = Path(tmpdir)
            result = initialize_workspace(
                "demo", oacp_root=hub_root, agents=["alice", "bob"]
            )

            project_root = Path(result["project_root"])
            self.assertTrue((project_root / "agents" / "alice" / "inbox").is_dir())
            self.assertTrue((project_root / "agents" / "bob" / "inbox").is_dir())
            self.assertTrue(
                (
                    project_root
                    / "agents"
                    / "alice"
                    / "audit"
                    / "autonomy_decisions"
                    / ".gitkeep"
                ).is_file()
            )
            self.assertTrue((project_root / "agents" / "alice" / "config.yaml").is_file())
            # Default agents should NOT exist
            self.assertFalse((project_root / "agents" / "claude").exists())
            self.assertFalse((project_root / "agents" / "codex").exists())
            self.assertFalse((project_root / "agents" / "cursor").exists())
            # Static dirs still created
            self.assertTrue((project_root / "packets" / "review").is_dir())

    def test_custom_agents_can_include_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_root = Path(tmpdir)
            result = initialize_workspace(
                "demo", oacp_root=hub_root, agents=["claude", "codex", "gemini"]
            )

            project_root = Path(result["project_root"])
            self.assertTrue((project_root / "agents" / "gemini" / "inbox").is_dir())
            self.assertTrue((project_root / "agents" / "gemini" / "config.yaml").is_file())

    def test_rejects_traversal_agent_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_root = Path(tmpdir)
            with self.assertRaises(ValueError):
                initialize_workspace(
                    "demo", oacp_root=hub_root, agents=["../../escape"]
                )
            with self.assertRaises(ValueError):
                initialize_workspace(
                    "demo", oacp_root=hub_root, agents=[".."]
                )

    def test_empty_agents_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_root = Path(tmpdir)
            result = initialize_workspace(
                "demo", oacp_root=hub_root, agents=()
            )

            project_root = Path(result["project_root"])
            # No agents dir entries
            self.assertFalse((project_root / "agents").exists())
            # Static dirs still created
            self.assertTrue((project_root / "packets" / "review").is_dir())
            self.assertTrue((project_root / "memory" / "project_facts.md").is_file())
            self.assertTrue((project_root / "memory" / "known_debt.md").is_file())
            self.assertTrue((project_root / "memory" / "archive").is_dir())

    def test_known_debt_template_has_expected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_root = Path(tmpdir)
            result = initialize_workspace("demo", oacp_root=hub_root)

            project_root = Path(result["project_root"])
            known_debt = (project_root / "memory" / "known_debt.md").read_text(
                encoding="utf-8"
            )

            self.assertIn("# Known Debt", known_debt)
            self.assertIn("| Item | Severity | Date Found | Source | Status |", known_debt)

    def test_artifact_links_require_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hub_root = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                initialize_workspace(
                    "demo",
                    oacp_root=hub_root,
                    artifact_links=[("docs", "docs")],
                )
            self.assertIn("--link requires --repo", str(ctx.exception))

    def test_creates_artifact_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as repo_tmpdir:
            hub_root = Path(tmpdir)
            repo_dir = Path(repo_tmpdir)
            (repo_dir / "docs").mkdir()

            initialize_workspace(
                "demo",
                oacp_root=hub_root,
                repo_dir=repo_dir,
                artifact_links=[("docs", "docs")],
            )

            link_path = hub_root / "projects" / "demo" / "artifacts" / "docs"
            self.assertTrue(link_path.is_symlink())
            self.assertEqual(link_path.resolve(), (repo_dir / "docs").resolve())


if __name__ == "__main__":
    unittest.main()
