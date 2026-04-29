# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/oacp_doctor.py."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Optional, Sequence, Tuple
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from oacp_doctor import (  # noqa: E402
    DoctorCategory,
    DoctorResult,
    Severity,
    apply_fixes,
    check_agent_status,
    check_environment,
    check_inbox_health,
    check_memory_sync,
    check_schemas,
    check_workspace,
    has_errors,
    print_json,
    print_report,
    run_doctor,
)
from memory_sync import CANONICAL_MEMORY_GITIGNORE  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fake_runner(responses: Optional[dict] = None):
    """Return a runner that returns preset responses by command name."""
    presets = responses or {}

    def runner(command: Sequence[str]) -> Tuple[int, str]:
        name = command[0]
        return presets.get(name, (0, f"{name} 1.0.0"))

    return runner


def _fake_which(available: set):
    """Return a which_fn that finds tools only in the given set."""

    def which_fn(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in available else None

    return which_fn


class TestCheckEnvironment(unittest.TestCase):
    def test_all_tools_available(self) -> None:
        runner = _fake_runner()
        which = _fake_which({"git", "python3", "gh", "ruff", "shellcheck"})
        cat = check_environment(runner=runner, which_fn=which)
        self.assertEqual(cat.name, "Environment")
        # 2 required + 3 optional + pyyaml = 6 results
        self.assertEqual(len(cat.results), 6)
        for r in cat.results:
            if r.name in ("git", "python3", "gh"):
                self.assertEqual(r.severity, Severity.ok, f"{r.name} should be ok")

    def test_missing_required_and_optional_tools(self) -> None:
        runner = _fake_runner()
        which = _fake_which({"python3", "ruff", "shellcheck"})  # missing git (required), gh (optional)
        cat = check_environment(runner=runner, which_fn=which)
        git_result = next(r for r in cat.results if r.name == "git")
        self.assertEqual(git_result.severity, Severity.error)
        # gh is optional — should be skip, not error
        gh_result = next(r for r in cat.results if r.name == "gh")
        self.assertEqual(gh_result.severity, Severity.skip)

    def test_missing_optional_tool(self) -> None:
        runner = _fake_runner()
        which = _fake_which({"git", "python3"})  # missing gh, ruff, shellcheck
        cat = check_environment(runner=runner, which_fn=which)
        gh_result = next(r for r in cat.results if r.name == "gh")
        self.assertEqual(gh_result.severity, Severity.skip)
        ruff_result = next(r for r in cat.results if r.name == "ruff")
        self.assertEqual(ruff_result.severity, Severity.skip)
        sc_result = next(r for r in cat.results if r.name == "shellcheck")
        self.assertEqual(sc_result.severity, Severity.skip)


class TestCheckWorkspace(unittest.TestCase):
    def test_valid_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            _write(project_dir / "workspace.json", '{"name": "test"}')
            (project_dir / "agents").mkdir()
            cat = check_workspace(project_dir)
            self.assertTrue(all(r.severity == Severity.ok for r in cat.results))

    def test_missing_workspace_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            cat = check_workspace(project_dir)
            ws_result = next(r for r in cat.results if r.name == "workspace.json")
            self.assertEqual(ws_result.severity, Severity.error)

    def test_invalid_workspace_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            _write(project_dir / "workspace.json", "not json{{{")
            (project_dir / "agents").mkdir()
            cat = check_workspace(project_dir)
            json_result = next(r for r in cat.results if r.name == "workspace.json")
            self.assertEqual(json_result.severity, Severity.error)

    def test_missing_agents_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            _write(project_dir / "workspace.json", '{"name": "test"}')
            cat = check_workspace(project_dir)
            agents_result = next(r for r in cat.results if r.name == "agents/")
            self.assertEqual(agents_result.severity, Severity.error)


class TestCheckInboxHealth(unittest.TestCase):
    def test_empty_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            (project_dir / "agents" / "claude" / "inbox").mkdir(parents=True)
            cat = check_inbox_health(project_dir)
            result = cat.results[0]
            self.assertEqual(result.severity, Severity.ok)
            self.assertIn("empty", result.message)

    def test_stale_message(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            inbox = project_dir / "agents" / "claude" / "inbox"
            inbox.mkdir(parents=True)
            msg = inbox / "20260214T1200Z_codex_task_request.yaml"
            _write(msg, "id: test\n")
            # Set mtime to 48 hours ago
            old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)).timestamp()
            os.utime(msg, (old_time, old_time))
            cat = check_inbox_health(project_dir)
            result = cat.results[0]
            self.assertEqual(result.severity, Severity.warn)
            self.assertIn("stale", result.message)

    def test_missing_inbox_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            (project_dir / "agents" / "claude").mkdir(parents=True)
            cat = check_inbox_health(project_dir)
            result = cat.results[0]
            self.assertEqual(result.severity, Severity.warn)
            self.assertIn("missing", result.message)


