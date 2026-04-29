#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Plain-git sync helpers for OACP durable memory."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

Runner = Callable[[Sequence[str]], Tuple[int, str]]

MARKER_FILE = ".oacp-memory-repo"
GIT_FETCH_TIMEOUT_SECONDS = 30
CANONICAL_MEMORY_GITIGNORE = """\
*
!*/
!.gitignore
!.oacp-memory-repo
!org-memory/**
!projects/*/memory/**
projects/*/memory/.cache/
"""

STALE_MEMORY_DAYS = 7


class MemorySyncError(Exception):
    """Raised when a memory sync command cannot safely proceed."""


@dataclass
class GitState:
    has_remote: bool
    has_upstream: bool
    ahead: int = 0
    behind: int = 0
    diverged: bool = False
    dirty: bool = False
    fetch_failed: bool = False
    fetch_output: str = ""
    upstream: str = ""


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: Optional[int] = None,
) -> Tuple[int, str]:
    """Run a command in *cwd* and return (exit_code, combined_output)."""
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f"Command not found: {command[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"Command timed out after {timeout}s: {' '.join(command)}"
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return completed.returncode, output.strip()


def _git(
    cwd: Path,
    args: Sequence[str],
    runner: Optional[Runner] = None,
    *,
    timeout: Optional[int] = None,
) -> Tuple[int, str]:
    command = ["git", *args]
    if runner is None:
        return run_command(command, cwd=cwd, timeout=timeout)
    return runner(command)


def marker_path(oacp_root: Path) -> Path:
    return oacp_root / MARKER_FILE


def is_configured(oacp_root: Path) -> bool:
    return marker_path(oacp_root).is_file()


def is_git_repo(oacp_root: Path, runner: Optional[Runner] = None) -> bool:
    rc, _ = _git(oacp_root, ["rev-parse", "--is-inside-work-tree"], runner)
    return rc == 0


def ensure_memory_repo(oacp_root: Path, runner: Optional[Runner] = None) -> None:
    if not is_configured(oacp_root):
        raise MemorySyncError("OACP memory sync is not configured.")
    if not is_git_repo(oacp_root, runner):
        raise MemorySyncError(
            f"{MARKER_FILE} is present, but {oacp_root} is not a git repository."
        )


def write_canonical_gitignore(oacp_root: Path) -> None:
    (oacp_root / ".gitignore").write_text(
        CANONICAL_MEMORY_GITIGNORE,
        encoding="utf-8",
    )


def write_marker(oacp_root: Path) -> None:
    marker_path(oacp_root).write_text(
        "OACP memory sync repository. Remove this file to disable hooks locally.\n",
        encoding="utf-8",
    )


def allowed_memory_dirs(oacp_root: Path) -> List[Path]:
    paths: List[Path] = []
    org_memory = oacp_root / "org-memory"
    if org_memory.exists():
        paths.append(org_memory)
    projects = oacp_root / "projects"
    if projects.is_dir():
        for project_dir in sorted(projects.iterdir()):
            memory_dir = project_dir / "memory"
            if memory_dir.exists():
                paths.append(memory_dir)
    return paths


def add_allowlist_paths(oacp_root: Path, runner: Optional[Runner] = None) -> None:
    paths = [oacp_root / ".gitignore", marker_path(oacp_root), *allowed_memory_dirs(oacp_root)]
    existing = [str(path.relative_to(oacp_root)) for path in paths if path.exists()]
    if existing:
        rc, output = _git(oacp_root, ["add", "--", *existing], runner)
        if rc != 0:
            raise MemorySyncError(f"git add failed: {output}")


def has_remote(oacp_root: Path, runner: Optional[Runner] = None) -> bool:
    rc, output = _git(oacp_root, ["remote"], runner)
    return rc == 0 and bool(output.strip())


def default_remote(oacp_root: Path, runner: Optional[Runner] = None) -> str:
    rc, output = _git(oacp_root, ["remote"], runner)
    if rc != 0:
        return ""
    remotes = [line.strip() for line in output.splitlines() if line.strip()]
    return remotes[0] if remotes else ""


def remote_exists(
    oacp_root: Path,
    remote_name: str,
    runner: Optional[Runner] = None,
) -> bool:
    rc, _ = _git(oacp_root, ["remote", "get-url", remote_name], runner)
    return rc == 0


