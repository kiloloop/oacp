# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Create-only initialization contract using synthetic project/agent names."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from init_project_workspace import initialize_workspace, main, plan_workspace  # noqa: E402


def tree(root):
    return {str(p.relative_to(root)): (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in root.rglob("*")}


def test_plan_creates_nothing_and_reports_global_membership(tmp_path, capsys):
    home = tmp_path / "absent"
    assert main(["demo", "--oacp-dir", str(home), "--agents", "author", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["can_apply"] is True
    assert {"path": str(home / "agents/author/profile.yaml"), "operation": "ensure_project_membership"} in plan["actions"]
    assert not home.exists()


def test_second_init_preserves_entire_tree_and_metadata(tmp_path, capsys):
    initialize_workspace("demo", oacp_root=tmp_path, agents=["author"])
    marker = tmp_path / "projects/demo/workspace.json"
    marker.write_text('{"custom": "retain this", "created_at": "original"}\n')
    before = tree(tmp_path)
    for flags in [[], ["--fail-if-exists"], ["--dry-run", "--json"]]:
        assert main(["demo", "--oacp-dir", str(tmp_path), "--agents", "reviewer", *flags]) == 3
        assert tree(tmp_path) == before
    capsys.readouterr()


@pytest.mark.parametrize("kind", ["directory", "file", "dangling_link"])
def test_existing_root_without_workspace_is_also_refused(tmp_path, kind):
    root = tmp_path / "projects/demo"
    root.parent.mkdir()
    if kind == "directory":
        root.mkdir()
    elif kind == "file":
        root.write_text("preserve")
    else:
        root.symlink_to(tmp_path / "missing")
    assert not plan_workspace("demo", oacp_root=tmp_path)["can_apply"]
    with pytest.raises(FileExistsError):
        initialize_workspace("demo", oacp_root=tmp_path)
    assert not (tmp_path / "agents").exists()


def test_concurrent_apply_has_one_winner(tmp_path):
    root = Path(__file__).resolve().parents[1]
    processes = [subprocess.Popen([sys.executable, "-m", "oacp.cli", "init", "demo",
                                  "--oacp-dir", str(tmp_path), "--agents", agent, "--json"],
                                 cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for agent in ["author", "reviewer"]]
    outputs = [p.communicate() for p in processes]
    assert sorted(p.returncode for p in processes) == [0, 3], outputs
    assert len(list((tmp_path / "projects/demo/agents").iterdir())) == 1
    assert len(list((tmp_path / "agents").iterdir())) == 1


def test_plan_does_not_reserve_project(tmp_path):
    assert plan_workspace("demo", oacp_root=tmp_path)["can_apply"]
    initialize_workspace("demo", oacp_root=tmp_path, agents=["author"])
    before = tree(tmp_path)
    with pytest.raises(FileExistsError):
        initialize_workspace("demo", oacp_root=tmp_path, agents=["reviewer"])
    assert tree(tmp_path) == before


@pytest.mark.parametrize("links", [[("docs", "../outside")], [("docs", "/outside")]])
def test_bad_links_fail_before_any_writes(tmp_path, links):
    home = tmp_path / "home"
    with pytest.raises(ValueError):
        initialize_workspace("demo", oacp_root=home, repo_dir=tmp_path, artifact_links=links)
    assert not home.exists()


def test_json_distinguishes_existing_root_from_parent_collision(tmp_path, capsys):
    initialize_workspace("demo", oacp_root=tmp_path, agents=[])
    assert main(["demo", "--oacp-dir", str(tmp_path), "--json"]) == 3
    error = json.loads(capsys.readouterr().out)
    assert set(error) == {"version", "error", "message"}
    assert error["version"] == 1 and error["error"] == "already_exists"
    other = tmp_path / "other"
    other.mkdir()
    (other / "projects").write_text("occupied")
    assert main(["demo", "--oacp-dir", str(other), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "initialization_failed"
    assert not (other / "agents").exists()


@pytest.mark.parametrize("destinations", [["a", "a/b"], ["a/b", "a"], [".gitkeep"], [".gitkeep/child"]])
def test_overlapping_artifact_links_refused_before_creation(tmp_path, destinations):
    home = tmp_path / "home"
    with pytest.raises(ValueError):
        initialize_workspace("demo", oacp_root=home, repo_dir=tmp_path,
                             artifact_links=[("source", dst) for dst in destinations])
    assert not home.exists()


def test_doctor_repair_hint_works_for_existing_project(tmp_path):
    from oacp_doctor import check_workspace
    from add_agent import add_agent

    root = tmp_path / "projects/demo"
    root.mkdir(parents=True)
    results = {r.name: r for r in check_workspace(root).results}
    assert "Init is create-only" in results["workspace.json"].fix_hint
    assert "oacp agent add demo <agent>" in results["agents/"].fix_hint
    add_agent("demo", "author", oacp_root=tmp_path)
    assert (root / "agents/author/inbox").is_dir()
