# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Shared constants and helpers for OACP scripts."""

from __future__ import annotations

import datetime as dt
import re
from contextlib import nullcontext
from importlib import resources
from pathlib import Path

AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CREATABLE_RUNTIMES = ("claude", "codex", "gemini")
ALL_RUNTIMES = ("claude", "codex", "gemini", "human", "unknown")
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