class TestCheckSchemas(unittest.TestCase):
    def test_valid_status_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            agent_dir = project_dir / "agents" / "claude"
            agent_dir.mkdir(parents=True)
            _write(
                agent_dir / "status.yaml",
                "runtime: claude\nstatus: available\ncapabilities:\n  - headless\nupdated_at: '2026-02-16T20:00:00Z'\n",
            )

            import yaml

            cat = check_schemas(project_dir, yaml_loader=yaml.safe_load)
            status_results = [r for r in cat.results if "status.yaml" in r.name]
            self.assertTrue(len(status_results) > 0)
            self.assertEqual(status_results[0].severity, Severity.ok)

    def test_invalid_status_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            agent_dir = project_dir / "agents" / "claude"
            agent_dir.mkdir(parents=True)
            _write(
                agent_dir / "status.yaml",
                "runtime: invalid_runtime\nstatus: available\ncapabilities: []\nupdated_at: '2026-02-16T20:00:00Z'\n",
            )

            import yaml

            cat = check_schemas(project_dir, yaml_loader=yaml.safe_load)
            status_results = [r for r in cat.results if "status.yaml" in r.name]
            self.assertTrue(len(status_results) > 0)
            self.assertEqual(status_results[0].severity, Severity.error)

    def test_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            agent_dir = project_dir / "agents" / "codex"
            agent_dir.mkdir(parents=True)
            _write(
                agent_dir / "status.yaml",
                "runtime: codex\n",
            )

            import yaml

            cat = check_schemas(project_dir, yaml_loader=yaml.safe_load)
            status_results = [r for r in cat.results if "status.yaml" in r.name]
            self.assertTrue(len(status_results) > 0)
            self.assertEqual(status_results[0].severity, Severity.error)
            self.assertIn("missing required field", status_results[0].message)

    def test_skip_when_no_yaml_loader(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            with mock.patch("oacp_doctor._try_yaml_import", return_value=None):
                cat = check_schemas(project_dir, yaml_loader=None)
            self.assertTrue(any(r.severity == Severity.skip for r in cat.results))


class TestCheckAgentStatus(unittest.TestCase):
    def test_present_fresh_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            agent_dir = project_dir / "agents" / "claude"
            agent_dir.mkdir(parents=True)
            now = dt.datetime.now(dt.timezone.utc)
            now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            _write(
                agent_dir / "status.yaml",
                f"runtime: claude\nstatus: available\nupdated_at: '{now_str}'\n",
            )

            import yaml

            cat = check_agent_status(
                project_dir,
                yaml_loader=yaml.safe_load,
                now_fn=lambda: now,
            )
            self.assertEqual(cat.results[0].severity, Severity.ok)

    def test_stale_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            agent_dir = project_dir / "agents" / "claude"
            agent_dir.mkdir(parents=True)
            now = dt.datetime.now(dt.timezone.utc)
            old_time = now - dt.timedelta(hours=5)
            old_str = old_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            _write(
                agent_dir / "status.yaml",
                f"runtime: claude\nstatus: available\nupdated_at: '{old_str}'\n",
            )

            import yaml

            cat = check_agent_status(
                project_dir,
                yaml_loader=yaml.safe_load,
                now_fn=lambda: now,
            )
            self.assertEqual(cat.results[0].severity, Severity.warn)
            self.assertIn("stale", cat.results[0].message)

    def test_missing_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            (project_dir / "agents" / "claude").mkdir(parents=True)
            cat = check_agent_status(project_dir)
            self.assertEqual(cat.results[0].severity, Severity.warn)
            self.assertIn("not found", cat.results[0].message)


class TestSeverityAggregation(unittest.TestCase):
    def test_warn_only_no_errors(self) -> None:
        cats = [
            DoctorCategory(
                name="Test",
                results=[
                    DoctorResult("a", Severity.ok, "ok"),
                    DoctorResult("b", Severity.warn, "warn"),
                ],
            )
        ]
        self.assertFalse(has_errors(cats))

    def test_any_error(self) -> None:
        cats = [
            DoctorCategory(
                name="Test",
                results=[
                    DoctorResult("a", Severity.ok, "ok"),
                    DoctorResult("b", Severity.error, "err"),
                ],
            )
        ]
        self.assertTrue(has_errors(cats))

    def test_all_ok(self) -> None:
        cats = [
            DoctorCategory(
                name="Test",
                results=[DoctorResult("a", Severity.ok, "ok")],
            )
        ]
        self.assertFalse(has_errors(cats))


class TestWorstSeverity(unittest.TestCase):
    def test_empty_category(self) -> None:
        cat = DoctorCategory(name="Empty")
        self.assertEqual(cat.worst_severity, Severity.ok)

    def test_mixed_severities(self) -> None:
        cat = DoctorCategory(
            name="Mixed",
            results=[
                DoctorResult("a", Severity.ok, "ok"),
                DoctorResult("b", Severity.warn, "warn"),
                DoctorResult("c", Severity.error, "err"),
            ],
        )
        self.assertEqual(cat.worst_severity, Severity.error)


class TestJsonOutput(unittest.TestCase):
    def test_json_structure(self) -> None:
        cats = [
            DoctorCategory(
                name="Env",
                results=[
                    DoctorResult("git", Severity.ok, "git 2.47.1"),
                    DoctorResult("ruff", Severity.skip, "not installed", "pip install ruff"),
                ],
            )
        ]
        captured = StringIO()
        with mock.patch("sys.stdout", captured):
            print_json(cats)
        data = json.loads(captured.getvalue())
        self.assertFalse(data["has_errors"])
        self.assertEqual(len(data["categories"]), 1)
        self.assertEqual(data["categories"][0]["name"], "Env")
        self.assertEqual(len(data["categories"][0]["results"]), 2)
        self.assertEqual(data["categories"][0]["results"][0]["severity"], "ok")
        self.assertEqual(data["categories"][0]["results"][1]["severity"], "skip")
        self.assertIn("fix_hint", data["categories"][0]["results"][1])


class TestTextReport(unittest.TestCase):
    def test_print_report_no_color(self) -> None:
        cats = [
            DoctorCategory(
                name="Environment",
                results=[DoctorResult("git", Severity.ok, "git 2.47.1")],
            )
        ]
        captured = StringIO()
        with mock.patch("oacp_doctor._use_color", return_value=False):
            with mock.patch("sys.stdout", captured):
                print_report(cats)
        output = captured.getvalue()
        self.assertIn("[+] Environment", output)
        self.assertIn("[+] git 2.47.1", output)


class TestRunDoctor(unittest.TestCase):
    def test_env_only_mode(self) -> None:
        runner = _fake_runner()
        which = _fake_which({"git", "python3", "gh"})
        cats = run_doctor(
            oacp_dir=Path("/tmp/claude/fake_hub"),
            project=None,
            runner=runner,
            which_fn=which,
        )
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0].name, "Environment")

    def test_missing_project_dir(self) -> None:
        runner = _fake_runner()
        which = _fake_which({"git", "python3", "gh"})
        cats = run_doctor(
            oacp_dir=Path("/tmp/claude/nonexistent_hub"),
            project="noproject",
            runner=runner,
            which_fn=which,
        )
        # Should have Environment + Workspace with error
        self.assertEqual(len(cats), 2)
        self.assertTrue(has_errors(cats))

    def test_full_check_with_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub_dir = Path(td)
            project_dir = hub_dir / "projects" / "testproj"
            project_dir.mkdir(parents=True)
            _write(project_dir / "workspace.json", '{"name": "testproj"}')
            (project_dir / "agents" / "claude" / "inbox").mkdir(parents=True)

            runner = _fake_runner()
            which = _fake_which({"git", "python3", "gh"})
            cats = run_doctor(
                oacp_dir=hub_dir,
                project="testproj",
                runner=runner,
                which_fn=which,
            )
            # Environment + Workspace + Inbox Health + Schemas + Agent Status = 5
            self.assertEqual(len(cats), 5)
            cat_names = [c.name for c in cats]
            self.assertIn("Environment", cat_names)
            self.assertIn("Workspace", cat_names)
            self.assertIn("Inbox Health", cat_names)
            self.assertIn("Schemas", cat_names)
            self.assertIn("Agent Status", cat_names)

    def test_include_memory_adds_memory_sync_category(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub_dir = Path(td)
            runner = _fake_runner()
            which = _fake_which({"git", "python3", "gh"})

            cats = run_doctor(
                oacp_dir=hub_dir,
                project=None,
                include_memory=True,
                runner=runner,
                which_fn=which,
            )

            self.assertEqual(cats[-1].name, "Memory Sync")
            self.assertEqual(cats[-1].results[0].severity, Severity.skip)


class TestCheckMemorySync(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_memory_sync_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cat = check_memory_sync(Path(td))

            self.assertEqual(cat.name, "Memory Sync")
            self.assertEqual(len(cat.results), 1)
            self.assertEqual(cat.results[0].severity, Severity.skip)
            self.assertIn("not configured", cat.results[0].message)

    def test_memory_sync_warns_for_tracked_agent_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(self._git(root, "init").returncode, 0)
            _write(root / ".oacp-memory-repo", "marker\n")
            _write(root / ".gitignore", CANONICAL_MEMORY_GITIGNORE)
            _write(root / "projects" / "demo" / "agents" / "codex" / "status.yaml", "busy\n")
            self.assertEqual(
                self._git(
                    root,
                    "add",
                    "-f",
                    ".oacp-memory-repo",
                    ".gitignore",
                    "projects/demo/agents/codex/status.yaml",
                ).returncode,
                0,
            )

            cat = check_memory_sync(root)
            messages = "\n".join(result.message for result in cat.results)

            self.assertIn("tracked file(s) outside memory allowlist", messages)
            self.assertIn("agents/ file(s) tracked", messages)

    def test_memory_sync_warns_for_escaping_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(self._git(root, "init").returncode, 0)
            _write(root / ".oacp-memory-repo", "marker\n")
            _write(root / ".gitignore", CANONICAL_MEMORY_GITIGNORE)
            _write(root / "projects" / "demo" / "memory" / ".gitignore", "!../agents/**\n")

            cat = check_memory_sync(root)
            overlay = next(result for result in cat.results if result.name == "memory-overlays")

            self.assertEqual(overlay.severity, Severity.warn)
            self.assertIn("escape memory", overlay.message)

    def test_memory_sync_does_not_report_agents_clean_when_ls_files_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root / ".oacp-memory-repo", "marker\n")
            _write(root / ".gitignore", CANONICAL_MEMORY_GITIGNORE)

            def runner(command: Sequence[str]) -> Tuple[int, str]:
                if command[:2] == ["git", "rev-parse"]:
                    if "--verify" in command:
                        return 1, ""
                    return 0, "true"
                if command[:2] == ["git", "status"]:
                    return 0, ""
                if command[:2] == ["git", "remote"]:
                    return 0, ""
                if command[:2] == ["git", "ls-files"] and "--others" not in command:
                    return 1, "boom"
                if command[:2] == ["git", "ls-files"] and "--others" in command:
                    return 0, ""
                return 0, ""

            cat = check_memory_sync(root, runner=runner)
            results = {result.name: result for result in cat.results}

            self.assertEqual(results["tracked-allowlist"].severity, Severity.warn)
            self.assertNotIn("agents-tracked", results)


class TestApplyFixes(unittest.TestCase):
    """Tests for the --fix path."""

    def _make_workspace(self, tmp, agents):
        """Create a minimal workspace with the given agent directories."""
        oacp_dir = Path(tmp)
        project_dir = oacp_dir / "projects" / "test"
        for agent in agents:
            (project_dir / "agents" / agent).mkdir(parents=True)
        # workspace.json
        _write(project_dir / "workspace.json", json.dumps({
            "project_name": "test",
            "agents": agents,
        }))
        # template
        tmpl_dir = oacp_dir / "templates"
        tmpl_dir.mkdir(parents=True, exist_ok=True)
        _write(tmpl_dir / "agent_status.template.yaml", (
            "runtime: claude\n"
            "model: claude-opus-4-6\n"
            "status: available\n"
            'updated_at: "2026-02-16T20:00:00Z"\n'
        ))
        return oacp_dir

    def test_create_status_sets_correct_runtime(self):
        """status.yaml created by --fix should have the agent's runtime, not 'claude'."""
        with tempfile.TemporaryDirectory() as tmp:
            oacp_dir = self._make_workspace(tmp, ["codex"])
            cats = run_doctor(project="test", oacp_dir=oacp_dir)
            fixed = apply_fixes(cats, oacp_dir, "test")

            self.assertTrue(any("codex/status.yaml" in f for f in fixed))

            import yaml
            status_file = oacp_dir / "projects" / "test" / "agents" / "codex" / "status.yaml"
            data = yaml.safe_load(status_file.read_text())
            self.assertEqual(data["runtime"], "codex")

    def test_create_status_unknown_runtime(self):
        """Agents not in VALID_RUNTIMES get runtime: unknown."""
        with tempfile.TemporaryDirectory() as tmp:
            oacp_dir = self._make_workspace(tmp, ["iris"])
            cats = run_doctor(project="test", oacp_dir=oacp_dir)
            fixed = apply_fixes(cats, oacp_dir, "test")

            self.assertTrue(any("iris/status.yaml" in f for f in fixed))

            import yaml
            status_file = oacp_dir / "projects" / "test" / "agents" / "iris" / "status.yaml"
            data = yaml.safe_load(status_file.read_text())
            self.assertEqual(data["runtime"], "unknown")

    def test_create_status_claude_stays_claude(self):
        """Claude agent gets runtime: claude (template default is correct)."""
        with tempfile.TemporaryDirectory() as tmp:
            oacp_dir = self._make_workspace(tmp, ["claude"])
            cats = run_doctor(project="test", oacp_dir=oacp_dir)
            apply_fixes(cats, oacp_dir, "test")

            import yaml
            status_file = oacp_dir / "projects" / "test" / "agents" / "claude" / "status.yaml"
            data = yaml.safe_load(status_file.read_text())
            self.assertEqual(data["runtime"], "claude")

    def test_fix_missing_inbox(self):
        """--fix creates missing inbox directories."""
        with tempfile.TemporaryDirectory() as tmp:
            oacp_dir = Path(tmp)
            project_dir = oacp_dir / "projects" / "test"
            # Create agent dir without inbox
            (project_dir / "agents" / "claude").mkdir(parents=True)
            _write(project_dir / "workspace.json", json.dumps({
                "project_name": "test", "agents": ["claude"],
            }))

            cats = run_doctor(project="test", oacp_dir=oacp_dir)
            fixed = apply_fixes(cats, oacp_dir, "test")

            self.assertTrue(any("inbox" in f for f in fixed))
            self.assertTrue((project_dir / "agents" / "claude" / "inbox").is_dir())

    def test_fix_stale_status(self):
        """--fix updates stale status.yaml timestamps."""
        with tempfile.TemporaryDirectory() as tmp:
            oacp_dir = Path(tmp)
            project_dir = oacp_dir / "projects" / "test"
            agent_dir = project_dir / "agents" / "claude"
            (agent_dir / "inbox").mkdir(parents=True)
            _write(agent_dir / "status.yaml", (
                "runtime: claude\n"
                "status: available\n"
                'updated_at: "2020-01-01T00:00:00Z"\n'
            ))
            _write(project_dir / "workspace.json", json.dumps({
                "project_name": "test", "agents": ["claude"],
            }))

            cats = run_doctor(project="test", oacp_dir=oacp_dir)
            fixed = apply_fixes(cats, oacp_dir, "test")

            self.assertTrue(any("timestamp" in f for f in fixed))
            import yaml
            data = yaml.safe_load((agent_dir / "status.yaml").read_text())
            # Should be recent, not 2020
            self.assertNotIn("2020", data["updated_at"])

    def test_fix_idempotent(self):
        """Running --fix twice produces no fixes on the second run."""
        with tempfile.TemporaryDirectory() as tmp:
            oacp_dir = self._make_workspace(tmp, ["codex"])
            cats = run_doctor(project="test", oacp_dir=oacp_dir)
            fixed1 = apply_fixes(cats, oacp_dir, "test")
            self.assertTrue(len(fixed1) > 0)

            # Second run
            cats2 = run_doctor(project="test", oacp_dir=oacp_dir)
            fixed2 = apply_fixes(cats2, oacp_dir, "test")
            self.assertEqual(len(fixed2), 0)


if __name__ == "__main__":
    unittest.main()
