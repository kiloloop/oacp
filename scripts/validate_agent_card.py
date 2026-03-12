#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Schema validation for agent card YAML files.

Validates agent card files against the canonical schema defined in
templates/agent_card.template.yaml. Agent cards declare static identity,
skills, capabilities, permissions, and protocol bindings for each agent.

Usage:
    validate_agent_card.py <file> [<file> ...]
    validate_agent_card.py --dir <directory>

Exit codes:
    0 — all files valid
    1 — validation errors found
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = (
    "version",
    "name",
    "runtime",
    "description",
)

OPTIONAL_FIELDS = (
    "model",
    "skills",
    "capabilities",
    "permissions",
    "availability",
    "protocol",
)

ALLOWED_FIELDS = set(REQUIRED_FIELDS + OPTIONAL_FIELDS)

ALLOWED_RUNTIMES = {"claude", "codex", "gemini", "human", "unknown"}

ALLOWED_MESSAGE_TYPES = {
    "task_request",
    "question",
    "notification",
    "handoff",
    "handoff_complete",
    "review_request",
    "review_feedback",
    "review_addressed",
    "review_lgtm",
    "follow_up",
    "brainstorm_request",
    "brainstorm_followup",
}

# Canonical 13 capability keys from runtime_capabilities.md
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

SKILL_REQUIRED_FIELDS = {"id", "name", "description"}
SKILL_OPTIONAL_FIELDS = {"tags", "examples"}
SKILL_ALLOWED_FIELDS = SKILL_REQUIRED_FIELDS | SKILL_OPTIONAL_FIELDS

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

ALLOWED_GITHUB_OPS = {
    "pr_comment",
    "pr_approve",
    "pr_merge",
    "pr_create",
    "issue_create",
    "issue_comment",
    "issue_close",
}

