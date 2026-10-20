# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Command migration coverage with realistic formats and synthetic identities."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest
import yaml

from oacp import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES = [
    ("agent", "add", "add_agent.py", []),
    ("agent", "init", "agent_profile.py", ["init"]),
    ("agent", "sync", "agent_profile.py", ["sync"]),
    ("agent", "show", "agent_profile.py", ["show"]),
    ("agent", "list", "agent_profile.py", ["list"]),
    ("trust", "keygen", "key_cli.py", ["gen"]),
    ("trust", "keys", "key_cli.py", ["list"]),
    ("trust", "import", "trust_cli.py", ["import"]),
    ("trust", "list", "trust_cli.py", ["list"]),
    ("trust", "revoke", "trust_cli.py", ["revoke"]),
    ("trust", "sign-policy", "trust_cli.py", ["sign-policy"]),
    ("autonomy", "outcome", "record_autonomy_outcome.py", []),
    ("autonomy", "finalize", "finalize_autonomy_record.py", []),
]


@pytest.mark.parametrize("group,verb,script,prefix", ROUTES)
def test_group_reuses_script_arguments_and_exit_code(group, verb, script, prefix):
    argv = ["sample", "--unknown-option", "value", "--", "literal"]
    with mock.patch.object(cli, "_run_script", return_value=7) as run:
        assert cli.main([group, verb, *argv]) == 7
        run.assert_called_once_with(script, [*prefix, *argv])


def run_cli(args, scratch):
    return subprocess.run(
        [sys.executable, "-m", "oacp.cli", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "OACP_HOME": str(scratch / "ambient"), "PYTHONPATH": str(REPO_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def with_home(group, verb, args, home, placement):
    if placement == "before":
        return [group, "--oacp-dir", str(home), verb, *args]
    if placement == "after":
        return [group, verb, *args, "--oacp-dir", str(home)]
    if placement == "equals-before":
        return [group, f"--oacp-dir={home}", verb, *args]
    if placement == "equals-after":
        return [group, verb, *args, f"--oacp-dir={home}"]
    return [group, "--oacp-dir", str(home.parent / "ignored"), verb, *args, f"--oacp-dir={home}"]


@pytest.mark.parametrize("placement", ["before", "after", "equals-before", "equals-after", "repeated"])
def test_agent_add_preserves_global_registry_and_home_positions(tmp_path, placement):
    home = tmp_path / "selected"
    project = home / "projects" / "demo"
    project.mkdir(parents=True)
    profile = run_cli(with_home("agent", "init", ["alice", "--runtime", "codex"], home, placement), tmp_path)
    assert profile.returncode == 0, profile.stderr
    added = run_cli(with_home("agent", "add", ["demo", "alice", "--runtime", "codex"], home, placement), tmp_path)
    assert added.returncode == 0, added.stderr
    data = yaml.safe_load((home / "agents" / "alice" / "profile.yaml").read_text())
    assert data["name"] == "alice"
    assert data["projects"] == ["demo"]
    assert (project / "agents" / "alice" / "agent_card.yaml").is_file()
    listed = run_cli(with_home("agent", "list", [], home, placement), tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert "alice" in listed.stdout
    assert not (tmp_path / "ambient").exists()
    assert not (tmp_path / "ignored").exists()


@pytest.mark.parametrize("placement", ["before", "after", "equals-before", "equals-after", "repeated"])
def test_local_trust_keys_need_no_project(tmp_path, placement):
    pytest.importorskip("cryptography")
    home = tmp_path / "selected"
    generated = run_cli(with_home("trust", "keygen", ["--agent", "alice", "--json"], home, placement), tmp_path)
    assert generated.returncode == 0, generated.stderr
    key = json.loads(generated.stdout)
    listed = run_cli(with_home("trust", "keys", ["--agent", "alice", "--json"], home, placement), tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert [entry["kid"] for entry in json.loads(listed.stdout)] == [key["kid"]]
    assert not (home / "projects").exists()
    assert not (tmp_path / "ambient").exists()
    assert not (tmp_path / "ignored").exists()


@pytest.mark.parametrize("group,verb,script,prefix", ROUTES)
def test_nested_help_reaches_the_existing_leaf(tmp_path, group, verb, script, prefix):
    direct = run_cli([group, verb, "--help"], tmp_path)
    nested = run_cli(["help", group, verb], tmp_path)
    assert direct.returncode == nested.returncode == 0
    assert direct.stdout == nested.stdout
    assert "usage:" in direct.stdout
    assert direct.stderr == nested.stderr == ""
    assert not (tmp_path / "ambient").exists()


@pytest.mark.parametrize("verb,script,args", [
    ("outcome", "record_autonomy_outcome.py", ["missing.yaml", "--decision", "approved"]),
    ("finalize", "finalize_autonomy_record.py", ["missing.yaml", "--validate"]),
])
def test_autonomy_validation_exit_matches_script(tmp_path, verb, script, args):
    args = [str(tmp_path / args[0]), *args[1:]]
    grouped = run_cli(["autonomy", verb, *args], tmp_path)
    direct = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
        cwd=REPO_ROOT, text=True, capture_output=True, check=False,
    )
    assert grouped.returncode == direct.returncode == 2
    assert grouped.stdout == direct.stdout
    assert grouped.stderr == direct.stderr


def test_group_help_and_invalid_subcommand(capsys):
    for group in ("agent", "trust", "autonomy"):
        assert cli.main([group, "--help"]) == 0
        assert "usage:" in capsys.readouterr().out
        with mock.patch.object(cli, "_run_script") as run:
            assert cli.main([group, "unsupported"]) == 2
            run.assert_not_called()
        assert "invalid choice" in capsys.readouterr().err
    assert cli.main(["agent"]) == 0
    assert "usage:" in capsys.readouterr().out
    assert cli.main(["agent", "--oacp-dir", "/unused"]) == 0
    assert "usage:" in capsys.readouterr().out
    assert cli.main(["trust"]) == 2
    assert "required" in capsys.readouterr().err
    assert len(cli.SCRIPT_NAMES) + len(cli.COMMAND_GROUPS) == 13


@pytest.mark.parametrize("placement", ["before", "after", "equals-before", "equals-after", "repeated"])
def test_project_trust_list_retains_its_scope(tmp_path, placement):
    home = tmp_path / "selected"
    (home / "projects" / "demo").mkdir(parents=True)
    listed = run_cli(with_home("trust", "list", ["--project", "demo", "--json"], home, placement), tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == {"catalog": [], "pins": {}}
    missing_project = run_cli(with_home("trust", "list", ["--json"], home, placement), tmp_path)
    assert missing_project.returncode == 2
    assert "--project" in missing_project.stderr
    assert not (tmp_path / "ambient").exists()
    assert not (tmp_path / "ignored").exists()
