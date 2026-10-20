# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Removed memory-related commands fail even when agent-memory is on PATH."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("command", ["memory", "org-memory", "write-event", "retention"])
@pytest.mark.parametrize("tool_present", [False, True])
@pytest.mark.parametrize("invocation", ["call", "help", "help-command"])
def test_removed_command_never_executes_memory_tool(
    tmp_path: Path, command: str, tool_present: bool, invocation: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "tool-executed"
    if tool_present:
        # Realistic executable format; all names and data are synthetic.
        stub = bin_dir / "agent-memory"
        stub.write_text(
            f"#!{sys.executable}\n"
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['STUB_MARKER']).touch()\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "PYTHONPATH": str(REPO_ROOT),
        "STUB_MARKER": str(marker),
    }
    args = {
        "call": [command, "--oacp-dir", str(tmp_path / "home")],
        "help": [command, "--help"],
        "help-command": ["help", command],
    }[invocation]
    completed = subprocess.run(
        [sys.executable, "-m", "oacp.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        f"ERROR: unknown command '{command}'\nRun `oacp --help` for usage.\n"
    )
    assert not marker.exists()
    assert not (tmp_path / "home").exists()
