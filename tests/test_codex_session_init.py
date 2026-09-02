# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/codex_session_init.py."""

from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import codex_session_init as session_init  # noqa: E402
from _oacp_constants import CODEX_SESSION_START_CONTEXT_LIMIT  # noqa: E402
from codex_session_init import (  # noqa: E402
    HOOK_CONTEXT_CHAR_LIMIT,
    _build_parser,
    build_session_start_hook_output,
    run_session_init,
)


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
    def test_autodetect_project_from_oacp_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)
            _write(repo_dir / ".oacp", json.dumps({"project_name": "demo"}))

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
            self.assertEqual(
                report["protocol"]["agent_safety_defaults.md"]["state"], "verified"
            )
            self.assertEqual(report["memory"]["project_facts.md"]["state"], "verified")
            self.assertEqual(report["memory"]["known_debt.md"]["state"], "verified")
            self.assertEqual(report["status_yaml"]["state"], "created")
            self.assertIn("project=demo", report["ack"])
            self.assertIn("status_yaml=created", report["ack"])
            self.assertIn("context=manifest", report["ack"])

            status_path = project_dir / "agents" / "codex" / "status.yaml"
            self.assertTrue(status_path.exists())
            raw = status_path.read_text(encoding="utf-8")
            self.assertIn("runtime: codex", raw)
            self.assertIn("model: gpt-test", raw)

    def test_agent_hub_marker_is_not_used(self) -> None:
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

            self.assertEqual(report["project"], "")
            self.assertEqual(report["status_yaml"]["state"], "no-project")
            self.assertIn("workspace.json or .oacp", report["warnings"][0])

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

            self.assertEqual(report["memory"]["project_facts.md"]["state"], "verified")
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

    def test_rendered_status_round_trips_capabilities(self) -> None:
        capabilities = ["headless", "custom_capability"]
        rendered = session_init._render_status_yaml(
            model="gpt-test",
            status="available",
            current_task="",
            capabilities=capabilities,
        )

        self.assertEqual(
            session_init._parse_capabilities_from_status(rendered),
            capabilities,
        )

    def test_valid_empty_capability_list_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.yaml"
            status_path.write_text(
                session_init._render_status_yaml(
                    model="old-model",
                    status="available",
                    current_task="",
                    capabilities=[],
                ),
                encoding="utf-8",
            )

            result = session_init._upsert_status_yaml(
                status_path=status_path,
                model="gpt-test",
                status="busy",
                current_task="demo#1",
                dry_run=False,
            )

            payload = yaml.safe_load(status_path.read_text(encoding="utf-8"))
            self.assertTrue(result["used_existing_capabilities"])
            self.assertEqual(payload["capabilities"], [])

    def test_present_invalid_capabilities_refuses_rewrite(self) -> None:
        cases = {
            "non-list-value": "runtime: codex\ncapabilities: not-a-list\n",
            "invalid-list-item": (
                "runtime: codex\ncapabilities:\n  - ok_cap\n  - 'bad key!'\n"
            ),
            "non-mapping-file": "- a list, not a mapping\n",
        }
        for name, body in cases.items():
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as tmp:
                    status_path = Path(tmp) / "status.yaml"
                    status_path.write_text(body, encoding="utf-8")
                    before = status_path.read_bytes()

                    result = session_init._upsert_status_yaml(
                        status_path=status_path,
                        model="gpt-test",
                        status="busy",
                        current_task="demo#1",
                        dry_run=False,
                    )

                    self.assertEqual(result["state"], "error")
                    self.assertFalse(result["used_existing_capabilities"])
                    self.assertIn("left unchanged", result["warning"])
                    self.assertEqual(status_path.read_bytes(), before)

    def test_session_init_surfaces_invalid_status_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            project_dir = _seed_memory(hub_dir, "demo")
            protocol_dir = _seed_protocol(repo_dir)

            status_path = project_dir / "agents" / "codex" / "status.yaml"
            _write(status_path, "runtime: codex\ncapabilities: not-a-list\n")
            before = status_path.read_bytes()

            report = run_session_init(
                project="demo",
                hub_dir=hub_dir,
                cwd=repo_dir,
                model="codex-latest",
                status="busy",
                current_task="demo#12",
                dry_run=False,
                protocol_dir=protocol_dir,
            )

            self.assertEqual(report["status_yaml"]["state"], "error")
            self.assertIn("status_yaml=error", report["ack"])
            self.assertTrue(
                any("left unchanged" in warning for warning in report["warnings"])
            )
            self.assertEqual(status_path.read_bytes(), before)

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

    def test_parser_accepts_hook_and_pull_memory(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--hook", "--pull-memory"])
        self.assertTrue(args.hook)
        self.assertTrue(args.pull_memory)

    def test_hook_output_is_bounded_and_truthful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            _seed_memory(hub_dir, "demo")
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
            output = build_session_start_hook_output(
                report,
                memory_sync={"state": "ok", "messages": ["already synced"]},
            )

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(
                output["hookSpecificOutput"]["hookEventName"], "SessionStart"
            )
            self.assertLessEqual(len(context), HOOK_CONTEXT_CHAR_LIMIT)
            self.assertIn("verified readability", context)
            self.assertIn("did not inject the full file contents", context)
            self.assertIn("Verified inputs: 7 files", context)
            self.assertIn("agent_safety_defaults.md", context)
            self.assertIn("known_debt.md", context)
            self.assertNotIn("# safety", context)
            self.assertNotIn("# facts", context)
            self.assertIn("SESSION_INIT_ACK: project=demo", context)
            ordered_names = [*session_init.PROTOCOL_FILES, *session_init.MEMORY_FILES]
            positions = [context.index(name) for name in ordered_names]
            self.assertEqual(positions, sorted(positions))

    def test_hook_context_uses_registered_limit_and_reports_truncation(self) -> None:
        self.assertEqual(HOOK_CONTEXT_CHAR_LIMIT, CODEX_SESSION_START_CONTEXT_LIMIT)
        context = session_init._bounded_hook_context(
            "x" * (CODEX_SESSION_START_CONTEXT_LIMIT + 1)
        )

        self.assertEqual(len(context), CODEX_SESSION_START_CONTEXT_LIMIT)
        self.assertIn("OACP startup context truncated", context)

    def test_hook_mode_runs_pull_before_verification(self) -> None:
        call_order = []
        hook_payload = {
            "hook_event_name": "SessionStart",
            "cwd": "/tmp",
            "model": "gpt-test",
            "source": "startup",
        }
        report = {
            "project": "demo",
            "hub_dir": "/tmp/oacp",
            "protocol": {
                name: {
                    "path": f"/tmp/protocol/{name}",
                    "state": "verified",
                    "bytes": 1,
                }
                for name in session_init.PROTOCOL_FILES
            },
            "memory": {
                name: {
                    "path": f"/tmp/memory/{name}",
                    "state": "verified",
                    "bytes": 1,
                }
                for name in session_init.MEMORY_FILES
            },
            "status_yaml": {"state": "updated"},
            "warnings": [],
            "ack": "project=demo;context=manifest",
        }

        def fake_pull(**kwargs):
            call_order.append("pull")
            return {"state": "ok", "messages": []}

        def fake_init(**kwargs):
            call_order.append("verify")
            self.assertEqual(kwargs["model"], "gpt-test")
            return report

        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["codex_session_init.py", "--hook", "--pull-memory"]), mock.patch.object(
            sys, "stdin", io.StringIO(json.dumps(hook_payload))
        ), mock.patch.object(sys, "stdout", stdout), mock.patch.object(
            session_init, "_pull_memory_report", side_effect=fake_pull
        ), mock.patch.object(session_init, "run_session_init", side_effect=fake_init):
            code = session_init.main()

        self.assertEqual(code, 0)
        self.assertEqual(call_order, ["pull", "verify"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )

    def test_hook_mode_records_unknown_when_model_is_missing(self) -> None:
        hook_payload = {
            "hook_event_name": "SessionStart",
            "cwd": "/tmp",
            "source": "startup",
        }
        report = {
            "project": "demo",
            "hub_dir": "/tmp/oacp",
            "protocol": {
                name: {"path": f"/tmp/protocol/{name}", "state": "verified", "bytes": 1}
                for name in session_init.PROTOCOL_FILES
            },
            "memory": {
                name: {"path": f"/tmp/memory/{name}", "state": "verified", "bytes": 1}
                for name in session_init.MEMORY_FILES
            },
            "status_yaml": {"state": "updated"},
            "warnings": [],
            "ack": "project=demo;context=manifest",
        }

        def fake_init(**kwargs):
            self.assertEqual(kwargs["model"], "unknown")
            return report

        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["codex_session_init.py", "--hook"]),
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook_payload))),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(session_init, "run_session_init", side_effect=fake_init),
        ):
            code = session_init.main()

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIn("systemMessage", payload)
        self.assertIn(
            "status.yaml records model: unknown",
            payload["hookSpecificOutput"]["additionalContext"],
        )

    def test_invalid_hook_input_degrades_without_blocking(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["codex_session_init.py", "--hook"]), mock.patch.object(
            sys, "stdin", io.StringIO("not-json")
        ), mock.patch.object(sys, "stdout", stdout):
            code = session_init.main()

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["continue"])
        self.assertIn("did not run", payload["hookSpecificOutput"]["additionalContext"])

    def test_hook_runtime_error_degrades_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_dir = root / "oacp"
            repo_dir = root / "repo"
            _seed_memory(hub_dir, "demo")
            _seed_protocol(repo_dir)
            hook_payload = {
                "hook_event_name": "SessionStart",
                "cwd": str(repo_dir),
                "model": "gpt-test",
                "source": "startup",
            }
            stdout = io.StringIO()
            argv = [
                "codex_session_init.py",
                "--hook",
                "--project",
                "demo",
                "--hub-dir",
                str(hub_dir),
            ]

            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                sys, "stdin", io.StringIO(json.dumps(hook_payload))
            ), mock.patch.object(sys, "stdout", stdout), mock.patch.object(
                session_init,
                "_upsert_status_yaml",
                side_effect=PermissionError("status.yaml is read-only"),
            ):
                code = session_init.main()

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["continue"])
        self.assertIn("degraded mode", payload["systemMessage"])
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("PermissionError", context)
        self.assertIn("session-init --pull-memory", context)

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
