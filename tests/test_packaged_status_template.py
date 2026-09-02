# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Packaged-path regression test: ``oacp doctor --fix`` on a wheel install
creates ``status.yaml`` from the bundled ``agent_status.template.yaml``.

A repo checkout cannot catch this class — the doctor's fallback finds the
template in the working tree — so the wheel is built, installed into a
throwaway venv, and the installed ``oacp`` entry point runs against a
workspace that carries no template copy. Requires ``build`` and
``hatchling`` (dev dependency group); the venv needs ``ensurepip``. All
three skip, never fail, when unavailable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, Sequence

import pytest

pytest.importorskip("build")
pytest.importorskip("hatchling")

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGED_TEMPLATE = "oacp/_templates/agent_status.template.yaml"
WINDOWS = os.name == "nt"


def _hermetic_env() -> Dict[str, str]:
    """The parent environment minus anything that could reach this checkout
    or the developer's workspace from inside the venv."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OACP_HOME", "PYTHONPATH"}
    }
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _run(argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), capture_output=True, text=True, env=_hermetic_env(), **kwargs
    )


def _tail(completed: subprocess.CompletedProcess) -> str:
    return completed.stdout[-2000:] + completed.stderr[-2000:]


@pytest.fixture(scope="module")
def wheel_path(tmp_path_factory) -> Path:
    outdir = tmp_path_factory.mktemp("wheel")
    completed = _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(outdir),
            str(REPO_ROOT),
        ]
    )
    if completed.returncode:
        pytest.fail("wheel build failed:\n" + _tail(completed))
    wheels = list(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def test_status_template_is_shipped(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as zf:
        assert PACKAGED_TEMPLATE in zf.namelist()


@pytest.fixture(scope="module")
def installed_oacp(tmp_path_factory, wheel_path: Path) -> Path:
    """The ``oacp`` entry point inside a venv holding only the built wheel."""
    venv_dir = tmp_path_factory.mktemp("venv") / "venv"
    completed = _run([sys.executable, "-m", "venv", str(venv_dir)])
    if completed.returncode:
        pytest.skip("venv with pip unavailable:\n" + _tail(completed))
    bindir = venv_dir / ("Scripts" if WINDOWS else "bin")
    python = bindir / ("python.exe" if WINDOWS else "python")
    # PyYAML is the wheel's only runtime dependency and every read on the
    # exercised path tolerates its absence; --no-index/--no-deps keeps the
    # install offline and the venv hermetic.
    completed = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-index",
            "--no-deps",
            str(wheel_path),
        ]
    )
    if completed.returncode:
        pytest.fail("wheel install failed:\n" + _tail(completed))
    # The venv must resolve the installed package, never this checkout.
    completed = _run(
        [str(python), "-c", "import oacp; print(oacp.__file__)"], cwd=str(venv_dir)
    )
    assert completed.returncode == 0, _tail(completed)
    resolved = Path(completed.stdout.strip()).resolve()
    assert venv_dir.resolve() in resolved.parents, resolved
    oacp_bin = bindir / ("oacp.exe" if WINDOWS else "oacp")
    assert oacp_bin.is_file(), oacp_bin
    return oacp_bin


def test_doctor_fix_creates_status_from_packaged_template(
    installed_oacp: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project_dir = home / "projects" / "proj"
    (project_dir / "agents" / "claude" / "inbox").mkdir(parents=True)
    (project_dir / "workspace.json").write_text(
        json.dumps({"project_name": "proj", "agents": ["claude"]}),
        encoding="utf-8",
    )
    # No workspace template copy: only the packaged fallback can serve.
    assert not (home / "templates").exists()

    completed = _run(
        [
            str(installed_oacp),
            "doctor",
            "--project",
            "proj",
            "--oacp-dir",
            str(home),
            "--fix",
            "--json",
        ],
        cwd=str(tmp_path),
    )
    # The exit status reflects unrelated environment checks; the JSON report
    # is the contract.
    assert completed.stdout.strip(), _tail(completed)
    payload = json.loads(completed.stdout)
    assert "Created claude/status.yaml" in payload["fixed"], _tail(completed)

    status = project_dir / "agents" / "claude" / "status.yaml"
    text = status.read_text(encoding="utf-8")
    assert "runtime: claude" in text
    assert "capabilities:" in text
