# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for update_workspace.sh."""

from __future__ import annotations

import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

BASH = "/bin/bash" if Path("/bin/bash").is_file() else "bash"
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
        [BASH, SCRIPT, project_name, *extra_args],
        capture_output=True,
        text=True,
        errors="surrogateescape",
        env=env,
    )


def make_workspace(tmp: str, agents=("codex", "claude", "gemini")) -> str:
    """Create a minimal workspace directory (simulates init having been run)."""
    project_root = os.path.join(tmp, "projects", "testproj")
    os.makedirs(project_root)
    for agent in agents:
        os.makedirs(os.path.join(project_root, "agents", agent))
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
                [BASH, SCRIPT, "no-such-project"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("does not exist", result.stderr)

    def test_no_args_shows_usage(self):
        result = subprocess.run(
            [BASH, SCRIPT],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stdout)

    def test_path_traversal_rejected(self):
        result = subprocess.run(
            [BASH, SCRIPT, "../../etc"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not contain", result.stderr)

    def test_dot_project_rejected(self):
        result = subprocess.run(
            [BASH, SCRIPT, ".hidden"],
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
    """Creates missing dirs that the workspace layout (`oacp init`) defines."""

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

    def test_creates_inbox_archive_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            for agent in ("codex", "claude", "gemini"):
                self.assertTrue(
                    os.path.isdir(
                        os.path.join(root, "agents", agent, "inbox", "archive")
                    ),
                    f"inbox/archive missing for {agent}",
                )

    def test_creates_logs_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertTrue(os.path.isdir(os.path.join(root, "logs")))

    def test_creates_memory_archive_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertTrue(os.path.isdir(os.path.join(root, "memory", "archive")))

    def test_creates_all_expected_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            expected = [
                "agents/codex/inbox", "agents/codex/inbox/archive",
                "agents/codex/outbox", "agents/codex/dead_letter",
                "agents/codex/audit/autonomy_decisions",
                "agents/claude/inbox", "agents/claude/inbox/archive",
                "agents/claude/outbox", "agents/claude/dead_letter",
                "agents/claude/audit/autonomy_decisions",
                "agents/gemini/inbox", "agents/gemini/inbox/archive",
                "agents/gemini/outbox", "agents/gemini/dead_letter",
                "agents/gemini/audit/autonomy_decisions",
                "packets/review", "packets/findings", "packets/test", "packets/deploy",
                "checkpoints", "merges", "memory", "memory/archive", "artifacts", "state", "logs",
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

    def test_gitkeep_in_inbox_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertTrue(
                os.path.isfile(
                    os.path.join(
                        root, "agents", "claude", "inbox", "archive", ".gitkeep"
                    )
                )
            )

    def test_gitkeep_in_autonomy_audit_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            self.assertTrue(
                os.path.isfile(
                    os.path.join(
                        root,
                        "agents",
                        "codex",
                        "audit",
                        "autonomy_decisions",
                        ".gitkeep",
                    )
                )
            )

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
            for f in ("project_facts.md", "decision_log.md", "open_threads.md", "known_debt.md"):
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

    def test_creates_known_debt_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            known_debt_path = os.path.join(root, "memory", "known_debt.md")
            with open(known_debt_path) as f:
                content = f.read()
            self.assertIn("# Known Debt", content)
            self.assertIn("| Item | Severity | Date Found | Source | Status |", content)


class TestAutonomyConfig(unittest.TestCase):
    """Backfills receiver autonomy config and audit dirs."""

    def test_creates_receiver_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            config_path = os.path.join(root, "agents", "codex", "config.yaml")
            self.assertTrue(os.path.isfile(config_path))
            with open(config_path) as f:
                content = f.read()
            self.assertIn("default_mode: always_pause", content)

    def test_never_overwrites_receiver_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            config_path = os.path.join(root, "agents", "codex", "config.yaml")
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w") as f:
                f.write("custom: true\n")
            run_update(root)
            with open(config_path) as f:
                self.assertEqual(f.read(), "custom: true\n")


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
            root = make_workspace(tmp, agents=())
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

    def test_dry_run_counts_missing_known_debt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            os.remove(os.path.join(root, "memory", "known_debt.md"))

            result = run_update(root, "--dry-run")

            self.assertEqual(result.returncode, 0)
            self.assertIn("file memory/known_debt.md (dry-run)", result.stdout)
            self.assertIn("1 created", result.stdout)
            self.assertFalse(os.path.exists(os.path.join(root, "memory", "known_debt.md")))


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


class TestExistingRoster(unittest.TestCase):
    """Migration uses existing synthetic agents, never a default roster."""

    def test_empty_roster_stays_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp, agents=())
            result = run_update(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(root) / "agents").exists())
            self.assertNotIn("agents/", result.stdout)
            self.assertIn("0 created", run_update(root).stdout)

    def test_custom_roster_and_current_policy_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(make_workspace(tmp, agents=("alice", "bob")))
            hidden = root / "agents" / ".scratch"
            hidden.mkdir()
            (root / "agents" / "not-an-agent.txt").write_text("keep")
            policy = root / "agents" / "alice" / "config.yaml"
            policy.write_bytes(b"# Synthetic policy format and identity.\nautonomy: {default_mode: always_pause}\nauth: synthetic-trailer\n")
            policy.chmod(0o600)
            card = root / "agents" / "alice" / "agent_card.yaml"
            card.write_text("name: alice\ncustom: keep\n")
            status_file = root / "agents" / "alice" / "status.yaml"
            status_file.write_text("state: custom\n")
            before = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                      for path in (policy, card, status_file)}
            preview = run_update(str(root), "--dry-run", "--quiet")
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("agents/bob/inbox", preview.stdout)
            self.assertFalse((root / "agents" / "bob" / "inbox").exists())
            result = run_update(str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            for agent in ("alice", "bob"):
                self.assertTrue((root / "agents" / agent / "inbox/archive/.gitkeep").is_file())
                self.assertTrue((root / "agents" / agent / "config.yaml").is_file())
            self.assertEqual(sorted(p.name for p in (root / "agents").iterdir()),
                             [".scratch", "alice", "bob", "not-an-agent.txt"])
            self.assertEqual(list(hidden.iterdir()), [])
            for path, metadata in before.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns), metadata)
            self.assertIn("0 created", run_update(str(root)).stdout)

    def test_backslash_project_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "OACP_HOME": tmp}
            result = subprocess.run([BASH, SCRIPT, "nested\\project"],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must not contain", result.stderr)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class TestLayoutDecoder(unittest.TestCase):
    def test_decoder_failure_stops_before_any_workspace_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(make_workspace(tmp, agents=()))
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            wrapper = bin_dir / "python3"
            # Synthetic interpreter failure after partial decoder output.
            wrapper.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "if len(sys.argv) > 2 and 'data = json.loads' in sys.argv[2]:\n"
                "    sys.stdout.buffer.write(b'Dmust-not-be-created\\0')\n"
                "    print('synthetic decoder failure', file=sys.stderr)\n"
                "    sys.exit(9)\n"
                "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
            )
            wrapper.chmod(0o755)
            for flags in ((), ("--dry-run",)):
                with self.subTest(flags=flags):
                    result = run_update(str(root), *flags, env_override={
                        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                        "TMPDIR": str(tmp),
                    })
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("synthetic decoder failure", result.stderr)
                    self.assertEqual(list(root.iterdir()), [])
                    self.assertNotIn("Updated workspace:", result.stdout)
                    self.assertEqual(list(Path(tmp).glob("oacp-layout.*")), [])

    def test_non_utf8_existing_agent_path_round_trips_where_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(make_workspace(tmp, agents=()))
            agents = root / "agents"
            agents.mkdir()
            raw_agent = os.fsencode(agents) + b"/agent-\xff"
            try:
                os.mkdir(raw_agent)
            except (OSError, UnicodeError) as exc:
                self.skipTest(f"filesystem rejects non-UTF8 directory names: {exc}")
            preview = run_update(str(root), "--dry-run")
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertFalse(os.path.isdir(raw_agent + b"/inbox"))
            applied = run_update(str(root))
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertTrue(os.path.isfile(raw_agent + b"/inbox/archive/.gitkeep"))
            self.assertEqual(os.listdir(os.fsencode(agents)), [b"agent-\xff"])
            repeated = run_update(str(root))
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("0 created", repeated.stdout)


if __name__ == "__main__":
    unittest.main()
