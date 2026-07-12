# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for the durable-memory git sync engine."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import oacp_doctor  # noqa: E402
from memory_sync import (  # noqa: E402
    CANONICAL_MEMORY_GITIGNORE,
    GIT_NETWORK_TIMEOUT_SECONDS,
    MemorySyncError,
    clone_memory_repo,
    pull_memory,
    push_memory,
    push_remote,
)


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def _git(*args: str, cwd: Optional[Path] = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _configure_identity(repo: Path) -> None:
    _git("config", "user.name", "OACP Test", cwd=repo)
    _git("config", "user.email", "oacp-test@example.invalid", cwd=repo)


def _create_remote(tmp_path: Path) -> Tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git("init", "--bare", str(remote))
    _git("init", str(seed))
    _git("checkout", "-b", "main", cwd=seed)
    _configure_identity(seed)
    _write(seed / ".gitignore", CANONICAL_MEMORY_GITIGNORE)
    _write(seed / ".oacp-memory-repo", "memory sync enabled\n")
    _write(seed / "org-memory" / "recent.md", "seed\n")
    _git("add", ".gitignore", ".oacp-memory-repo", "org-memory/recent.md", cwd=seed)
    _git("commit", "-m", "seed memory", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    _git("--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")
    return remote, seed


def _clone(remote: Path, destination: Path) -> None:
    clone_memory_repo(destination, str(remote))
    _configure_identity(destination)


def _commit_and_push(repo: Path, relative_path: str, content: str) -> None:
    _write(repo / relative_path, content)
    _git("add", relative_path, cwd=repo)
    _git("commit", "-m", f"update {relative_path}", cwd=repo)
    _git("push", cwd=repo)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: List[Tuple[Tuple[str, ...], Optional[int]]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str]:
        call = tuple(command)
        self.calls.append((call, timeout))
        args = list(command[1:])
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true"
        if args == ["status", "--porcelain"]:
            return 0, ""
        if args == ["remote"]:
            return 0, "origin"
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return 0, "origin/main"
        if args == ["rev-list", "--left-right", "--count", "HEAD...origin/main"]:
            return 0, "0 1"
        return 0, ""


def _assert_timed_call(
    runner: RecordingRunner,
    expected_args: Sequence[str],
) -> None:
    assert any(
        list(command[1:]) == list(expected_args)
        and timeout == GIT_NETWORK_TIMEOUT_SECONDS
        for command, timeout in runner.calls
    )


def test_fetch_pull_push_and_clone_receive_network_timeouts(tmp_path: Path) -> None:
    root = tmp_path / "oacp"
    root.mkdir()
    _write(root / ".oacp-memory-repo", "memory sync enabled\n")
    runner = RecordingRunner()

    lines = pull_memory(root, runner=runner)
    push_remote(root, runner=runner)
    clone_target = tmp_path / "clone"
    clone_memory_repo(clone_target, "example.invalid/repo.git", runner=runner)

    assert lines == ["OACP memory pull: synced 1 commit(s)."]
    _assert_timed_call(runner, ["fetch", "--quiet"])
    _assert_timed_call(runner, ["pull", "--ff-only"])
    _assert_timed_call(runner, ["push"])
    _assert_timed_call(
        runner,
        ["clone", "example.invalid/repo.git", str(clone_target)],
    )


def test_doctor_memory_fetch_forwards_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "oacp"
    root.mkdir()
    _write(root / ".oacp-memory-repo", "memory sync enabled\n")
    _write(root / ".gitignore", CANONICAL_MEMORY_GITIGNORE)
    calls: List[Tuple[Tuple[str, ...], Optional[int]]] = []

    def fake_run_git_command(
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str]:
        assert cwd == root
        call = tuple(command)
        calls.append((call, timeout))
        args = list(command[1:])
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true"
        if args == ["ls-files"]:
            return 0, ".gitignore\n.oacp-memory-repo"
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return 0, ""
        if args == ["status", "--porcelain"]:
            return 0, ""
        if args == ["remote"]:
            return 0, "origin"
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return 1, ""
        if args == ["rev-parse", "--verify", "HEAD"]:
            return 1, ""
        return 0, ""

    monkeypatch.setattr(oacp_doctor, "run_git_command", fake_run_git_command)

    oacp_doctor.check_memory_sync(root)

    assert any(
        list(command[1:]) == ["fetch", "--quiet"]
        and timeout == GIT_NETWORK_TIMEOUT_SECONDS
        for command, timeout in calls
    )


def test_force_clone_preserves_existing_home_as_backup(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    root = tmp_path / "oacp"
    _write(root / "local-only.txt", "keep me\n")

    lines = clone_memory_repo(root, str(remote), force=True)

    backups = list(tmp_path.glob("oacp.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "local-only.txt").read_text(encoding="utf-8") == "keep me\n"
    assert (root / "org-memory" / "recent.md").read_text(encoding="utf-8") == "seed\n"
    assert any("Moved existing OACP_HOME aside" in line for line in lines)


def test_force_clone_failure_restores_existing_home(tmp_path: Path) -> None:
    root = tmp_path / "oacp"
    _write(root / "local-only.txt", "keep me\n")

    with pytest.raises(MemorySyncError, match="git clone failed"):
        clone_memory_repo(root, str(tmp_path / "missing.git"), force=True)

    assert (root / "local-only.txt").read_text(encoding="utf-8") == "keep me\n"
    assert not list(tmp_path.glob("oacp.backup-*"))


def test_pull_memory_fast_forwards_real_repo(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    root = tmp_path / "oacp"
    _clone(remote, root)
    _commit_and_push(seed, "org-memory/new.md", "remote update\n")

    lines = pull_memory(root)

    assert lines == ["OACP memory pull: synced 1 commit(s)."]
    assert (root / "org-memory" / "new.md").read_text(encoding="utf-8") == "remote update\n"


def test_push_memory_commits_dirty_memory_and_pushes(tmp_path: Path) -> None:
    remote, _ = _create_remote(tmp_path)
    root = tmp_path / "oacp"
    _clone(remote, root)
    _write(root / "org-memory" / "local.md", "local update\n")

    lines, code = push_memory(root)

    assert code == 0
    assert any("committed 1 file(s)" in line for line in lines)
    assert _git("status", "--porcelain", cwd=root) == ""
    assert (
        _git("--git-dir", str(remote), "show", "main:org-memory/local.md")
        == "local update"
    )


def test_push_memory_refuses_behind_repo(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    root = tmp_path / "oacp"
    _clone(remote, root)
    _commit_and_push(seed, "org-memory/remote.md", "remote update\n")

    lines, code = push_memory(root)

    assert code == 1
    assert any("behind upstream" in line for line in lines)


def test_push_memory_refuses_diverged_repo(tmp_path: Path) -> None:
    remote, seed = _create_remote(tmp_path)
    root = tmp_path / "oacp"
    _clone(remote, root)
    _write(root / "org-memory" / "local.md", "local update\n")
    _git("add", "org-memory/local.md", cwd=root)
    _git("commit", "-m", "local update", cwd=root)
    _commit_and_push(seed, "org-memory/remote.md", "remote update\n")

    lines, code = push_memory(root)

    assert code == 1
    assert any("diverged" in line for line in lines)
