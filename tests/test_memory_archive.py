# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for project memory archive/restore tooling."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from memory_archive_common import build_archive_name  # noqa: E402
from memory_cli import main as memory_main  # noqa: E402
from promote_to_archive import archive_memory_file  # noqa: E402
from restore_from_archive import restore_memory_file  # noqa: E402


def _make_project_root(tmp: str) -> tuple[Path, Path]:
    oacp_root = Path(tmp) / "oacp"
    project_root = oacp_root / "projects" / "demo"
    (project_root / "memory" / "archive").mkdir(parents=True, exist_ok=True)
    return oacp_root, project_root


class TestMemoryArchiveScripts(unittest.TestCase):
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
