# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_workflow(name: str) -> dict:
    path = REPO_ROOT / ".github" / "workflows" / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "on" not in data and True in data:
        data["on"] = data.pop(True)
    return data


def test_ci_workflow_runs_preflight_on_pull_requests() -> None:
    workflow = _load_workflow("ci.yml")

    assert workflow["name"] == "CI"
    assert workflow["on"]["pull_request"]["branches"] == ["main"]
    assert "preflight" in workflow["jobs"]

    steps = workflow["jobs"]["preflight"]["steps"]
    commands = [step.get("run", "") for step in steps]

    assert 'make preflight ARGS="--full"' in commands
    assert "python -m build" in commands


def test_release_workflow_publishes_with_trusted_publishing() -> None:
    workflow = _load_workflow("release.yml")

    assert workflow["name"] == "Release"
    assert workflow["on"]["push"]["tags"] == ["v[0-9]*"]

    build_steps = workflow["jobs"]["build"]["steps"]
    build_step_names = {step["name"]: step for step in build_steps}
    assert build_step_names["Check out repository"]["uses"].startswith("actions/checkout@")
    assert build_step_names["Set up Python"]["uses"].startswith("actions/setup-python@")
    assert build_step_names["Run quality gate"]["run"] == 'make preflight ARGS="--full"'
    assert build_step_names["Build wheel and sdist"]["run"] == "python -m build"
    assert build_step_names["Upload release artifacts"]["uses"].startswith("actions/upload-artifact@")

    publish_job = workflow["jobs"]["publish-pypi"]
    assert publish_job["environment"]["name"] == "pypi"
    assert publish_job["permissions"]["id-token"] == "write"
    assert publish_job["steps"][0]["uses"].startswith("actions/download-artifact@")
    assert publish_job["steps"][-1]["uses"].startswith("pypa/gh-action-pypi-publish@")

    release_job = workflow["jobs"]["github-release"]
    assert set(release_job["needs"]) == {"build", "publish-pypi"}
    assert release_job["permissions"]["contents"] == "write"
    assert release_job["steps"][0]["uses"].startswith("actions/download-artifact@")
    assert "gh release create" in release_job["steps"][-1]["run"]