# ---------------------------------------------------------------------------
# YAML loading (with fallback)
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file, trying PyYAML first then falling back to simple parser."""
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
    except ImportError:
        data = _parse_simple_yaml(raw)
    except Exception as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("top-level YAML must be a mapping")
    return data


def _parse_simple_yaml(raw: str) -> Dict[str, Any]:
    """Minimal YAML parser for flat/nested key-value mappings with lists."""
    data: Dict[str, Any] = {}
    lines = raw.splitlines()
    i = 0
    current_section: str | None = None
    current_subsection: str | None = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        indent = len(line) - len(line.lstrip())

        # List item
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if current_subsection and current_section:
                section = data.setdefault(current_section, {})
                if not isinstance(section, dict):
                    i += 1
                    continue
                lst = section.setdefault(current_subsection, [])
                if not isinstance(lst, list):
                    section[current_subsection] = []
                    lst = section[current_subsection]
                # Check if it's a dict item (has colon)
                if ":" in item_text:
                    item_dict: Dict[str, Any] = {}
                    key, rest = item_text.split(":", 1)
                    item_dict[key.strip()] = _unquote(rest.strip())
                    # Read continuation lines at deeper indent
                    i += 1
                    while i < len(lines):
                        next_line = lines[i]
                        next_stripped = next_line.strip()
                        next_indent = len(next_line) - len(next_line.lstrip())
                        if not next_stripped or next_stripped.startswith("#"):
                            i += 1
                            continue
                        if next_indent <= indent:
                            break
                        if ":" in next_stripped:
                            k, v = next_stripped.split(":", 1)
                            k = k.strip()
                            v = v.strip()
                            if v.startswith("[") and v.endswith("]"):
                                item_dict[k] = [
                                    _unquote(x.strip())
                                    for x in v[1:-1].split(",")
                                    if x.strip()
                                ]
                            else:
                                item_dict[k] = _unquote(v)
                        i += 1
                    lst.append(item_dict)
                    continue
                else:
                    lst.append(_unquote(item_text))
            elif current_section:
                lst = data.setdefault(current_section, [])
                if not isinstance(lst, list):
                    data[current_section] = []
                    lst = data[current_section]
                lst.append(_unquote(item_text))
            i += 1
            continue

        if ":" not in line:
            i += 1
            continue

        key, rest = line.split(":", 1)
        key = key.strip()
        rest = rest.strip()

        if indent == 0:
            if not rest:
                current_section = key
                current_subsection = None
                # Initialize list-typed top-level fields as [] not {}
                if key == "skills":
                    data.setdefault(key, [])
                else:
                    data.setdefault(key, {})
            elif rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1]
                data[key] = [_unquote(x.strip()) for x in inner.split(",") if x.strip()]
                current_section = None
                current_subsection = None
            else:
                data[key] = _unquote(_strip_comment(rest))
                current_section = None
                current_subsection = None
        elif indent > 0 and current_section:
            section = data.setdefault(current_section, {})
            if isinstance(section, dict):
                if not rest:
                    current_subsection = key
                    section.setdefault(key, [])
                elif rest.startswith("[") and rest.endswith("]"):
                    inner = rest[1:-1]
                    section[key] = [_unquote(x.strip()) for x in inner.split(",") if x.strip()]
                    current_subsection = None
                else:
                    section[key] = _unquote(_strip_comment(rest))
                    current_subsection = None

        i += 1

    return data


def _strip_comment(value: str) -> str:
    """Strip inline YAML comments (# ...) from a scalar value."""
    in_quote = False
    quote_char = ""
    for idx, ch in enumerate(value):
        if ch in ("'", '"') and not in_quote:
            in_quote = True
            quote_char = ch
        elif ch == quote_char and in_quote:
            in_quote = False
        elif ch == "#" and not in_quote:
            return value[:idx].strip()
    return value.strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    if value == "null" or value == "~":
        return ""
    return value


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_agent_card(data: Dict[str, Any]) -> List[str]:
    """Validate an agent card YAML dict. Returns list of error strings."""
    errors: List[str] = []

    # Check for unknown top-level fields
    unknown = sorted(set(data.keys()) - ALLOWED_FIELDS)
    if unknown:
        errors.append(f"unknown field(s): {', '.join(unknown)}")

    # Required fields
    for field in REQUIRED_FIELDS:
        val = data.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"missing or empty required field: {field}")

    # version — semver format
    version = str(data.get("version", "")).strip()
    if version and not VERSION_RE.fullmatch(version):
        errors.append("field 'version' must be semver format: X.Y.Z")

    # name — safe identifier
    name = str(data.get("name", "")).strip()
    if name and not NAME_RE.fullmatch(name):
        errors.append(
            "field 'name' must be 1-64 chars of alphanumeric, dots, hyphens, or underscores"
        )

    # runtime
    runtime = str(data.get("runtime", "")).strip()
    if runtime and runtime not in ALLOWED_RUNTIMES:
        errors.append(
            f"field 'runtime' must be one of: {', '.join(sorted(ALLOWED_RUNTIMES))}"
        )

    # skills — list of structured objects
    skills = data.get("skills")
    if skills is not None:
        if not isinstance(skills, list):
            errors.append("field 'skills' must be a list")
        else:
            skill_ids: set[str] = set()
            for idx, skill in enumerate(skills):
                if not isinstance(skill, dict):
                    errors.append(f"skills[{idx}]: must be a mapping")
                    continue
                # Required skill fields
                for sf in SKILL_REQUIRED_FIELDS:
                    sv = skill.get(sf)
                    if sv is None or (isinstance(sv, str) and not sv.strip()):
                        errors.append(f"skills[{idx}]: missing or empty required field: {sf}")
                # Unknown skill fields
                unknown_sf = sorted(set(skill.keys()) - SKILL_ALLOWED_FIELDS)
                if unknown_sf:
                    errors.append(f"skills[{idx}]: unknown field(s): {', '.join(unknown_sf)}")
                # Unique skill id
                sid = str(skill.get("id", "")).strip()
                if sid:
                    if sid in skill_ids:
                        errors.append(f"skills[{idx}]: duplicate skill id: {sid}")
                    skill_ids.add(sid)
                # tags must be a list
                tags = skill.get("tags")
                if tags is not None and not isinstance(tags, list):
                    errors.append(f"skills[{idx}]: 'tags' must be a list")

    # capabilities — dict with list values
    caps = data.get("capabilities")
    if caps is not None:
        if not isinstance(caps, dict):
            errors.append("field 'capabilities' must be a mapping")
        else:
            for cap_key in ("tools", "languages", "domains"):
                cap_val = caps.get(cap_key)
                if cap_val is not None and not isinstance(cap_val, list):
                    errors.append(f"capabilities.{cap_key} must be a list")

    # permissions
    perms = data.get("permissions")
    if perms is not None:
        if not isinstance(perms, dict):
            errors.append("field 'permissions' must be a mapping")
        else:
            for pf in ("allowed_dirs", "denied_dirs", "allowed_commands", "denied_commands",
                        "github_operations"):
                pv = perms.get(pf)
                if pv is not None and not isinstance(pv, list):
                    errors.append(f"permissions.{pf} must be a list")
            # Validate github_operations entries against allowlist
            gh_ops = perms.get("github_operations")
            if isinstance(gh_ops, list):
                for op in gh_ops:
                    op_str = str(op).strip()
                    if op_str and op_str not in ALLOWED_GITHUB_OPS:
                        errors.append(
                            f"permissions.github_operations: unknown operation '{op_str}'"
                        )

    # availability
    avail = data.get("availability")
    if avail is not None:
        if not isinstance(avail, dict):
            errors.append("field 'availability' must be a mapping")
        else:
            mct = avail.get("max_concurrent_tasks")
            if mct is not None:
                try:
                    mct_val = int(mct)
                    if mct_val <= 0:
                        errors.append("availability.max_concurrent_tasks must be a positive integer")
                except (ValueError, TypeError):
                    errors.append("availability.max_concurrent_tasks must be an integer")

    # protocol
    proto = data.get("protocol")
    if proto is not None:
        if not isinstance(proto, dict):
            errors.append("field 'protocol' must be a mapping")
        else:
            smt = proto.get("supported_message_types")
            if smt is not None:
                if not isinstance(smt, list):
                    errors.append("protocol.supported_message_types must be a list")
                else:
                    for mt in smt:
                        mt_str = str(mt).strip()
                        if mt_str and mt_str not in ALLOWED_MESSAGE_TYPES:
                            errors.append(
                                f"protocol.supported_message_types: unknown type '{mt_str}'"
                            )

    return errors


