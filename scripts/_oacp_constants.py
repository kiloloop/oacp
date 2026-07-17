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
# The protocol spec version the tooling implements. Stamped into audit
# records, compiled envelopes, and workspace.json at init so every artifact
# names the contract it was produced under.
SPEC_VERSION = "0.4.0"
CREATABLE_RUNTIMES = ("claude", "codex", "cursor", "gemini")
ALL_RUNTIMES = ("claude", "codex", "cursor", "gemini", "human", "unknown")
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
