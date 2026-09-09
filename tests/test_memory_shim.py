# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The `oacp memory` / `oacp org-memory` exec shim, end to end.

A stub `agent-memory` on PATH records the argv it was exec'd with, so the
passthrough, the `--oacp-dir` -> `--home` rewrite, and the `init` -> `enable`
map are observed through a real `os.execvp`, not a mock. The absent-tool
path is exercised with a PATH that holds no `agent-memory` at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from oacp.cli import DELEGATED_COMMANDS, delegated_argv  # noqa: E402

# The shebang is this interpreter by absolute path: the test PATH holds only the
# stub directory and the interpreter directory, which need not spell `python3`.
STUB = """\
#!__PYTHON__
import json, os, sys
with open(os.environ["STUB_ARGV_FILE"], "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv, "stdout": "stub ran"}, fh)
print("stub ran")
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


def _run_oacp(args: Sequence[str], *, path: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH"}}
    env.update({"PATH": path, "PYTHONPATH": str(REPO_ROOT)})
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "oacp.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        check=False,
    )


@pytest.fixture
def stub_tool(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "agent-memory"
    stub.write_text(STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    argv_file = tmp_path / "argv.json"
    python_dir = str(Path(sys.executable).parent)

    def run(args: Sequence[str], **env_extra: str) -> subprocess.CompletedProcess:
        return _run_oacp(
            args,
            path=os.pathsep.join([str(bin_dir), python_dir]),
            env_extra={"STUB_ARGV_FILE": str(argv_file), **env_extra},
        )

    def recorded() -> List[str]:
        return json.loads(argv_file.read_text(encoding="utf-8"))["argv"]

    return run, recorded


class TestDelegatedArgv:
    def test_memory_passthrough_rewrites_the_home_flag(self) -> None:
        assert delegated_argv("memory", ["pull", "--oacp-dir", "/h"]) == ["agent-memory", "pull", "--home", "/h"]
        assert delegated_argv("memory", ["push", "--oacp-dir=/h"]) == ["agent-memory", "push", "--home=/h"]

    def test_memory_init_maps_to_enable(self) -> None:
        assert delegated_argv("memory", ["init", "--remote", "git@x:y.git"]) == [
            "agent-memory", "enable", "--remote", "git@x:y.git",
        ]

    def test_only_the_verb_position_is_mapped(self) -> None:
        # A later `init` token is an argument, not the verb.
        assert delegated_argv("memory", ["archive", "demo", "init"]) == ["agent-memory", "archive", "demo", "init"]

    def test_org_memory_targets_the_org_tier(self) -> None:
        assert delegated_argv("org-memory", ["init", "--oacp-dir", "/h"]) == ["agent-memory", "org", "init", "--home", "/h"]

    def test_every_delegated_command_has_a_prefix(self) -> None:
        assert set(DELEGATED_COMMANDS) == {"memory", "org-memory"}


class TestExecShim:
    def test_memory_pull_execs_the_tool_with_rewritten_argv(self, stub_tool) -> None:
        run, recorded = stub_tool
        completed = run(["memory", "pull", "--oacp-dir", "/tmp/home"])
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "stub ran"
        assert recorded()[1:] == ["pull", "--home", "/tmp/home"]

    def test_memory_init_execs_enable(self, stub_tool) -> None:
        run, recorded = stub_tool
        completed = run(["memory", "init", "--remote", "git@example:org/memory.git"])
        assert completed.returncode == 0, completed.stderr
        assert recorded()[1:] == ["enable", "--remote", "git@example:org/memory.git"]

    def test_org_memory_init_execs_org_init(self, stub_tool) -> None:
        run, recorded = stub_tool
        completed = run(["org-memory", "init"])
        assert completed.returncode == 0, completed.stderr
        assert recorded()[1:] == ["org", "init"]

    def test_exit_status_passes_through(self, stub_tool) -> None:
        run, _recorded = stub_tool
        completed = run(["memory", "push"], STUB_EXIT="3")
        assert completed.returncode == 3

    def test_absent_tool_exits_127_with_one_stderr_line(self, tmp_path: Path) -> None:
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        completed = _run_oacp(
            ["memory", "pull"],
            path=os.pathsep.join([str(empty_bin), str(Path(sys.executable).parent)]),
            env_extra={},
        )
        assert completed.returncode == 127
        assert completed.stdout == ""
        lines = completed.stderr.splitlines()
        assert len(lines) == 1, completed.stderr
        assert "agent-memory" in lines[0]
        assert "pip install agent-memory-cli" in lines[0]