def configured_upstream(oacp_root: Path, runner: Optional[Runner] = None) -> str:
    rc, output = _git(
        oacp_root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        runner,
    )
    return output.strip() if rc == 0 else ""


def status_porcelain(oacp_root: Path, runner: Optional[Runner] = None) -> str:
    rc, output = _git(oacp_root, ["status", "--porcelain"], runner)
    if rc != 0:
        raise MemorySyncError(f"git status failed: {output}")
    return output.strip()


def current_branch(oacp_root: Path, runner: Optional[Runner] = None) -> str:
    rc, output = _git(oacp_root, ["branch", "--show-current"], runner)
    if rc == 0 and output.strip():
        return output.strip()
    return "HEAD"


def fetch_remote(oacp_root: Path, runner: Optional[Runner] = None) -> Tuple[bool, str]:
    if not has_remote(oacp_root, runner):
        return True, ""
    rc, output = _git(
        oacp_root,
        ["fetch", "--quiet"],
        runner,
        timeout=GIT_FETCH_TIMEOUT_SECONDS,
    )
    return rc == 0, output


def compute_git_state(
    oacp_root: Path,
    *,
    runner: Optional[Runner] = None,
    fetch: bool = True,
) -> GitState:
    dirty = bool(status_porcelain(oacp_root, runner))
    remote = has_remote(oacp_root, runner)
    fetch_failed = False
    fetch_output = ""
    if fetch and remote:
        ok, fetch_output = fetch_remote(oacp_root, runner)
        fetch_failed = not ok

    upstream = configured_upstream(oacp_root, runner)
    state = GitState(
        has_remote=remote,
        has_upstream=bool(upstream),
        dirty=dirty,
        fetch_failed=fetch_failed,
        fetch_output=fetch_output,
        upstream=upstream,
    )
    if not upstream:
        return state

    rc, output = _git(
        oacp_root,
        ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
        runner,
    )
    if rc != 0:
        state.fetch_failed = True
        state.fetch_output = output
        return state
    parts = output.split()
    if len(parts) >= 2:
        state.ahead = int(parts[0])
        state.behind = int(parts[1])
        state.diverged = state.ahead > 0 and state.behind > 0
    return state


def state_warnings(state: GitState, *, include_dirty: bool = True) -> List[str]:
    warnings: List[str] = []
    if state.fetch_failed:
        detail = f": {state.fetch_output}" if state.fetch_output else ""
        warnings.append(f"WARNING: remote fetch failed{detail}")
    if include_dirty and state.dirty:
        warnings.append(
            "WARNING: uncommitted memory changes are present; memory is not clean."
        )
    if state.diverged:
        warnings.append(
            "WARNING: memory repo has diverged from its upstream; resolve manually."
        )
    elif state.behind:
        warnings.append(
            f"WARNING: memory repo is behind upstream by {state.behind} commit(s)."
        )
    elif state.ahead:
        warnings.append(
            f"WARNING: memory repo has {state.ahead} unpushed commit(s); wrap-up will push."
        )
    return warnings


def staged_files(oacp_root: Path, runner: Optional[Runner] = None) -> List[str]:
    rc, output = _git(oacp_root, ["diff", "--cached", "--name-only"], runner)
    if rc != 0:
        raise MemorySyncError(f"git diff --cached failed: {output}")
    return [line for line in output.splitlines() if line.strip()]


def has_commits(oacp_root: Path, runner: Optional[Runner] = None) -> bool:
    rc, _ = _git(oacp_root, ["rev-parse", "--verify", "HEAD"], runner)
    return rc == 0


def make_commit_message(file_count: int, *, env: Optional[Dict[str, str]] = None) -> str:
    source = env if env is not None else os.environ
    agent = (
        source.get("OACP_AGENT")
        or source.get("AGENT_NAME")
        or source.get("USER")
        or "unknown"
    )
    host = socket.gethostname().split(".")[0] or "host"
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return f"memory: {agent}@{host} {today} ({file_count} files)"


