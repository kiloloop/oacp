# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/codex_session_init.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from codex_session_init import _build_parser, run_session_init  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_protocol(repo_dir: Path) -> Path:
    protocol_dir = repo_dir / "docs" / "protocol"
    _write(protocol_dir / "agent_safety_defaults.md", "# safety\n")
    _write(protocol_dir / "dispatch_states.yaml", "version: 1\n")
    _write(protocol_dir / "session_init.md", "# init\n")
    return protocol_dir


def _seed_memory(hub_dir: Path, project: str) -> Path:
    project_dir = hub_dir / "projects" / project
    _write(project_dir / "memory" / "project_facts.md", "# facts\n")
    _write(project_dir / "memory" / "decision_log.md", "# decisions\n")
    _write(project_dir / "memory" / "open_threads.md", "# threads\n")
    _write(project_dir / "memory" / "known_debt.md", "# debt\n")
    return project_dir


class TestCodexSessionInit(unittest.TestCase):
    def test_autodetect_project_from_agent_hub_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)
            _write(repo_dir / ".agent-hub", json.dumps({"project_name": "demo"}))

            report = run_session_init(
                project=None,
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="gpt-test",
                status="available",
                current_task="",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["project"], "demo")
            self.assertEqual(report["protocol"]["agent_safety_defaults.md"]["state"], "loaded")
            self.assertEqual(report["memory"]["project_facts.md"]["state"], "loaded")
            self.assertEqual(report["memory"]["known_debt.md"]["state"], "loaded")
            self.assertEqual(report["status_yaml"]["state"], "created")
            self.assertIn("project=demo", report["ack"])
            self.assertIn("status_yaml=created", report["ack"])

            status_path = project_dir / "agents" / "codex" / "status.yaml"
            self.assertTrue(status_path.exists())
            raw = status_path.read_text(encoding="utf-8")
            self.assertIn("runtime: codex", raw)
            self.assertIn("model: gpt-test", raw)

    def test_autodetect_project_from_agent_hub_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / ".agent-hub").symlink_to(project_dir)

            report = run_session_init(
                project=None,
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="gpt-test",
                status="available",
                current_task="",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["project"], "demo")
            self.assertEqual(report["status_yaml"]["state"], "created")

    def test_autodetect_project_from_workspace_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)
            _write(repo_dir / "workspace.json", json.dumps({"project_name": "demo"}))

            report = run_session_init(
                project=None,
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="gpt-test",
                status="available",
                current_task="",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["project"], "demo")
            self.assertEqual(report["status_yaml"]["state"], "created")

    def test_autodetect_project_from_oacp_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)
            workspace = project_dir / "workspace.json"
            _write(workspace, json.dumps({"project_name": "demo"}))
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / ".oacp").symlink_to(workspace)

            report = run_session_init(
                project=None,
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="gpt-test",
                status="available",
                current_task="",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["project"], "demo")
            self.assertEqual(report["status_yaml"]["state"], "created")

    def test_missing_memory_files_warns_but_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = hub_dir / "projects" / "demo"
            protocol_dir = _seed_protocol(repo_dir)
            _write(project_dir / "memory" / "project_facts.md", "# facts\n")

            report = run_session_init(
                project="demo",
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="gpt-test",
                status="available",
                current_task="",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["memory"]["project_facts.md"]["state"], "loaded")
            self.assertEqual(report["memory"]["decision_log.md"]["state"], "missing")
            self.assertEqual(report["memory"]["open_threads.md"]["state"], "missing")
            self.assertEqual(report["memory"]["known_debt.md"]["state"], "missing")
            self.assertTrue(
                any("memory file decision_log.md: missing" in w for w in report["warnings"])
            )
            self.assertTrue(
                any("memory file known_debt.md: missing" in w for w in report["warnings"])
            )
            self.assertEqual(report["status_yaml"]["state"], "created")

    def test_updates_existing_status_and_preserves_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)

            status_path = project_dir / "agents" / "codex" / "status.yaml"
            _write(
                status_path,
                "\n".join(
                    [
                        "runtime: codex",
                        "model: old-model",
                        "status: offline",
                        'current_task: ""',
                        "capabilities:",
                        "  - headless",
                        "  - shell_access",
                        'updated_at: "2026-02-01T00:00:00Z"',
                        "",
                    ]
                ),
            )

            report = run_session_init(
                project="demo",
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="codex-latest",
                status="busy",
                current_task="demo-project#12",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["status_yaml"]["state"], "updated")
            self.assertTrue(report["status_yaml"]["used_existing_capabilities"])
            payload = yaml.safe_load(status_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["model"], "codex-latest")
            self.assertEqual(payload["status"], "busy")
            self.assertEqual(payload["current_task"], "demo-project#12")
            self.assertIn("headless", payload["capabilities"])
            self.assertIn("shell_access", payload["capabilities"])

    def test_no_project_detected_skips_memory_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            protocol_dir = _seed_protocol(repo_dir)

            report = run_session_init(
                project=None,
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="gpt-test",
                status="available",
                current_task="",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["project"], "")
            self.assertEqual(report["memory"]["project_facts.md"]["state"], "skipped")
            self.assertEqual(report["memory"]["known_debt.md"]["state"], "skipped")
            self.assertEqual(report["status_yaml"]["state"], "no-project")
            self.assertIn("project=none", report["ack"])

    def test_dry_run_does_not_write_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)

            report = run_session_init(
                project="demo",
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="gpt-test",
                status="available",
                current_task="",
                dry_run=True,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["status_yaml"]["state"], "dry-run")
            status_path = project_dir / "agents" / "codex" / "status.yaml"
            self.assertFalse(status_path.exists())

    def test_status_yaml_handles_special_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)

            run_session_init(
                project="demo",
                hub_dir=hub_dir,
                cwd=repo_dir,
                model='gpt-4: turbo',
                status="offline",
                current_task='task "quoted"',
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            status_path = project_dir / "agents" / "codex" / "status.yaml"
            payload = yaml.safe_load(status_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["model"], "gpt-4: turbo")
            self.assertEqual(payload["status"], "offline")
            self.assertEqual(payload["current_task"], 'task "quoted"')
            self.assertNotIn("browser", payload["capabilities"])

    def test_parser_accepts_offline_status(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--status", "offline"])
        self.assertEqual(args.status, "offline")

    def test_archive_dir_is_not_loaded_during_session_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)
            _write(project_dir / "memory" / "archive" / "20260320T000000Z_notes.md", "# archived\n")

            report = run_session_init(
                project="demo",
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="gpt-test",
                status="available",
                current_task="",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertNotIn("archive", report["memory"])
            self.assertNotIn("20260320T000000Z_notes.md", report["memory"])


if __name__ == "__main__":
    unittest.main()
