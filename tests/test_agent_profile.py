# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/agent_profile.py — two-tier agent profile management."""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import yaml  # noqa: E402
from agent_profile import (  # noqa: E402
    cmd_init,
    cmd_list,
    cmd_show,
    load_global_profile,
    load_project_card,
    merge_profiles,
    resolve_agent_profile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    """Write a dict as YAML to the given path, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _global_profile(oacp_root: Path, name: str, data: dict) -> None:
    """Write a global profile at oacp_root/agents/<name>/profile.yaml."""
    _write_yaml(oacp_root / "agents" / name / "profile.yaml", data)


def _project_card(oacp_root: Path, project: str, name: str, data: dict) -> None:
    """Write a project agent card at oacp_root/projects/<project>/agents/<name>/agent_card.yaml."""
    _write_yaml(
        oacp_root / "projects" / project / "agents" / name / "agent_card.yaml",
        data,
    )


def _make_args(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace from keyword arguments."""
    return argparse.Namespace(**kwargs)


def _capture_stdout(fn, *args, **kwargs):
    """Call fn and return (return_value, captured_stdout)."""
    buf = StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rv = fn(*args, **kwargs)
    finally:
        sys.stdout = old
    return rv, buf.getvalue()


def _capture_stderr(fn, *args, **kwargs):
    """Call fn and return (return_value, captured_stderr)."""
    buf = StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        rv = fn(*args, **kwargs)
    finally:
        sys.stderr = old
    return rv, buf.getvalue()


# ---------------------------------------------------------------------------
# Tests: merge_profiles
# ---------------------------------------------------------------------------


class TestMergeProfiles(unittest.TestCase):
    """Tests for the merge_profiles function."""

    def test_scalar_override(self) -> None:
        """Project scalar values (name, runtime) override global."""
        global_data = {"name": "claude", "runtime": "claude", "model": "opus-3"}
        project_data = {"name": "claude-proj", "runtime": "claude", "model": "opus-4"}
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(merged["name"], "claude-proj")
        self.assertEqual(merged["model"], "opus-4")

    def test_empty_field_keeps_global(self) -> None:
        """Empty string in project keeps the global value."""
        global_data = {"name": "claude", "runtime": "claude", "model": "opus-3"}
        project_data = {"name": "", "model": "  "}
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(merged["name"], "claude")
        self.assertEqual(merged["model"], "opus-3")

    def test_none_field_keeps_global(self) -> None:
        """None in project keeps the global value."""
        global_data = {"name": "claude", "trust_level": "elevated"}
        project_data = {"name": None, "trust_level": None}
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(merged["name"], "claude")
        self.assertEqual(merged["trust_level"], "elevated")

    def test_dict_merge(self) -> None:
        """Capabilities dict merge: project keys override, global-only keys preserved."""
        global_data = {
            "capabilities": {
                "tools": ["Bash", "Read"],
                "languages": ["python"],
                "domains": ["backend"],
            }
        }
        project_data = {
            "capabilities": {
                "tools": ["Bash", "Read", "Edit", "Write"],
                "domains": ["frontend"],
            }
        }
        merged = merge_profiles(global_data, project_data)
        # tools is a list subfield — project replaces global
        self.assertEqual(merged["capabilities"]["tools"], ["Bash", "Read", "Edit", "Write"])
        # domains is a list subfield — project replaces global
        self.assertEqual(merged["capabilities"]["domains"], ["frontend"])
        # languages only in global — preserved
        self.assertEqual(merged["capabilities"]["languages"], ["python"])

    def test_list_replacement(self) -> None:
        """Skills list in project replaces global entirely."""
        global_data = {
            "skills": [
                {"id": "review", "name": "Review", "description": "Reviews code"},
                {"id": "impl", "name": "Implementation", "description": "Implements"},
            ]
        }
        project_data = {
            "skills": [
                {"id": "deploy", "name": "Deploy", "description": "Deploys stuff"},
            ]
        }
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(len(merged["skills"]), 1)
        self.assertEqual(merged["skills"][0]["id"], "deploy")

    def test_missing_sections_use_global(self) -> None:
        """Sections only in global are preserved in merged output."""
        global_data = {
            "name": "claude",
            "trust_level": "elevated",
            "quota": {"max_cost_usd_per_month": 100, "reset_day": 1},
            "routing_rules": {"primary": ["codex"], "avoid": []},
        }
        project_data = {"name": "claude-proj"}
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(merged["trust_level"], "elevated")
        self.assertEqual(merged["quota"]["max_cost_usd_per_month"], 100)
        self.assertEqual(merged["routing_rules"]["primary"], ["codex"])

    def test_list_subfield_replacement(self) -> None:
        """capabilities.tools in project replaces global list entirely."""
        global_data = {"capabilities": {"tools": ["Bash", "Read"]}}
        project_data = {"capabilities": {"tools": ["Edit"]}}
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(merged["capabilities"]["tools"], ["Edit"])

    def test_routing_rules_list_subfield_replacement(self) -> None:
        """routing_rules.primary in project replaces global list."""
        global_data = {"routing_rules": {"primary": ["codex"], "avoid": ["gemini"]}}
        project_data = {"routing_rules": {"primary": ["iris"]}}
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(merged["routing_rules"]["primary"], ["iris"])
        # avoid not in project — kept from global
        self.assertEqual(merged["routing_rules"]["avoid"], ["gemini"])

    def test_empty_list_does_not_override_global(self) -> None:
        """Empty lists in project card (template defaults) should not mask global values."""
        global_data = {
            "capabilities": {"tools": ["Bash", "Read"], "languages": ["python"]},
            "routing_rules": {"primary": ["codex"], "avoid": ["gemini"]},
            "skills": [{"id": "review", "name": "Review", "description": "Reviews"}],
        }
        project_data = {
            "capabilities": {"tools": [], "languages": []},
            "routing_rules": {"primary": [], "avoid": []},
            "skills": [],
        }
        merged = merge_profiles(global_data, project_data)
        # Empty project lists should NOT replace global values
        self.assertEqual(merged["capabilities"]["tools"], ["Bash", "Read"])
        self.assertEqual(merged["capabilities"]["languages"], ["python"])
        self.assertEqual(merged["routing_rules"]["primary"], ["codex"])
        self.assertEqual(merged["routing_rules"]["avoid"], ["gemini"])
        self.assertEqual(len(merged["skills"]), 1)

    def test_empty_project_returns_global(self) -> None:
        """Empty project data returns a copy of global."""
        global_data = {"name": "claude", "runtime": "claude"}
        merged = merge_profiles(global_data, {})
        self.assertEqual(merged, global_data)

    def test_deep_copy_isolation(self) -> None:
        """Merged result does not share references with inputs."""
        global_data = {"capabilities": {"tools": ["Bash"]}}
        project_data = {"capabilities": {"languages": ["python"]}}
        merged = merge_profiles(global_data, project_data)
        # Mutating merged should not affect inputs
        merged["capabilities"]["tools"].append("Edit")
        self.assertEqual(global_data["capabilities"]["tools"], ["Bash"])

    def test_new_project_key_added(self) -> None:
        """Project can introduce a key not present in global."""
        global_data = {"name": "claude"}
        project_data = {"description": "Project-specific agent"}
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(merged["description"], "Project-specific agent")
        self.assertEqual(merged["name"], "claude")

    def test_empty_dict_subfield_keeps_global(self) -> None:
        """Empty string in a dict sub-field keeps the global value."""
        global_data = {"capabilities": {"tools": ["Bash"], "custom_key": "important"}}
        project_data = {"capabilities": {"custom_key": ""}}
        merged = merge_profiles(global_data, project_data)
        self.assertEqual(merged["capabilities"]["custom_key"], "important")


# ---------------------------------------------------------------------------
# Tests: load_global_profile / load_project_card / resolve_agent_profile
# ---------------------------------------------------------------------------


class TestLoadGlobalProfile(unittest.TestCase):
    def test_loads_existing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data = {"name": "claude", "runtime": "claude"}
            _global_profile(root, "claude", data)
            result = load_global_profile(root, "claude")
            self.assertIsNotNone(result)
            self.assertEqual(result["name"], "claude")

    def test_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = load_global_profile(root, "nonexistent")
            self.assertIsNone(result)


class TestLoadProjectCard(unittest.TestCase):
    def test_loads_existing_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data = {"name": "claude", "model": "opus-4"}
            _project_card(root, "demo", "claude", data)
            result = load_project_card(root, "demo", "claude")
            self.assertIsNotNone(result)
            self.assertEqual(result["model"], "opus-4")

    def test_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = load_project_card(root, "demo", "nonexistent")
            self.assertIsNone(result)


class TestResolveAgentProfile(unittest.TestCase):
    def test_global_only_no_project(self) -> None:
        """Without a project arg, returns global profile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {"name": "claude", "runtime": "claude"})
            result = resolve_agent_profile(root, "claude")
            self.assertEqual(result["name"], "claude")

    def test_merged_when_both_exist(self) -> None:
        """Returns merged profile when both global and project card exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {
                "name": "claude",
                "runtime": "claude",
                "model": "opus-3",
                "trust_level": "elevated",
            })
            _project_card(root, "demo", "claude", {"model": "opus-4"})
            result = resolve_agent_profile(root, "claude", project="demo")
            self.assertEqual(result["model"], "opus-4")
            self.assertEqual(result["trust_level"], "elevated")

    def test_global_only_with_project(self) -> None:
        """Returns global profile when project card doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {"name": "claude", "runtime": "claude"})
            result = resolve_agent_profile(root, "claude", project="demo")
            self.assertEqual(result["name"], "claude")

    def test_project_only(self) -> None:
        """Returns project card when global profile doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _project_card(root, "demo", "claude", {"name": "claude", "model": "opus-4"})
            result = resolve_agent_profile(root, "claude", project="demo")
            self.assertEqual(result["name"], "claude")
            self.assertEqual(result["model"], "opus-4")

    def test_returns_none_when_neither_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = resolve_agent_profile(root, "ghost", project="demo")
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Tests: cmd_init
# ---------------------------------------------------------------------------