def push_remote(oacp_root: Path, runner: Optional[Runner] = None) -> Tuple[int, str]:
    if not has_remote(oacp_root, runner):
        return 0, "No memory remote configured; commit remains local."
    upstream = configured_upstream(oacp_root, runner)
    if upstream:
        return _git(oacp_root, ["push"], runner)
    remote = default_remote(oacp_root, runner)
    branch = current_branch(oacp_root, runner)
    return _git(oacp_root, ["push", "-u", remote, branch], runner)


def pull_memory(oacp_root: Path, runner: Optional[Runner] = None) -> List[str]:
    """Advisory pull used by session-start hooks. Returns human output lines."""
    if not is_configured(oacp_root):
        return []
    ensure_memory_repo(oacp_root, runner)
    state = compute_git_state(oacp_root, runner=runner, fetch=True)
    lines = state_warnings(state)
    if state.dirty or state.diverged or state.ahead or state.fetch_failed:
        return lines
    if not state.has_upstream:
        if state.has_remote:
            return ["OACP memory pull: no upstream branch configured; skipping."]
        return ["OACP memory pull: local-only memory repo; no remote to pull."]
    if state.behind:
        behind = state.behind
        rc, output = _git(oacp_root, ["pull", "--ff-only"], runner)
        if rc != 0:
            return [*lines, f"WARNING: memory pull --ff-only failed: {output}"]
        return [f"OACP memory pull: synced {behind} commit(s)."]
    return ["OACP memory pull: already synced."]


def push_memory(oacp_root: Path, runner: Optional[Runner] = None) -> Tuple[List[str], int]:
    """Commit allowlisted memory changes and push when a remote exists."""
    if not is_configured(oacp_root):
        return [], 0
    ensure_memory_repo(oacp_root, runner)
    state = compute_git_state(oacp_root, runner=runner, fetch=True)
    lines = state_warnings(state, include_dirty=False)
    if state.diverged:
        return [
            *lines,
            "ERROR: memory repo is diverged; resolve manually before pushing.",
        ], 1
    if state.behind:
        return [
            *lines,
            "ERROR: memory repo is behind upstream; pull before pushing memory.",
        ], 1
    add_allowlist_paths(oacp_root, runner)
    files = staged_files(oacp_root, runner)
    if files:
        message = make_commit_message(len(files))
        rc, output = _git(oacp_root, ["commit", "-m", message], runner)
        if rc != 0:
            return [*lines, f"ERROR: memory commit failed: {output}"], 1
        lines.append(f"OACP memory push: committed {len(files)} file(s).")
    else:
        lines.append("OACP memory push: no memory changes to commit.")

    rc, output = push_remote(oacp_root, runner)
    if rc != 0:
        lines.append(
            "WARNING: memory push failed; commit remains local. "
            f"Resolve before assuming memory is synced. {output}".rstrip()
        )
        return lines, 1
    if output:
        lines.append(output)
    elif has_remote(oacp_root, runner):
        lines.append("OACP memory push: pushed to remote.")
    return lines, 0


def init_memory_repo(
    oacp_root: Path,
    *,
    remote: Optional[str] = None,
    runner: Optional[Runner] = None,
) -> List[str]:
    oacp_root.mkdir(parents=True, exist_ok=True)
    if not is_git_repo(oacp_root, runner):
        rc, output = _git(oacp_root, ["init"], runner)
        if rc != 0:
            raise MemorySyncError(f"git init failed: {output}")

    write_canonical_gitignore(oacp_root)
    write_marker(oacp_root)
    if remote:
        if remote_exists(oacp_root, "origin", runner):
            rc, output = _git(oacp_root, ["remote", "set-url", "origin", remote], runner)
            if rc != 0:
                raise MemorySyncError(f"git remote set-url failed: {output}")
        else:
            rc, output = _git(oacp_root, ["remote", "add", "origin", remote], runner)
            if rc != 0:
                raise MemorySyncError(f"git remote add failed: {output}")

    add_allowlist_paths(oacp_root, runner)
    files = staged_files(oacp_root, runner)
    lines: List[str] = []
    if files:
        rc, output = _git(
            oacp_root,
            ["commit", "-m", make_commit_message(len(files))],
            runner,
        )
        if rc != 0:
            raise MemorySyncError(f"initial memory commit failed: {output}")
        lines.append(f"Initialized OACP memory repo with {len(files)} file(s).")
    else:
        lines.append("OACP memory repo already initialized; no changes to commit.")

    if remote:
        rc, output = push_remote(oacp_root, runner)
        if rc != 0:
            lines.append(
                "WARNING: initial memory push failed; commit remains local. "
                f"{output}".rstrip()
            )
        else:
            lines.append("OACP memory remote configured.")
    return lines


