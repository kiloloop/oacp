# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for workspace discovery config files (workspace.json and .oacp)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
INIT_SCRIPT = str(SCRIPTS_DIR / "init_project_workspace.sh")
UPDATE_SCRIPT = str(SCRIPTS_DIR / "update_workspace.sh")


def run_init(tmp: str, project: str, *extra_args: str) -> subprocess.CompletedProcess:
    """Run init_project_workspace.sh in a temp hub root."""
    hub_root = os.path.join(tmp, "hub")
    os.makedirs(os.path.join(hub_root, "projects"), exist_ok=True)
    env = os.environ.copy()
    env["OACP_HOME"] = hub_root
    return subprocess.run(
        ["bash", INIT_SCRIPT, project, *extra_args],
        capture_output=True,
        text=True,
        env=env,
    )


def run_update(project_root: str, *extra_args: str) -> subprocess.CompletedProcess:
    """Run update_workspace.sh against a temp workspace."""
    project_name = os.path.basename(project_root)
    hub_root = os.path.dirname(os.path.dirname(project_root))
    env = os.environ.copy()
    env["OACP_HOME"] = hub_root
    return subprocess.run(
        ["bash", UPDATE_SCRIPT, project_name, *extra_args],
        capture_output=True,
        text=True,
        env=env,
    )


def make_workspace(tmp: str, project: str = "testproj") -> str:
    """Create a minimal workspace directory (simulates init having been run)."""
    project_root = os.path.join(tmp, "projects", project)
    os.makedirs(project_root)
    return project_root