class TestAgentInit(unittest.TestCase):
    def test_creates_global_profile(self) -> None:
        """Verify profile.yaml is created at oacp_root/agents/<name>/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = _make_args(name="claude", runtime="claude")
            rc = cmd_init(args, root)
            self.assertEqual(rc, 0)
            profile_path = root / "agents" / "claude" / "profile.yaml"
            self.assertTrue(profile_path.is_file())
            content = profile_path.read_text(encoding="utf-8")
            self.assertIn('name: "claude"', content)
            self.assertIn('runtime: "claude"', content)

    def test_idempotent(self) -> None:
        """Running init twice doesn't overwrite existing profile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = _make_args(name="claude", runtime="claude")

            # First run
            rc1, out1 = _capture_stdout(cmd_init, args, root)
            self.assertEqual(rc1, 0)
            self.assertIn("Created", out1)

            # Write a marker to verify no overwrite
            profile_path = root / "agents" / "claude" / "profile.yaml"
            original_content = profile_path.read_text(encoding="utf-8")
            profile_path.write_text(original_content + "\n# marker\n", encoding="utf-8")

            # Second run
            rc2, out2 = _capture_stdout(cmd_init, args, root)
            self.assertEqual(rc2, 0)
            self.assertIn("already exists", out2)

            # Marker should still be there
            content = profile_path.read_text(encoding="utf-8")
            self.assertIn("# marker", content)

    def test_invalid_runtime(self) -> None:
        """Returns error for invalid runtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = _make_args(name="agent1", runtime="gpt")
            rc, err = _capture_stderr(cmd_init, args, root)
            self.assertEqual(rc, 1)
            self.assertIn("invalid runtime", err)

    def test_all_valid_runtimes(self) -> None:
        """All VALID_RUNTIMES are accepted."""
        for runtime in ("claude", "codex", "gemini", "human"):
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                args = _make_args(name=f"agent-{runtime}", runtime=runtime)
                rc = cmd_init(args, root)
                self.assertEqual(rc, 0, f"runtime '{runtime}' should be valid")

    def test_profile_contains_template_sections(self) -> None:
        """Created profile contains expected template sections."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = _make_args(name="claude", runtime="claude")
            cmd_init(args, root)
            content = (root / "agents" / "claude" / "profile.yaml").read_text(encoding="utf-8")
            # Template should have these sections
            self.assertIn("capabilities", content)
            self.assertIn("skills", content)
            self.assertIn("trust_level", content)


