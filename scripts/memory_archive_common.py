#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for active/archive memory file operations."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Tuple


ACTIVE_MEMORY_FILES = (
    "project_facts.md",
    "decision_log.md",
    "open_threads.md",
    "known_debt.md",
)

_SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ARCHIVED_BASENAME_RE = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6}Z)_(?P<basename>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)


def validate_project_name(project_name: str) -> None:
    if project_name.startswith(".") or "/" in project_name or "\\" in project_name:
        raise ValueError("project name must not contain path separators or start with '.'")


def validate_memory_basename(file_name: str) -> None:
    if "/" in file_name or "\\" in file_name or not _SAFE_BASENAME_RE.fullmatch(file_name):
        raise ValueError(
            "memory file name must be a simple basename containing only "
            "[A-Za-z0-9._-]"
        )


def project_memory_paths(oacp_root: Path, project_name: str) -> Tuple[Path, Path, Path]:
    validate_project_name(project_name)
    project_dir = oacp_root / "projects" / project_name
    if not project_dir.is_dir():
        raise ValueError(f"project '{project_name}' not found at {project_dir}")
    memory_dir = project_dir / "memory"
    archive_dir = memory_dir / "archive"
    return project_dir, memory_dir, archive_dir


def build_archive_name(
    memory_file: str, now: dt.datetime | None = None
) -> str:
    validate_memory_basename(memory_file)
    current = now or dt.datetime.now(dt.timezone.utc)
    timestamp = current.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{memory_file}"


def original_name_from_archive(archived_file: str) -> str:
    if "/" in archived_file or "\\" in archived_file:
        raise ValueError("archived file name must be a simple basename")
    match = _ARCHIVED_BASENAME_RE.fullmatch(archived_file)
    if match is None:
        raise ValueError(
            "archived file name must match <UTC timestamp>_<basename>"
        )
    return match.group("basename")