class TestInitCreatesWorkspaceJson(unittest.TestCase):
    """init_project_workspace.sh creates workspace.json with correct fields."""

    def test_workspace_json_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_init(tmp, "myproj")
            self.assertEqual(result.returncode, 0, result.stderr)
            ws_json = os.path.join(tmp, "hub", "projects", "myproj", "workspace.json")
            self.assertTrue(os.path.isfile(ws_json))

    def test_workspace_json_has_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_init(tmp, "myproj")
            ws_json = os.path.join(tmp, "hub", "projects", "myproj", "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            expected_keys = {"project_name", "repo_path", "created_at", "updated_at", "standards_version"}
            self.assertEqual(set(data.keys()), expected_keys)

    def test_workspace_json_project_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_init(tmp, "myproj")
            ws_json = os.path.join(tmp, "hub", "projects", "myproj", "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            self.assertEqual(data["project_name"], "myproj")

    def test_workspace_json_repo_path_null_without_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_init(tmp, "myproj")
            ws_json = os.path.join(tmp, "hub", "projects", "myproj", "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            self.assertIsNone(data["repo_path"])

    def test_workspace_json_repo_path_set_with_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            run_init(tmp, "myproj", "--repo", repo)
            ws_json = os.path.join(tmp, "hub", "projects", "myproj", "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            self.assertEqual(data["repo_path"], str(Path(repo).resolve()))

    def test_workspace_json_timestamps_are_iso(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_init(tmp, "myproj")
            ws_json = os.path.join(tmp, "hub", "projects", "myproj", "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            # Should contain T separator (ISO 8601)
            self.assertIn("T", data["created_at"])
            self.assertIn("T", data["updated_at"])

    def test_workspace_json_standards_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_init(tmp, "myproj")
            ws_json = os.path.join(tmp, "hub", "projects", "myproj", "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            # Should be a non-empty version string
            self.assertIsInstance(data["standards_version"], str)
            self.assertRegex(data["standards_version"], r"^\d+\.\d+\.\d+$")

    def test_workspace_json_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_init(tmp, "myproj")
            ws_json = os.path.join(tmp, "hub", "projects", "myproj", "workspace.json")
            with open(ws_json) as f:
                # Should not raise
                data = json.load(f)
            self.assertIsInstance(data, dict)


class TestInitNoLongerCreatesOacpMarker(unittest.TestCase):
    """init no longer creates .oacp in repo root (runtimes symlink workspace.json instead)."""

    def test_oacp_marker_not_created_with_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            result = run_init(tmp, "myproj", "--repo", repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            oacp_file = os.path.join(repo, ".oacp")
            self.assertFalse(os.path.isfile(oacp_file))

    def test_oacp_marker_not_created_without_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_init(tmp, "myproj")
            ws_root = os.path.join(tmp, "hub", "projects", "myproj")
            self.assertFalse(os.path.isfile(os.path.join(ws_root, ".oacp")))

    def test_prints_symlink_hint_with_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            result = run_init(tmp, "myproj", "--repo", repo)
            self.assertIn("ln -sf", result.stdout)


class TestUpdateBumpsWorkspaceJson(unittest.TestCase):
    """update_workspace.sh updates timestamps in existing workspace.json."""

    def test_updates_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            # Create initial workspace.json
            initial = {
                "project_name": "testproj",
                "repo_path": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "standards_version": "0.4.0",
            }
            ws_json = os.path.join(root, "workspace.json")
            with open(ws_json, "w") as f:
                json.dump(initial, f)
            time.sleep(0.1)  # Ensure different timestamp
            result = run_update(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(ws_json) as f:
                updated = json.load(f)
            self.assertNotEqual(updated["updated_at"], "2026-01-01T00:00:00+00:00")

    def test_preserves_created_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            initial = {
                "project_name": "testproj",
                "repo_path": "/some/repo",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "standards_version": "0.4.0",
            }
            ws_json = os.path.join(root, "workspace.json")
            with open(ws_json, "w") as f:
                json.dump(initial, f)
            run_update(root)
            with open(ws_json) as f:
                updated = json.load(f)
            self.assertEqual(updated["created_at"], "2026-01-01T00:00:00+00:00")

    def test_preserves_repo_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            initial = {
                "project_name": "testproj",
                "repo_path": "/my/repo",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "standards_version": "0.4.0",
            }
            ws_json = os.path.join(root, "workspace.json")
            with open(ws_json, "w") as f:
                json.dump(initial, f)
            run_update(root)
            with open(ws_json) as f:
                updated = json.load(f)
            self.assertEqual(updated["repo_path"], "/my/repo")

    def test_updates_standards_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            initial = {
                "project_name": "testproj",
                "repo_path": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "standards_version": "0.4.0",
            }
            ws_json = os.path.join(root, "workspace.json")
            with open(ws_json, "w") as f:
                json.dump(initial, f)
            run_update(root)
            with open(ws_json) as f:
                updated = json.load(f)
            # Should match current VERSION file
            self.assertRegex(updated["standards_version"], r"^\d+\.\d+\.\d+$")
            self.assertNotEqual(updated["standards_version"], "0.4.0")


class TestUpdateCreatesWorkspaceJsonIfMissing(unittest.TestCase):
    """update_workspace.sh creates workspace.json for pre-existing workspaces."""

    def test_creates_workspace_json_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            result = run_update(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            ws_json = os.path.join(root, "workspace.json")
            self.assertTrue(os.path.isfile(ws_json))

    def test_created_workspace_json_has_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            ws_json = os.path.join(root, "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            expected_keys = {"project_name", "repo_path", "created_at", "updated_at", "standards_version"}
            self.assertEqual(set(data.keys()), expected_keys)

    def test_created_workspace_json_project_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            ws_json = os.path.join(root, "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            self.assertEqual(data["project_name"], "testproj")

    def test_created_workspace_json_repo_path_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root)
            ws_json = os.path.join(root, "workspace.json")
            with open(ws_json) as f:
                data = json.load(f)
            self.assertIsNone(data["repo_path"])

    def test_dry_run_does_not_create_workspace_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(tmp)
            run_update(root, "--dry-run")
            ws_json = os.path.join(root, "workspace.json")
            self.assertFalse(os.path.isfile(ws_json))


if __name__ == "__main__":
    unittest.main()
