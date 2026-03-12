#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Validation helpers for structured handoff message bodies."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

AGENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _is_nonempty_scalar(value: str) -> bool:
    value = _unquote(value.strip())
    if not value:
        return False
    if value in ("[]", "{}", "null", "~"):
        return False
    return True


def _find_key_line(
    lines: List[str],
    key: str,
    indent: int,
    *,
    start: int = 0,
    end: Optional[int] = None,
) -> Tuple[int, str]:
    """Return (line_index, value_after_colon) or (-1, "")."""
    if end is None:
        end = len(lines)
    pattern = re.compile(rf"^{re.escape(' ' * indent)}{re.escape(key)}\s*:\s*(.*)$")
    for idx in range(start, end):
        match = pattern.match(lines[idx].rstrip())
        if match:
            return idx, match.group(1).strip()
    return -1, ""


def _find_block_end(lines: List[str], start_idx: int, indent: int) -> int:
    """Find the exclusive end index for a YAML block rooted at indent."""
    idx = start_idx + 1
    while idx < len(lines):
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        if _line_indent(line) <= indent:
            break
        idx += 1
    return idx


def _has_list_item(lines: List[str], key_idx: int, key_indent: int) -> bool:
    """Check whether key has at least one child list item."""
    end = _find_block_end(lines, key_idx, key_indent)
    for idx in range(key_idx + 1, end):
        line = lines[idx]
        if not line.strip():
            continue
        if _line_indent(line) <= key_indent:
            continue
        if line.lstrip().startswith("- "):
            tail = line.lstrip()[2:].strip()
            if tail:
                return True
    return False


def validate_handoff_packet_text(text: str) -> List[str]:
    """Validate message body for `type: handoff`."""
    errors: List[str] = []
    lines = text.splitlines()

    required_scalars = ("source_agent", "target_agent", "intent")
    scalars = {}

    for key in required_scalars:
        idx, value = _find_key_line(lines, key, 0)
        if idx < 0:
            errors.append(f"missing required field '{key}'")
            continue
        if not _is_nonempty_scalar(value):
            errors.append(f"field '{key}' must be non-empty")
            continue
        scalars[key] = _unquote(value)

    source = scalars.get("source_agent")
    target = scalars.get("target_agent")
    if source and not AGENT_RE.fullmatch(source):
        errors.append("field 'source_agent' must match ^[A-Za-z0-9._-]{1,64}$")
    if target and not AGENT_RE.fullmatch(target):
        errors.append("field 'target_agent' must match ^[A-Za-z0-9._-]{1,64}$")
    if source and target and source == target:
        errors.append("field 'target_agent' must differ from 'source_agent'")

    for key in ("artifacts_to_review", "definition_of_done"):
        idx, value = _find_key_line(lines, key, 0)
        if idx < 0:
            errors.append(f"missing required field '{key}'")
            continue
        if value:
            if not _is_nonempty_scalar(value):
                errors.append(f"field '{key}' must contain at least one entry")
            continue
        if not _has_list_item(lines, idx, 0):
            errors.append(f"field '{key}' must contain at least one list item")

    context_idx, _ = _find_key_line(lines, "context_bundle", 0)
    if context_idx < 0:
        errors.append("missing required field 'context_bundle'")
        return errors

    context_end = _find_block_end(lines, context_idx, 0)
    for key in ("files_touched", "decisions_made", "blockers_hit", "suggested_next_steps"):
        idx, value = _find_key_line(lines, key, 2, start=context_idx + 1, end=context_end)
        if idx < 0:
            errors.append(f"missing context_bundle field '{key}'")
            continue
        if value:
            if not _is_nonempty_scalar(value):
                errors.append(f"context_bundle '{key}' must contain at least one entry")
            continue
        if not _has_list_item(lines, idx, 2):
            errors.append(f"context_bundle '{key}' must contain at least one list item")

    return errors


def validate_handoff_complete_text(text: str) -> List[str]:
    """Validate message body for `type: handoff_complete`."""
    errors: List[str] = []
    lines = text.splitlines()

    required_scalars = ("issue", "pr", "branch", "tests_run", "next_owner")
    values = {}
    for key in required_scalars:
        _, value = _find_key_line(lines, key, 0)
        if not _is_nonempty_scalar(value):
            errors.append(f"missing required field '{key}'")
            continue
        values[key] = _unquote(value)

    pr = values.get("pr", "")
    if pr and not re.fullmatch(r"#?\d+", pr):
        errors.append("field 'pr' must be numeric (e.g., 83 or #83)")

    owner = values.get("next_owner", "")
    if owner and not AGENT_RE.fullmatch(owner):
        errors.append("field 'next_owner' must match ^[A-Za-z0-9._-]{1,64}$")

    return errors
