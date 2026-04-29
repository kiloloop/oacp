# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for project memory archive/restore tooling."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from memory_archive_common import build_archive_name  # noqa: E402
from memory_sync import CANONICAL_MEMORY_GITIGNORE, is_allowed_memory_path  # noqa: E402
from memory_cli import main as memory_main  # noqa: E402
from promote_to_archive import archive_memory_file  # noqa: E402
from restore_from_archive import restore_memory_file  # noqa: E402


def _make_project_root(tmp: str) -> tuple[Path, Path]:
    oacp_root = Path(tmp) / "oacp"
    project_root = oacp_root / "projects" / "demo"
    (project_root / "memory" / "archive").mkdir(parents=True, exist_ok=True)
    return oacp_root, project_root


class TestMemoryArchiveScripts(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    @contextlib.contextmanager
    def _git_identity(self):
        with mock.patch.dict(
            os.environ,
            {
                "GIT_AUTHOR_NAME": "OACP Test",
                "GIT_AUTHOR_EMAIL": "oacp-test@example.com",
                "GIT_COMMITTER_NAME": "OACP Test",
                "GIT_COMMITTER_EMAIL": "oacp-test@example.com",
                "OACP_AGENT": "codex",
            },
        ):
            yield

    def test_memory_cli_init_writes_marker_allowlist_and_initial_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            stdout = io.StringIO()

            with self._git_identity(), contextlib.redirect_stdout(stdout):
                code = memory_main(["init", "--oacp-dir", str(oacp_root)])

            self.assertEqual(code, 0)
            self.assertTrue((oacp_root / ".git").is_dir())
            self.assertTrue((oacp_root / ".oacp-memory-repo").is_file())
            self.assertEqual(
                (oacp_root / ".gitignore").read_text(encoding="utf-8"),
                CANONICAL_MEMORY_GITIGNORE,
            )
            log = self._git(oacp_root, "log", "-1", "--format=%s")
            self.assertEqual(log.returncode, 0)
            self.assertIn("memory: codex@", log.stdout)

    def test_memory_pull_noops_silently_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            oacp_root.mkdir()
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = memory_main(["pull", "--oacp-dir", str(oacp_root)])

            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")

    def test_memory_push_adds_allowlist_paths_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            with self._git_identity():
                self.assertEqual(memory_main(["init", "--oacp-dir", str(oacp_root)]), 0)
                (oacp_root / "org-memory").mkdir()
                (oacp_root / "org-memory" / "recent.md").write_text(
                    "# recent\n",
                    encoding="utf-8",
                )
                memory_dir = oacp_root / "projects" / "demo" / "memory"
                memory_dir.mkdir(parents=True)
                (memory_dir / "project_facts.md").write_text("# facts\n", encoding="utf-8")
                agent_dir = oacp_root / "projects" / "demo" / "agents" / "codex"
                agent_dir.mkdir(parents=True)
                (agent_dir / "status.yaml").write_text("status: busy\n", encoding="utf-8")

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = memory_main(["push", "--oacp-dir", str(oacp_root)])

            self.assertEqual(code, 0)
            files = self._git(oacp_root, "ls-files")
            self.assertEqual(files.returncode, 0)
            tracked = set(files.stdout.splitlines())
            self.assertIn("org-memory/recent.md", tracked)
            self.assertIn("projects/demo/memory/project_facts.md", tracked)
            self.assertNotIn("projects/demo/agents/codex/status.yaml", tracked)
            self.assertNotIn("uncommitted memory changes", stdout.getvalue())

    def test_memory_push_refuses_diverged_repo_without_committing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "memory.git"
            repo_a = root / "a"
            repo_b = root / "b"

            self.assertEqual(self._git(root, "init", "--bare", str(remote)).returncode, 0)
            self.assertEqual(self._git(root, "clone", str(remote), str(repo_a)).returncode, 0)
            with self._git_identity(), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    memory_main(["init", "--remote", str(remote), "--oacp-dir", str(repo_a)]),
                    0,
                )
            self.assertEqual(self._git(root, "clone", str(remote), str(repo_b)).returncode, 0)

            with self._git_identity():
                (repo_a / "org-memory").mkdir(exist_ok=True)
                (repo_a / "org-memory" / "remote.md").write_text(
                    "# remote\n",
                    encoding="utf-8",
                )
                self.assertEqual(self._git(repo_a, "add", "org-memory/remote.md").returncode, 0)
                self.assertEqual(self._git(repo_a, "commit", "-m", "remote memory").returncode, 0)
                self.assertEqual(self._git(repo_a, "push").returncode, 0)

                (repo_b / "org-memory").mkdir(exist_ok=True)
                (repo_b / "org-memory" / "local.md").write_text("# local\n", encoding="utf-8")
                self.assertEqual(self._git(repo_b, "add", "org-memory/local.md").returncode, 0)
                self.assertEqual(self._git(repo_b, "commit", "-m", "local memory").returncode, 0)
                before = self._git(repo_b, "rev-list", "--count", "HEAD").stdout.strip()
                (repo_b / "org-memory" / "uncommitted.md").write_text(
                    "# uncommitted\n",
                    encoding="utf-8",
                )

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = memory_main(["push", "--oacp-dir", str(repo_b)])

            after = self._git(repo_b, "rev-list", "--count", "HEAD").stdout.strip()
            self.assertEqual(code, 1)
            self.assertEqual(before, after)
            self.assertIn("diverged", stdout.getvalue())

    def test_memory_push_noops_silently_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            oacp_root.mkdir()
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = memory_main(["push", "--oacp-dir", str(oacp_root)])

            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")

    def test_is_allowed_memory_path_excludes_cache_entry(self) -> None:
        self.assertFalse(is_allowed_memory_path("projects/demo/memory/.cache"))
        self.assertFalse(is_allowed_memory_path("projects/demo/memory/.cache/item.md"))
        self.assertTrue(is_allowed_memory_path("projects/demo/memory/project_facts.md"))

    def test_memory_clone_refuses_non_empty_target_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            oacp_root.mkdir()
            (oacp_root / "workspace.json").write_text("{}", encoding="utf-8")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                code = memory_main(
                    ["clone", "https://example.invalid/memory.git", "--oacp-dir", str(oacp_root)]
                )

            self.assertEqual(code, 1)
            self.assertIn("Refusing to clone into non-empty OACP_HOME", stderr.getvalue())

    def test_memory_clone_force_restores_target_when_clone_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            oacp_root.mkdir()
            (oacp_root / "workspace.json").write_text("{}", encoding="utf-8")
            missing_remote = Path(tmp) / "missing.git"
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                code = memory_main(
                    ["clone", str(missing_remote), "--force", "--oacp-dir", str(oacp_root)]
                )

            self.assertEqual(code, 1)
            self.assertTrue((oacp_root / "workspace.json").is_file())

    def test_memory_init_adds_origin_when_other_remote_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            remote = Path(tmp) / "memory.git"
            other_remote = Path(tmp) / "other.git"
            self.assertEqual(self._git(Path(tmp), "init", "--bare", str(remote)).returncode, 0)
            self.assertEqual(
                self._git(Path(tmp), "init", "--bare", str(other_remote)).returncode,
                0,
            )
            oacp_root.mkdir()
            self.assertEqual(self._git(oacp_root, "init").returncode, 0)
            self.assertEqual(
                self._git(oacp_root, "remote", "add", "upstream", str(other_remote)).returncode,
                0,
            )

            with self._git_identity(), contextlib.redirect_stdout(io.StringIO()):
                code = memory_main(
                    ["init", "--remote", str(remote), "--oacp-dir", str(oacp_root)]
                )

            self.assertEqual(code, 0)
            origin = self._git(oacp_root, "remote", "get-url", "origin")
            self.assertEqual(origin.stdout.strip(), str(remote))

    def test_memory_disable_removes_marker_but_keeps_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            with self._git_identity(), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(memory_main(["init", "--oacp-dir", str(oacp_root)]), 0)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = memory_main(["disable", "--oacp-dir", str(oacp_root)])

            self.assertEqual(code, 0)
            self.assertFalse((oacp_root / ".oacp-memory-repo").exists())
            self.assertTrue((oacp_root / ".git").is_dir())

    def test_archive_memory_file_moves_into_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, project_root = _make_project_root(tmp)
            source = project_root / "memory" / "notes.md"
            source.write_text("# notes\n", encoding="utf-8")
            fixed_now = dt.datetime(2026, 3, 20, 1, 2, 3, tzinfo=dt.timezone.utc)

            result = archive_memory_file(
                "demo",
                "notes.md",
                oacp_root=oacp_root,
                now=fixed_now,
            )

            destination = project_root / "memory" / "archive" / result["archived_file"]
            self.assertEqual(result["status"], "archived")
            self.assertFalse(source.exists())
            self.assertTrue(destination.is_file())
            self.assertEqual(result["archived_file"], "20260320T010203Z_notes.md")

    def test_restore_memory_file_moves_back_to_active_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, project_root = _make_project_root(tmp)
            archived = project_root / "memory" / "archive" / "20260320T010203Z_notes.md"
            archived.write_text("# archived\n", encoding="utf-8")

            result = restore_memory_file(
                "demo",
                "20260320T010203Z_notes.md",
                oacp_root=oacp_root,
            )

            destination = project_root / "memory" / result["restored_file"]
            self.assertEqual(result["status"], "restored")
            self.assertFalse(archived.exists())
            self.assertTrue(destination.is_file())
            self.assertEqual(result["restored_file"], "notes.md")

    def test_archive_rejects_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, _ = _make_project_root(tmp)
            with self.assertRaisesRegex(ValueError, "memory file not found"):
                archive_memory_file("demo", "notes.md", oacp_root=oacp_root)

    def test_archive_rejects_destination_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, project_root = _make_project_root(tmp)
            source = project_root / "memory" / "notes.md"
            source.write_text("# notes\n", encoding="utf-8")
            fixed_now = dt.datetime(2026, 3, 20, 1, 2, 3, tzinfo=dt.timezone.utc)
            archived_name = build_archive_name("notes.md", now=fixed_now)
            (project_root / "memory" / "archive" / archived_name).write_text(
                "# existing\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "archive destination already exists"):
                archive_memory_file(
                    "demo",
                    "notes.md",
                    oacp_root=oacp_root,
                    now=fixed_now,
                )

    def test_restore_rejects_existing_active_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, project_root = _make_project_root(tmp)
            (project_root / "memory" / "archive" / "20260320T010203Z_notes.md").write_text(
                "# archived\n", encoding="utf-8"
            )
            (project_root / "memory" / "notes.md").write_text("# current\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "active memory destination already exists"):
                restore_memory_file(
                    "demo",
                    "20260320T010203Z_notes.md",
                    oacp_root=oacp_root,
                )

    def test_restore_rejects_missing_archive_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root = Path(tmp) / "oacp"
            project_root = oacp_root / "projects" / "demo"
            (project_root / "memory").mkdir(parents=True, exist_ok=True)

            with self.assertRaisesRegex(ValueError, "memory archive directory not found"):
                restore_memory_file(
                    "demo",
                    "20260320T010203Z_notes.md",
                    oacp_root=oacp_root,
                )

    def test_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, _ = _make_project_root(tmp)
            with self.assertRaisesRegex(ValueError, "simple basename"):
                archive_memory_file("demo", "../notes.md", oacp_root=oacp_root)

    def test_archive_rejects_project_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, _ = _make_project_root(tmp)
            with self.assertRaisesRegex(
                ValueError, "project name must not contain path separators"
            ):
                archive_memory_file("../demo", "notes.md", oacp_root=oacp_root)

    def test_restore_rejects_invalid_archive_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, _ = _make_project_root(tmp)
            with self.assertRaisesRegex(ValueError, "must match <UTC timestamp>_<basename>"):
                restore_memory_file("demo", "notes.md", oacp_root=oacp_root)

    def test_archive_rejects_standard_active_memory_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, project_root = _make_project_root(tmp)
            (project_root / "memory" / "known_debt.md").write_text("# debt\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cannot archive standard active memory file"):
                archive_memory_file("demo", "known_debt.md", oacp_root=oacp_root)

    def test_archive_dry_run_makes_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, project_root = _make_project_root(tmp)
            source = project_root / "memory" / "notes.md"
            source.write_text("# notes\n", encoding="utf-8")
            fixed_now = dt.datetime(2026, 3, 20, 1, 2, 3, tzinfo=dt.timezone.utc)

            result = archive_memory_file(
                "demo",
                "notes.md",
                oacp_root=oacp_root,
                dry_run=True,
                now=fixed_now,
            )

            destination = project_root / "memory" / "archive" / result["archived_file"]
            self.assertEqual(result["status"], "dry-run")
            self.assertTrue(source.is_file())
            self.assertFalse(destination.exists())

    def test_memory_cli_archive_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, project_root = _make_project_root(tmp)
            (project_root / "memory" / "notes.md").write_text("# notes\n", encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = memory_main(
                    [
                        "archive",
                        "demo",
                        "notes.md",
                        "--oacp-dir",
                        str(oacp_root),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["action"], "archive")
            self.assertRegex(payload["archived_file"], r"^\d{8}T\d{6}Z_notes\.md$")

    def test_memory_cli_restore_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_root, project_root = _make_project_root(tmp)
            archived_name = "20260320T010203Z_notes.md"
            (project_root / "memory" / "archive" / archived_name).write_text(
                "# archived\n", encoding="utf-8"
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = memory_main(
                    [
                        "restore",
                        "demo",
                        archived_name,
                        "--oacp-dir",
                        str(oacp_root),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["action"], "restore")
            self.assertEqual(payload["restored_file"], "notes.md")


if __name__ == "__main__":
    unittest.main()