# ---------------------------------------------------------------------------
# Tests: cmd_list
# ---------------------------------------------------------------------------


class TestAgentList(unittest.TestCase):
    def test_global_agents(self) -> None:
        """Lists agents with global profiles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {"name": "claude", "runtime": "claude"})
            _global_profile(root, "codex", {"name": "codex", "runtime": "codex"})

            args = _make_args(project=None)
            rc, out = _capture_stdout(cmd_list, args, root)
            self.assertEqual(rc, 0)
            self.assertIn("claude", out)
            self.assertIn("codex", out)
            self.assertIn("global", out)

    def test_project_agents(self) -> None:
        """Lists agents from a project."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _project_card(root, "demo", "iris", {"name": "iris"})

            args = _make_args(project="demo")
            rc, out = _capture_stdout(cmd_list, args, root)
            self.assertEqual(rc, 0)
            self.assertIn("iris", out)
            self.assertIn("project", out)

    def test_combined(self) -> None:
        """Shows both global and project agents with tags."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {"name": "claude", "runtime": "claude"})
            _global_profile(root, "codex", {"name": "codex", "runtime": "codex"})
            _project_card(root, "demo", "claude", {"name": "claude"})
            _project_card(root, "demo", "iris", {"name": "iris"})

            args = _make_args(project="demo")
            rc, out = _capture_stdout(cmd_list, args, root)
            self.assertEqual(rc, 0)

            # claude should have both tags
            lines = out.strip().split("\n")
            claude_line = [ln for ln in lines if "claude" in ln][0]
            self.assertIn("global", claude_line)
            self.assertIn("project", claude_line)

            # codex should have only global
            codex_line = [ln for ln in lines if "codex" in ln][0]
            self.assertIn("global", codex_line)
            self.assertNotIn("project", codex_line)

            # iris should have only project
            iris_line = [ln for ln in lines if "iris" in ln][0]
            self.assertNotIn("global", iris_line)
            self.assertIn("project", iris_line)

    def test_empty(self) -> None:
        """Shows 'no agents found' when empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = _make_args(project=None)
            rc, out = _capture_stdout(cmd_list, args, root)
            self.assertEqual(rc, 0)
            self.assertIn("No", out)

    def test_empty_with_project(self) -> None:
        """Shows 'no agents' message that includes project name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = _make_args(project="demo")
            rc, out = _capture_stdout(cmd_list, args, root)
            self.assertEqual(rc, 0)
            self.assertIn("No", out)
            self.assertIn("demo", out)

    def test_agents_sorted_alphabetically(self) -> None:
        """Agent list output is sorted alphabetically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "zulu", {"name": "zulu"})
            _global_profile(root, "alpha", {"name": "alpha"})
            _global_profile(root, "mike", {"name": "mike"})

            args = _make_args(project=None)
            rc, out = _capture_stdout(cmd_list, args, root)
            self.assertEqual(rc, 0)
            lines = [ln.strip() for ln in out.strip().split("\n") if ln.strip()]
            names = [ln.split()[0] for ln in lines]
            self.assertEqual(names, ["alpha", "mike", "zulu"])


