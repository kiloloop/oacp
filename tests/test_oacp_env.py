# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/_oacp_env.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _oacp_env import resolve_oacp_home  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestResolveOacpHome(unittest.TestCase):
    def test_explicit_path_wins(self) -> None:
        resolved = resolve_oacp_home("/tmp/custom-oacp")
        self.assertEqual(resolved, Path("/tmp/custom-oacp"))

    def test_env_var_wins_when_present(self) -> None:
        with mock.patch.dict(os.environ, {"OACP_HOME": "/tmp/env-oacp"}, clear=False):
            resolved = resolve_oacp_home()
        self.assertEqual(resolved, Path("/tmp/env-oacp"))

    def test_discovers_home_from_oacp_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_dir = root / "repo"
            oacp_home = root / "hq-src" / "oacp_home"
            workspace = oacp_home / "projects" / "demo" / "workspace.json"
            _write(workspace, '{"project_name": "demo"}')
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / ".oacp").symlink_to(workspace)

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OACP_HOME", None)
                resolved = resolve_oacp_home(cwd=repo_dir)

            self.assertEqual(resolved.resolve(), oacp_home.resolve())

    def test_discovers_home_from_parent_repo_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_dir = root / "repo"
            nested_dir = repo_dir / "subdir" / "deep"
            oacp_home = root / "hq-src" / "oacp_home"
            workspace = oacp_home / "projects" / "demo" / "workspace.json"
            _write(workspace, '{"project_name": "demo"}')
            nested_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / ".oacp").symlink_to(workspace)

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OACP_HOME", None)
                resolved = resolve_oacp_home(cwd=nested_dir)

            self.assertEqual(resolved.resolve(), oacp_home.resolve())

    def test_discovers_home_from_workspace_json_in_project_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oacp_home = Path(tmp) / "hq-src" / "oacp_home"
            project_dir = oacp_home / "projects" / "demo"
            workspace = project_dir / "workspace.json"
            _write(workspace, '{"project_name": "demo"}')

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OACP_HOME", None)
                resolved = resolve_oacp_home(cwd=project_dir)

            self.assertEqual(resolved.resolve(), oacp_home.resolve())


if __name__ == "__main__":
    unittest.main()
