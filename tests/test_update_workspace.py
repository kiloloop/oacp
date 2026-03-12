# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for update_workspace.sh."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "update_workspace.sh")


def run_update(project_root: str, *extra_args: str, env_override: dict | None = None) -> subprocess.CompletedProcess:
    """Run update_workspace.sh against a temp workspace."""
    project_name = os.path.basename(project_root)
    hub_root = os.path.dirname(os.path.dirname(project_root))  # strip projects/<name>
    env = os.environ.copy()
    env["OACP_HOME"] = hub_root
    if env_override:
        env.update(env_override)
    return subprocess.run(
        ["bash", SCRIPT, project_name, *extra_args],
        capture_output=True,
        text=True,
        env=env,
    )


def make_workspace(tmp: str) -> str:
    """Create a minimal workspace directory (simulates init having been run)."""
    project_root = os.path.join(tmp, "projects", "testproj")
    os.makedirs(project_root)
    return project_root


class TestGuard(unittest.TestCase):
    """Workspace must already exist."""

    def test_nonexistent_workspace_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = os.path.join(tmp, "hub")
            os.makedirs(os.path.join(hub, "projects"))
            env = os.environ.copy()
            env["OACP_HOME"] = hub
            result = subprocess.run(
                ["bash", SCRIPT, "no-such-project"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("does not exist", result.stderr)

    def test_no_args_shows_usage(self):
        result = subprocess.run(
            ["bash", SCRIPT],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stdout)

    def test_path_traversal_rejected(self):
        result = subprocess.run(
            ["bash", SCRIPT, "../../etc"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not contain", result.stderr)

    def test_dot_project_rejected(self):
        result = subprocess.run(
            ["bash", SCRIPT, ".hidden"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not contain", result.stderr)

    def test_repo_without_value_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            result = run_update(root, "--repo")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--repo requires a value", result.stderr)


class TestDirectoryCreation(unittest.TestCase):
    """Creates missing dirs that init_project_workspace.sh defines."""

    def test_creates_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            result = run_update(root)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(os.path.isdir(os.path.join(root, "state")))

    def test_creates_dead_letter_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            for agent in ("codex", "claude", "gemini"):
                self.assertTrue(
                    os.path.isdir(os.path.join(root, "agents", agent, "dead_letter")),
                    f"dead_letter missing for {agent}",
                )

    def test_creates_logs_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertTrue(os.path.isdir(os.path.join(root, "logs")))

    def test_creates_all_expected_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            expected = [
                "agents/codex/inbox", "agents/codex/outbox", "agents/codex/dead_letter",
                "agents/claude/inbox", "agents/claude/outbox", "agents/claude/dead_letter",
                "agents/gemini/inbox", "agents/gemini/outbox", "agents/gemini/dead_letter",
                "packets/review", "packets/findings", "packets/test", "packets/deploy",
                "checkpoints", "merges", "memory", "artifacts", "state", "logs",
            ]
            for d in expected:
                self.assertTrue(os.path.isdir(os.path.join(root, d)), f"Missing: {d}")


class TestIdempotency(unittest.TestCase):
    """Second run should report 0 created."""

    def test_second_run_zero_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            result = run_update(root)
            self.assertEqual(result.returncode, 0)
            self.assertIn("0 created", result.stdout)


class TestGitkeep(unittest.TestCase):
    """Creates .gitkeep in placeholder dirs but not in memory/state/logs."""

    def test_gitkeep_in_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertTrue(os.path.isfile(os.path.join(root, "agents", "codex", "inbox", ".gitkeep")))

    def test_gitkeep_in_dead_letter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertTrue(os.path.isfile(os.path.join(root, "agents", "claude", "dead_letter", ".gitkeep")))

    def test_no_gitkeep_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertFalse(os.path.isfile(os.path.join(root, "memory", ".gitkeep")))

    def test_no_gitkeep_in_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertFalse(os.path.isfile(os.path.join(root, "state", ".gitkeep")))

    def test_no_gitkeep_in_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertFalse(os.path.isfile(os.path.join(root, "logs", ".gitkeep")))


class TestMemoryFiles(unittest.TestCase):
    """Recreates memory files if missing, never overwrites existing."""

    def test_creates_memory_files_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            for f in ("project_facts.md", "decision_log.md", "open_threads.md"):
                path = os.path.join(root, "memory", f)
                self.assertTrue(os.path.isfile(path), f"Missing: memory/{f}")
                self.assertGreater(os.path.getsize(path), 0, f"Empty: memory/{f}")

    def test_never_overwrites_existing_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            os.makedirs(os.path.join(root, "memory"))
            sentinel = "DO NOT OVERWRITE THIS CONTENT"
            facts_path = os.path.join(root, "memory", "project_facts.md")
            with open(facts_path, "w") as f:
                f.write(sentinel)
            run_update(root)
            with open(facts_path) as f:
                self.assertEqual(f.read(), sentinel)


class TestNeverRemovesFiles(unittest.TestCase):
    """Update must never delete existing files."""

    def test_existing_files_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            os.makedirs(os.path.join(root, "packets", "review"))
            custom = os.path.join(root, "packets", "review", "my_packet.md")
            with open(custom, "w") as f:
                f.write("custom content")
            run_update(root)
            self.assertTrue(os.path.isfile(custom))
            with open(custom) as f:
                self.assertEqual(f.read(), "custom content")


class TestDryRun(unittest.TestCase):
    """--dry-run makes no filesystem changes."""

    def test_dry_run_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            result = run_update(root, "--dry-run")
            self.assertEqual(result.returncode, 0)
            self.assertIn("dry-run", result.stdout)
            # Only the base project dir should exist — nothing else created
            entries = set()
            for dirpath, dirnames, filenames in os.walk(root):
                for d in dirnames:
                    entries.add(os.path.join(dirpath, d))
                for f in filenames:
                    entries.add(os.path.join(dirpath, f))
            self.assertEqual(len(entries), 0, f"Dry-run created files: {entries}")


class TestSymlinks(unittest.TestCase):
    """--link creates and updates artifact symlinks."""

    def test_link_creates_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            repo = os.path.join(tmp, "repo")
            src_dir = os.path.join(repo, "reports")
            os.makedirs(src_dir)
            run_update(root, "--repo", repo, "--link", "reports:reports")
            link = os.path.join(root, "artifacts", "reports")
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.readlink(link), src_dir)

    def test_link_updates_existing_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            repo = os.path.join(tmp, "repo")
            old_dir = os.path.join(repo, "old_reports")
            new_dir = os.path.join(repo, "reports")
            os.makedirs(old_dir)
            os.makedirs(new_dir)
            # Create initial symlink to old target
            os.makedirs(os.path.join(root, "artifacts"))
            os.symlink(old_dir, os.path.join(root, "artifacts", "reports"))
            # Update to new target
            run_update(root, "--repo", repo, "--link", "reports:reports")
            link = os.path.join(root, "artifacts", "reports")
            self.assertEqual(os.readlink(link), new_dir)

    def test_link_without_repo_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            result = run_update(root, "--link", "reports:reports")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--link requires --repo", result.stderr)

    def test_broken_symlink_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            os.makedirs(os.path.join(root, "artifacts"))
            os.symlink("/nonexistent/path", os.path.join(root, "artifacts", "broken"))
            result = run_update(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("broken symlink", result.stdout)
            self.assertIn("1 warnings", result.stdout)


if __name__ == "__main__":
    unittest.main()
