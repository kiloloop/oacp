# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for shared OACP constants/helpers."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _oacp_constants import (  # noqa: E402
    AGENT_RE,
    WORKSPACE_LAYOUT,
    existing_agent_names,
    validate_project_name,
    workspace_layout,
    _template_path,
    _write_if_missing,
    is_agent_dir,
    utc_now_iso,
)


class TestAgentRegex(unittest.TestCase):
    def test_rejects_leading_punctuation(self) -> None:
        self.assertIsNone(AGENT_RE.fullmatch("_bot"))
        self.assertIsNone(AGENT_RE.fullmatch(".bot"))
        self.assertIsNone(AGENT_RE.fullmatch("-bot"))

    def test_accepts_alphanumeric_lead(self) -> None:
        self.assertIsNotNone(AGENT_RE.fullmatch("iris"))
        self.assertIsNotNone(AGENT_RE.fullmatch("agent_1"))


class TestAgentDirectory(unittest.TestCase):
    def test_accepts_only_visible_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            visible = root / "claude"
            hidden = root / ".claude"
            visible.mkdir()
            hidden.mkdir()
            file_path = root / "codex"
            file_path.touch()

            self.assertTrue(is_agent_dir(visible))
            self.assertFalse(is_agent_dir(hidden))
            self.assertFalse(is_agent_dir(file_path))


class TestUtcNowIso(unittest.TestCase):
    def test_formats_aware_timestamp(self) -> None:
        now = dt.datetime(2026, 3, 20, 12, 34, 56, tzinfo=dt.timezone.utc)
        self.assertEqual(utc_now_iso(now), "2026-03-20T12:34:56Z")

    def test_assumes_naive_timestamp_is_utc(self) -> None:
        now = dt.datetime(2026, 3, 20, 12, 34, 56)
        self.assertEqual(utc_now_iso(now), "2026-03-20T12:34:56Z")


class TestWriteIfMissing(unittest.TestCase):
    def test_writes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "file.txt"
            self.assertTrue(_write_if_missing(path, "hello"))
            self.assertFalse(_write_if_missing(path, "goodbye"))
            self.assertEqual(path.read_text(encoding="utf-8"), "hello")


class TestTemplatePath(unittest.TestCase):
    def test_resolves_repo_template(self) -> None:
        with _template_path("agent_card.template.yaml") as path:
            self.assertTrue(path.is_file())


class TestWorkspaceLayout(unittest.TestCase):
    def test_table_and_expansion_are_immutable(self):
        self.assertIsInstance(WORKSPACE_LAYOUT, tuple)
        self.assertTrue(all(isinstance(row, tuple) for row in WORKSPACE_LAYOUT))
        empty = workspace_layout()
        self.assertTrue(empty)
        self.assertTrue(all(not path.startswith("agents/") for path, _ in empty))
        selected = dict(workspace_layout(("alice", "bob")))
        for agent in ("alice", "bob"):
            self.assertTrue(selected[f"agents/{agent}/inbox/archive"])
            self.assertTrue(selected[f"agents/{agent}/audit/autonomy_decisions"])
        for path in ("memory", "memory/archive", "state", "logs"):
            self.assertFalse(selected[path])
        self.assertTrue(selected["packets/review"])
        self.assertEqual(dict(workspace_layout(("alice",), scope="agent")),
                         {path: keep for path, keep in selected.items() if path.startswith("agents/alice/")})

    def test_project_name_boundary_is_shared_without_narrowing_other_names(self):
        from add_agent import _validate_project_name as add_validate
        from init_project_workspace import _validate_project_name as init_validate
        self.assertIs(add_validate, validate_project_name)
        self.assertIs(init_validate, validate_project_name)
        for invalid in (".hidden", "..", "a/b", "a\\b"):
            with self.subTest(name=invalid), self.assertRaisesRegex(ValueError, "path separators"):
                validate_project_name(invalid)
        for valid in ("demo", "a..b", "project 1", "_custom"):
            validate_project_name(valid)

    def test_existing_roster_is_sorted_and_skips_hidden_or_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(existing_agent_names(root), ())
            agents = root / "agents"
            agents.mkdir()
            for name in ("bob", ".scratch", "alice"):
                (agents / name).mkdir()
            (agents / "not-an-agent.txt").touch()
            self.assertEqual(existing_agent_names(root), ("alice", "bob"))


if __name__ == "__main__":
    unittest.main()
