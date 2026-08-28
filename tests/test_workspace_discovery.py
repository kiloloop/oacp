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
UPDATE_SCRIPT = str(SCRIPTS_DIR / "update_workspace.sh")


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

    def test_update_stamps_spec_version_and_preserves_orphan(self):
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
            self.assertRegex(updated["spec_version"], r"^\d+\.\d+\.\d+$")
            # The retired field is no longer re-stamped, but an existing
            # value is left in place for readers that have not migrated.
            self.assertEqual(updated["standards_version"], "0.4.0")


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
            expected_keys = {"project_name", "repo_path", "created_at", "updated_at", "spec_version"}
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