def validate_agent_card_file(path: Path) -> List[str]:
    """Validate an agent card YAML file. Returns list of error strings."""
    if not path.is_file():
        return [f"file does not exist: {path}"]
    try:
        data = _load_yaml(path)
    except ValueError as exc:
        return [str(exc)]
    return validate_agent_card(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate agent card YAML files against the canonical schema."
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Path(s) to agent card YAML file(s).",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress success output")
    parser.add_argument(
        "--dir",
        action="store_true",
        help="Treat arguments as directories — validate all agent_card.yaml files recursively",
    )
    args = parser.parse_args()

    paths: list[Path] = []
    for f in args.files:
        p = Path(f)
        if args.dir and p.is_dir():
            paths.extend(sorted(p.rglob("agent_card.yaml")))
        else:
            paths.append(p)

    if not paths:
        print("No files to validate.", file=sys.stderr)
        return 1

    total_errors = 0
    for path in paths:
        errs = validate_agent_card_file(path)
        if errs:
            total_errors += len(errs)
            for err in errs:
                print(f"ERROR [{path}]: {err}", file=sys.stderr)
        elif not args.quiet:
            print(f"OK: {path}")

    if total_errors > 0:
        print(f"\n{total_errors} error(s) in {len(paths)} file(s).", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"\nAll {len(paths)} file(s) valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
