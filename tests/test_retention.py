# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for project-wide message retention."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from oacp import cli  # noqa: E402
from retention import (  # noqa: E402
    DEFAULT_RETENTION_POLICY,
    FileSnapshot,
    _remove_if_unchanged,
    main,
    resolve_retention_policy,
    run_retention,
)


NOW = dt.datetime(2026, 8, 10, 21, 0, tzinfo=dt.timezone.utc)


class RetentionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.oacp_root = Path(self._tmp.name)
        self.project_dir = self.oacp_root / "projects" / "demo"
        self.agent_dir = self.project_dir / "agents" / "codex"
        for relative in ("outbox", "dead_letter", "inbox/archive"):
            directory = self.agent_dir / relative
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ".gitkeep").touch()
        self._write_workspace()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_workspace(self, retention=None) -> None:
        workspace = {"project_name": "demo", "spec_version": "0.4.3"}
        if retention is not None:
            workspace["retention"] = retention
        (self.project_dir / "workspace.json").write_text(
            json.dumps(workspace), encoding="utf-8"
        )

    def _write_at(self, relative: str, *, days_old: int, body: str = "x") -> Path:
        path = self.agent_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        timestamp = (NOW - dt.timedelta(days=days_old)).timestamp()
        os.utime(path, (timestamp, timestamp))
        return path

    def test_missing_config_uses_protocol_defaults(self) -> None:
        policy = resolve_retention_policy({"project_name": "demo"})
        self.assertEqual(policy, DEFAULT_RETENTION_POLICY)
        self.assertIsNot(policy, DEFAULT_RETENTION_POLICY)

    def test_partial_override_deep_merges_and_null_disables_dimension(self) -> None:
        policy = resolve_retention_policy(
            {
                "retention": {
                    "outbox": {"max_age_days": 7, "max_count": None}
                }
            }
        )
        self.assertEqual(policy["outbox"], {"max_age_days": 7, "max_count": None})
        self.assertEqual(policy["dead_letter"], DEFAULT_RETENTION_POLICY["dead_letter"])

    def test_invalid_override_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer or null"):
            resolve_retention_policy(
                {"retention": {"outbox": {"max_age_days": 0}}}
            )
        with self.assertRaisesRegex(ValueError, "unknown target"):
            resolve_retention_policy({"retention": {"logs": {}}})

    def test_prunes_outbox_by_age(self) -> None:
        self._write_workspace(
            {"outbox": {"max_age_days": 5, "max_count": None}}
        )
        old = self._write_at("outbox/old.yaml", days_old=6)
        recent = self._write_at("outbox/recent.yaml", days_old=1)

        report = run_retention("demo", oacp_root=self.oacp_root, now=NOW)

        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(report["pruned_count"], 1)
        self.assertEqual(report["actions"][0]["reasons"], ["age"])

    def test_prunes_oldest_outbox_files_by_count(self) -> None:
        self._write_workspace(
            {"outbox": {"max_age_days": None, "max_count": 2}}
        )
        files = [
            self._write_at(f"outbox/{index}.yaml", days_old=4 - index)
            for index in range(4)
        ]

        report = run_retention("demo", oacp_root=self.oacp_root, now=NOW)

        self.assertFalse(files[0].exists())
        self.assertFalse(files[1].exists())
        self.assertTrue(files[2].exists())
        self.assertTrue(files[3].exists())
        self.assertEqual(report["pruned_count"], 2)
        self.assertTrue(
            all("count" in action["reasons"] for action in report["actions"])
        )

    def test_quarantine_seed_and_marked_fixture_are_never_pruned(self) -> None:
        self._write_workspace(
            {"dead_letter": {"max_age_days": 1, "max_count": 1}}
        )
        seed = self._write_at(
            "dead_letter/"
            "20260807050407_iris_brainstorm_request_6055.yaml."
            "bb0c48f6.20260807T050525Z",
            days_old=90,
        )
        marked = self._write_at("dead_letter/manual-fixture.yaml", days_old=90)
        marker = self._write_at(
            "dead_letter/manual-fixture.yaml.retain", days_old=90, body=""
        )
        suffixed = self._write_at(
            "dead_letter/manual-fixture.yaml.quarantine", days_old=90
        )
        ordinary = self._write_at("dead_letter/stale.tmp", days_old=2)

        report = run_retention("demo", oacp_root=self.oacp_root, now=NOW)

        self.assertTrue(seed.exists())
        self.assertTrue(marked.exists())
        self.assertTrue(marker.exists())
        self.assertTrue(suffixed.exists())
        self.assertFalse(ordinary.exists())
        self.assertEqual(report["protected_count"], 3)
        self.assertEqual(report["pruned_count"], 1)

    def test_prunes_processed_inbox_archive(self) -> None:
        self._write_workspace(
            {"inbox_archive": {"max_age_days": 3, "max_count": None}}
        )
        archived = self._write_at("inbox/archive/processed.yaml", days_old=4)

        report = run_retention("demo", oacp_root=self.oacp_root, now=NOW)

        self.assertFalse(archived.exists())
        self.assertEqual(report["actions"][0]["target"], "inbox_archive")

    def test_dry_run_reports_without_deleting(self) -> None:
        self._write_workspace(
            {"outbox": {"max_age_days": 1, "max_count": None}}
        )
        old = self._write_at("outbox/old.yaml", days_old=2)

        report = run_retention(
            "demo", oacp_root=self.oacp_root, dry_run=True, now=NOW
        )

        self.assertTrue(old.exists())
        self.assertEqual(report["planned_count"], 1)
        self.assertEqual(report["pruned_count"], 0)
        self.assertEqual(report["actions"][0]["status"], "would_prune")

    def test_pre_unlink_identity_recheck_skips_changed_file(self) -> None:
        path = self._write_at("outbox/old.yaml", days_old=90)
        metadata = path.lstat()
        snapshot = FileSnapshot(
            path=path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mtime_ns=metadata.st_mtime_ns,
            size=metadata.st_size,
        )
        path.write_text("replacement", encoding="utf-8")

        status, error = _remove_if_unchanged(snapshot)

        self.assertEqual(status, "skipped_changed")
        self.assertIsNone(error)
        self.assertTrue(path.exists())

    def test_missing_candidate_has_dedicated_counter(self) -> None:
        self._write_workspace(
            {"outbox": {"max_age_days": 1, "max_count": None}}
        )
        self._write_at("outbox/old.yaml", days_old=2)

        with mock.patch(
            "retention._remove_if_unchanged",
            return_value=("skipped_missing", None),
        ):
            report = run_retention("demo", oacp_root=self.oacp_root, now=NOW)

        self.assertEqual(report["skipped_changed_count"], 0)
        self.assertEqual(report["skipped_missing_count"], 1)

    def test_symlinked_agent_and_target_directories_are_ignored(self) -> None:
        outside_target = self.oacp_root / "outside-target"
        outside_target.mkdir()
        target_file = outside_target / "old.yaml"
        target_file.write_text("outside", encoding="utf-8")
        old_timestamp = (NOW - dt.timedelta(days=90)).timestamp()
        os.utime(target_file, (old_timestamp, old_timestamp))

        shutil.rmtree(self.agent_dir / "outbox")
        (self.agent_dir / "outbox").symlink_to(
            outside_target,
            target_is_directory=True,
        )

        outside_agent = self.oacp_root / "outside-agent"
        outside_dead_letter = outside_agent / "dead_letter"
        outside_dead_letter.mkdir(parents=True)
        agent_file = outside_dead_letter / "old.yaml"
        agent_file.write_text("outside", encoding="utf-8")
        os.utime(agent_file, (old_timestamp, old_timestamp))
        (self.project_dir / "agents" / "linked").symlink_to(
            outside_agent,
            target_is_directory=True,
        )

        report = run_retention("demo", oacp_root=self.oacp_root, now=NOW)

        self.assertTrue(target_file.exists())
        self.assertTrue(agent_file.exists())
        self.assertEqual(report["planned_count"], 0)

    def test_clean_tree_is_noop(self) -> None:
        report = run_retention("demo", oacp_root=self.oacp_root, now=NOW)
        self.assertEqual(report["scanned_count"], 0)
        self.assertEqual(report["planned_count"], 0)
        self.assertEqual(report["actions"], [])

    def test_cli_json_noop_and_dispatch_registration(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "demo",
                    "--oacp-dir",
                    str(self.oacp_root),
                    "--dry-run",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["actions"], [])
        self.assertEqual(cli.SCRIPT_NAMES["retention"], "retention.py")
        self.assertIn("retention", cli.HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
