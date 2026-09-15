# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Shared constants and helpers for OACP scripts."""

from __future__ import annotations

import datetime as dt
import re
from contextlib import contextmanager, nullcontext
from importlib import resources
from pathlib import Path

AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# The protocol contract version the tooling implements. Stamped into audit
# records, compiled envelopes, and workspace.json at init so every artifact
# names the contract it was produced under. Moves only under the
# contract-version rule (docs/protocol/autonomy.md, "Contract version");
# the spec's own literals must agree (tests/test_spec_version.py).
SPEC_VERSION = "0.5.2"
CREATABLE_RUNTIMES = ("claude", "codex", "cursor", "gemini")
ALL_RUNTIMES = ("claude", "codex", "cursor", "gemini", "human", "unknown")
CODEX_SESSION_START_CONTEXT_LIMIT = 2_500
CANONICAL_CAPABILITIES = {
    "headless",
    "mcp_tools",
    "shell_access",
    "git_ops",
    "github_cli",
    "subagents",
    "parallel_teams",
    "web_search",
    "browser",
    "session_memory",
    "notifications",
    "async_tasks",
    "image_generation",
}


def is_agent_dir(path: Path) -> bool:
    """Return whether *path* is a visible agent directory."""
    return path.is_dir() and not path.name.startswith(".")


def utc_now_iso(now: dt.datetime | None = None) -> str:
    """Return a UTC RFC3339 timestamp with seconds precision."""
    base = now or dt.datetime.now(dt.timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=dt.timezone.utc)
    else:
        base = base.astimezone(dt.timezone.utc)
    return base.strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def locked_audit(audit_path: Path):
    """Serialize read-modify-write access to an autonomy audit record.

    EVERY audit writer (human-outcome recording, message_auth attachment,
    and any future result-block writer) must hold this lock from before
    reading the record until after the atomic replace. The lock lives on a
    stable sibling ``<name>.lock`` file that is never replaced — locking
    the audit file's own inode is unsound because atomic-replace updates
    swap the inode under waiters, so stale-lock holders would each
    "win" and silently drop each other's blocks. The empty ``.lock``
    sibling persists; it carries no data. POSIX-only (flock), matching the
    existing audit-writer requirement.
    """
    import fcntl

    audit_path = Path(audit_path)
    lock_path = audit_path.with_name(audit_path.name + ".lock")
    with open(lock_path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_replace_yaml(path: Path, data: dict) -> None:
    """Atomically replace a YAML file in place, preserving its mode.

    The shared write half of every audit read-modify-write: dump, fsync a
    sibling temp file, then rename over the original so readers never see
    a partial record. Callers must already hold ``locked_audit`` on the
    target. YAML import stays local so this module keeps loading in
    environments without pyyaml (doctor degrades gracefully there).
    """
    import os
    import tempfile

    import yaml

    path = Path(path)
    content = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    mode = path.stat().st_mode
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def atomic_replace_bytes(path: Path, data: bytes) -> None:
    """Atomically replace a file with exact bytes, preserving its mode.

    The restore half of a rolled-back multi-file audit transaction: the
    original bytes go back verbatim (not a re-serialization, which would
    normalize formatting and collapse duplicate-key evidence). Same
    fsync + rename discipline as ``atomic_replace_yaml``; callers must
    already hold ``locked_audit`` on the target.
    """
    import os
    import tempfile

    path = Path(path)
    mode = path.stat().st_mode
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_if_missing(path: Path, content: str) -> bool:
    """Write content to *path* only if it does not already exist."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _template_path(relative: str):
    """Resolve a template file from the repo tree or installed package."""
    repo_template = Path(__file__).resolve().parent.parent / "templates" / relative
    if repo_template.is_file():
        return nullcontext(repo_template)
    resource = resources.files("oacp").joinpath("_templates", relative)
    return resources.as_file(resource)