# ---------------------------------------------------------------------------
# Tests: cmd_show
# ---------------------------------------------------------------------------


class TestAgentShow(unittest.TestCase):
    def test_merged_output(self) -> None:
        """Shows merged profile when both global and project card exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {
                "name": "claude",
                "runtime": "claude",
                "model": "opus-3",
                "trust_level": "elevated",
                "capabilities": {"tools": ["Bash", "Read"], "languages": ["python"]},
            })
            _project_card(root, "demo", "claude", {
                "model": "opus-4",
                "capabilities": {"tools": ["Bash", "Read", "Edit"]},
            })

            args = _make_args(name="claude", project="demo")
            rc, out = _capture_stdout(cmd_show, args, root)
            self.assertEqual(rc, 0)

            shown = yaml.safe_load(out)
            self.assertEqual(shown["model"], "opus-4")
            self.assertEqual(shown["trust_level"], "elevated")
            self.assertEqual(shown["capabilities"]["tools"], ["Bash", "Read", "Edit"])
            self.assertEqual(shown["capabilities"]["languages"], ["python"])

    def test_global_only(self) -> None:
        """Shows global profile when no project card exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {"name": "claude", "runtime": "claude"})

            args = _make_args(name="claude", project="demo")
            rc, out = _capture_stdout(cmd_show, args, root)
            self.assertEqual(rc, 0)

            shown = yaml.safe_load(out)
            self.assertEqual(shown["name"], "claude")

    def test_project_only(self) -> None:
        """Shows project card when no global profile exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _project_card(root, "demo", "claude", {
                "name": "claude",
                "model": "opus-4",
            })

            args = _make_args(name="claude", project="demo")
            rc, out = _capture_stdout(cmd_show, args, root)
            self.assertEqual(rc, 0)

            shown = yaml.safe_load(out)
            self.assertEqual(shown["name"], "claude")
            self.assertEqual(shown["model"], "opus-4")

    def test_not_found(self) -> None:
        """Returns error when agent doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = _make_args(name="ghost", project="demo")
            rc, err = _capture_stderr(cmd_show, args, root)
            self.assertEqual(rc, 1)
            self.assertIn("no profile found", err)
            self.assertIn("ghost", err)

    def test_not_found_includes_project_in_error(self) -> None:
        """Error message includes project name when specified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = _make_args(name="ghost", project="myproject")
            rc, err = _capture_stderr(cmd_show, args, root)
            self.assertEqual(rc, 1)
            self.assertIn("myproject", err)

    def test_show_without_project(self) -> None:
        """Shows global profile when no project is specified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {"name": "claude", "runtime": "claude"})

            args = _make_args(name="claude", project=None)
            rc, out = _capture_stdout(cmd_show, args, root)
            self.assertEqual(rc, 0)

            shown = yaml.safe_load(out)
            self.assertEqual(shown["name"], "claude")

    def test_show_output_is_valid_yaml(self) -> None:
        """The show output is parseable YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _global_profile(root, "claude", {
                "name": "claude",
                "runtime": "claude",
                "skills": [{"id": "review", "name": "Review", "description": "Does reviews"}],
            })

            args = _make_args(name="claude", project=None)
            rc, out = _capture_stdout(cmd_show, args, root)
            self.assertEqual(rc, 0)

            # Should not raise
            parsed = yaml.safe_load(out)
            self.assertIsInstance(parsed, dict)
            self.assertEqual(len(parsed["skills"]), 1)


if __name__ == "__main__":
    unittest.main()