def _is_non_empty(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def clone_memory_repo(
    oacp_root: Path,
    url: str,
    *,
    force: bool = False,
    runner: Optional[Runner] = None,
) -> List[str]:
    if _is_non_empty(oacp_root) and not force:
        raise MemorySyncError(
            f"Refusing to clone into non-empty OACP_HOME: {oacp_root}. "
            "Pass --force to move it aside first."
        )

    if not oacp_root.exists() or not _is_non_empty(oacp_root):
        oacp_root.parent.mkdir(parents=True, exist_ok=True)
        rc, output = _git(oacp_root.parent, ["clone", url, str(oacp_root)], runner)
        if rc != 0:
            raise MemorySyncError(f"git clone failed: {output}")
        return [f"Cloned OACP memory repo into {oacp_root}."]

    backup = oacp_root.with_name(
        f"{oacp_root.name}.backup-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    shutil.move(str(oacp_root), str(backup))
    try:
        rc, output = _git(oacp_root.parent, ["clone", url, str(oacp_root)], runner)
    except Exception:
        if not oacp_root.exists():
            shutil.move(str(backup), str(oacp_root))
        raise
    if rc != 0:
        if oacp_root.exists():
            shutil.rmtree(oacp_root)
        shutil.move(str(backup), str(oacp_root))
        raise MemorySyncError(f"git clone failed: {output}")
    return [
        f"Moved existing OACP_HOME aside to {backup}.",
        f"Cloned OACP memory repo into {oacp_root}.",
    ]


def disable_memory_repo(oacp_root: Path) -> List[str]:
    marker = marker_path(oacp_root)
    if marker.exists():
        marker.unlink()
        return [f"Removed {marker}; memory hooks are disabled locally."]
    return ["OACP memory sync already disabled."]


def normalize_gitignore(text: str) -> str:
    return text.replace("\r\n", "\n")


def is_allowed_memory_path(path: str) -> bool:
    if path in {".gitignore", MARKER_FILE}:
        return True
    if path.startswith("org-memory/"):
        return True
    parts = path.split("/")
    if len(parts) >= 4 and parts[0] == "projects" and parts[2] == "memory":
        if parts[3] == ".cache":
            return False
        return True
    return False


def tracked_files(oacp_root: Path, runner: Optional[Runner] = None) -> List[str]:
    rc, output = _git(oacp_root, ["ls-files"], runner)
    if rc != 0:
        raise MemorySyncError(f"git ls-files failed: {output}")
    return [line.strip() for line in output.splitlines() if line.strip()]


def untracked_files(oacp_root: Path, runner: Optional[Runner] = None) -> List[str]:
    rc, output = _git(
        oacp_root,
        ["ls-files", "--others", "--exclude-standard"],
        runner,
    )
    if rc != 0:
        raise MemorySyncError(f"git ls-files --others failed: {output}")
    return [line.strip() for line in output.splitlines() if line.strip()]


def overlay_gitignores(oacp_root: Path) -> Iterable[Path]:
    projects = oacp_root / "projects"
    if not projects.is_dir():
        return []
    return sorted(projects.glob("*/memory/.gitignore"))


def escaping_overlay_patterns(path: Path) -> List[str]:
    bad: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith("!"):
            continue
        pattern = line[1:].strip()
        parts = [part for part in pattern.replace("\\", "/").split("/") if part]
        if ".." in parts:
            bad.append(raw)
    return bad


def last_commit_age_days(
    oacp_root: Path,
    *,
    now: Optional[dt.datetime] = None,
    runner: Optional[Runner] = None,
) -> Optional[int]:
    if not has_commits(oacp_root, runner):
        return None
    rc, output = _git(oacp_root, ["log", "-1", "--format=%ct"], runner)
    if rc != 0:
        return None
    try:
        ts = int(output.strip())
    except ValueError:
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    commit_time = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    return max(0, int((current - commit_time).total_seconds() // 86400))
