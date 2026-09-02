#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Add an agent to an existing OACP project workspace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from _oacp_constants import (
    AGENT_RE,
    CREATABLE_RUNTIMES,
    _template_path,
    _write_if_missing,
    utc_now_iso,
)
from agent_profile import load_global_profile, upsert_global_profile

AGENT_SUBDIRS = (
    "inbox",
    "inbox/archive",
    "outbox",
    "dead_letter",
    "audit/autonomy_decisions",
)

CODEX_SUPPORTED_MESSAGE_TYPES = (
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
)

CLAUDE_SUPPORTED_MESSAGE_TYPES = (
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
)


def _load_runtime_capabilities() -> Dict[str, Any]:
    """Load runtime_capabilities.yaml template."""
    with _template_path("runtime_capabilities.yaml") as path:
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required for --runtime support: pip install pyyaml"
            )
        return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_template(relative: str) -> str:
    """Load a template file and return its text content."""
    with _template_path(relative) as path:
        return path.read_text(encoding="utf-8")


def _receiver_config_template() -> str:
    """Load the default receiver config template."""
    return _load_template("receiver_config.template.yaml")


def _render_status_yaml(
    agent_name: str, runtime: str, caps: Dict[str, Any]
) -> str:
    """Render a status.yaml for the agent from the capabilities template."""
    model = str(caps.get("model", runtime)).strip() or "unknown"
    if runtime in ("claude", "codex") and model == runtime:
        model = "unknown"
    lines = [
        f"runtime: {runtime}",
        f"model: {model}",
        "status: available",
        'current_task: ""',
        "capabilities:",
    ]
    for cap in caps.get("capabilities", []):
        lines.append(f"  - {cap}")
    lines.append(f'updated_at: "{utc_now_iso()}"')
    lines.append("")
    return "\n".join(lines)


def _render_agent_card_yaml(
    agent_name: str,
    runtime: str,
    caps: Dict[str, Any],
    global_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """Render an agent_card.yaml by filling in the template placeholders.

    If *global_profile* is provided, its identity fields (model, description)
    are used as defaults instead of the runtime_capabilities template.
    """
    template = _load_template("agent_card.template.yaml")
    # Fill in identity fields
    template = template.replace(
        'name: ""', f'name: "{agent_name}"', 1
    )
    template = template.replace(
        'runtime: ""', f'runtime: "{runtime}"', 1
    )
    model = caps.get("model", runtime)
    # Escape quotes and newlines for safe YAML scalar interpolation
    model = str(model).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    template = template.replace(
        'model: ""', f'model: "{model}"', 1
    )
    description = f"{agent_name} agent ({runtime} runtime)"
    if global_profile and global_profile.get("description"):
        # Escape quotes and newlines for safe YAML scalar interpolation
        raw = str(global_profile["description"])
        description = raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    template = template.replace(
        'description: ""',
        f'description: "{description}"',
        1,
    )
    # Strip profile-tier sections from scaffolded cards — these fields are
    # meant to be inherited from the global profile, not set per-project.
    # Keeping them in the card template (for documentation) but removing from
    # rendered output prevents template defaults from masking global values.
    lines = template.split("\n")
    filtered: List[str] = []
    skip_section = False
    for line in lines:
        stripped = line.strip()
        # Detect section headers to skip
        if stripped.startswith("# ── Routing") or stripped.startswith("# ── Trust"):
            skip_section = True
            continue
        # Stop skipping at the next major section
        if skip_section and stripped.startswith("# ──"):
            skip_section = False
        if skip_section:
            continue
        filtered.append(line)
    template = "\n".join(filtered)

    # Fill in protocol paths
    template = template.replace(
        'inbox_path: ""',
        f'inbox_path: "agents/{agent_name}/inbox/"',
        1,
    )
    template = template.replace(
        'outbox_path: ""',
        f'outbox_path: "agents/{agent_name}/outbox/"',
        1,
    )
    if runtime == "claude":
        template, _ = _merge_supported_message_types_yaml(
            template,
            CLAUDE_SUPPORTED_MESSAGE_TYPES,
        )
    elif runtime == "codex":
        template, _ = _merge_supported_message_types_yaml(
            template,
            CODEX_SUPPORTED_MESSAGE_TYPES,
        )
    return template


def _merge_supported_message_types_yaml(
    raw: str,
    required_types: Sequence[str],
) -> tuple[str, bool]:
    """Append missing message types while preserving an agent card's formatting."""
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid agent card YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("agent card must contain a top-level mapping")
    protocol = payload.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("agent card protocol must be a mapping")
    current_types = protocol.get("supported_message_types")
    if not isinstance(current_types, list) or not all(
        isinstance(item, str) for item in current_types
    ):
        raise ValueError("agent card supported_message_types must be a list of strings")

    missing_types = [item for item in required_types if item not in current_types]
    if not missing_types:
        return raw, False

    lines = raw.splitlines(keepends=True)
    field_index: Optional[int] = None
    field_indent = 0
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        stripped = content.lstrip(" ")
        if stripped.startswith("supported_message_types:"):
            suffix = stripped.removeprefix("supported_message_types:").strip()
            if suffix and not suffix.startswith("#"):
                raise ValueError(
                    "agent card supported_message_types must use block-list syntax"
                )
            field_index = index
            field_indent = len(content) - len(stripped)
            break
    if field_index is None:
        raise ValueError("agent card is missing protocol.supported_message_types")

    # Walk the item lines to find where the list ends. Block-sequence items may
    # sit at the SAME indentation as the key (valid YAML), so the item indent is
    # taken from the first item actually found, never assumed deeper.
    insert_index: Optional[int] = None
    item_prefix: Optional[str] = None
    item_indent = 0
    for index in range(field_index + 1, len(lines)):
        content = lines[index].rstrip("\r\n")
        stripped = content.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(content) - len(stripped)
        if item_prefix is None:
            if indent >= field_indent and stripped.startswith("- "):
                item_prefix = content[:indent]
                item_indent = indent
                insert_index = index + 1
                continue
            break
        if (indent == item_indent and stripped.startswith("- ")) or indent > item_indent:
            insert_index = index + 1
            continue
        break
    if item_prefix is None or insert_index is None:
        raise ValueError("failed to locate supported_message_types list items")

    newline = "\r\n" if "\r\n" in raw else "\n"
    additions = [f"{item_prefix}- {item}{newline}" for item in missing_types]
    head = list(lines[:insert_index])
    if head and not head[-1].endswith(("\n", "\r")):
        head[-1] += newline
    updated = "".join([*head, *additions, *lines[insert_index:]])

    try:
        updated_payload = yaml.safe_load(updated)
        updated_types = updated_payload["protocol"]["supported_message_types"]
    except (yaml.YAMLError, KeyError, TypeError) as exc:
        raise ValueError(f"failed to preserve agent card YAML structure: {exc}") from exc
    if updated_types != [*current_types, *missing_types]:
        raise ValueError("failed to preserve required supported message types")
    return updated, True


def ensure_agent_card_message_types(
    card_path: Path,
    required_types: Sequence[str],
) -> bool:
    """Update one existing card with missing message types only."""
    raw = card_path.read_text(encoding="utf-8")
    updated, changed = _merge_supported_message_types_yaml(raw, required_types)
    if changed:
        card_path.write_text(updated, encoding="utf-8")
    return changed


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add an agent to an existing OACP project workspace.",
    )
    parser.add_argument("project_name", help="Project workspace name")
    parser.add_argument("agent_name", help="Agent name (alphanumeric, dots, hyphens, underscores; max 64 chars)")
    parser.add_argument(
        "--runtime",
        choices=CREATABLE_RUNTIMES,
        default=None,
        help="Agent runtime — generates status.yaml and agent_card.yaml with defaults",
    )
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    return parser.parse_args(list(argv))


def _validate_project_name(name: str) -> None:
    if name.startswith(".") or "/" in name or "\\" in name:
        raise ValueError(
            "project name must not contain path separators or start with '.'"
        )


def add_agent(
    project_name: str,
    agent_name: str,
    *,
    oacp_root: Path,
    runtime: Optional[str] = None,
) -> Dict[str, Any]:
    """Add an agent to a project workspace.

    Returns a dict with ``agent_dir``, ``created_files``, and ``skipped_files``.
    """
    _validate_project_name(project_name)

    if not AGENT_RE.match(agent_name):
        raise ValueError(
            f"Invalid agent name '{agent_name}': must match {AGENT_RE.pattern}"
        )

    if runtime is not None and runtime not in CREATABLE_RUNTIMES:
        raise ValueError(
            f"Invalid runtime '{runtime}': must be one of {CREATABLE_RUNTIMES}"
        )

    project_dir = oacp_root / "projects" / project_name
    if not project_dir.is_dir():
        raise ValueError(
            f"Project '{project_name}' not found at {project_dir}"
        )

    agent_dir = project_dir / "agents" / agent_name
    created_files: List[str] = []
    skipped_files: List[str] = []

    # Create subdirectories with .gitkeep
    for subdir in AGENT_SUBDIRS:
        dirpath = agent_dir / subdir
        dirpath.mkdir(parents=True, exist_ok=True)
        gitkeep = dirpath / ".gitkeep"
        if _write_if_missing(gitkeep, ""):
            created_files.append(str(gitkeep.relative_to(project_dir)))
        else:
            skipped_files.append(str(gitkeep.relative_to(project_dir)))

    config_path = agent_dir / "config.yaml"
    if _write_if_missing(config_path, _receiver_config_template()):
        created_files.append(str(config_path.relative_to(project_dir)))
    else:
        skipped_files.append(str(config_path.relative_to(project_dir)))

    # Check for global profile defaults
    global_profile = None
    try:
        global_profile = load_global_profile(oacp_root, agent_name)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"could not load global profile for '{agent_name}': {exc}"
        ) from exc

    # Optional runtime-specific files
    registry_model: Optional[str] = None
    registry_description: Optional[str] = None
    if runtime is not None:
        caps = _load_runtime_capabilities().get(runtime, {})
        # If global profile exists, use its identity fields as defaults
        if global_profile:
            if global_profile.get("model"):
                caps["model"] = global_profile["model"]

        status_content = _render_status_yaml(agent_name, runtime, caps)
        status_path = agent_dir / "status.yaml"
        if _write_if_missing(status_path, status_content):
            created_files.append(str(status_path.relative_to(project_dir)))
        else:
            skipped_files.append(str(status_path.relative_to(project_dir)))

        card_content = _render_agent_card_yaml(agent_name, runtime, caps, global_profile)
        card_path = agent_dir / "agent_card.yaml"
        if _write_if_missing(card_path, card_content):
            created_files.append(str(card_path.relative_to(project_dir)))
        else:
            skipped_files.append(str(card_path.relative_to(project_dir)))

        registry_model = str(caps.get("model", runtime))
        registry_description = f"{agent_name} agent ({runtime} runtime)"

    registry_runtime = runtime or (
        agent_name if agent_name in CREATABLE_RUNTIMES else "unknown"
    )
    registry_update = upsert_global_profile(
        oacp_root,
        agent_name,
        registry_runtime,
        projects=[project_name],
        model=registry_model,
        description=registry_description,
    )

    return {
        "agent_dir": agent_dir,
        "created_files": created_files,
        "skipped_files": skipped_files,
        "registry_update": registry_update,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    from _oacp_env import resolve_oacp_home

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    try:
        result = add_agent(
            args.project_name,
            args.agent_name,
            oacp_root=oacp_root,
            runtime=args.runtime,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    agent_dir = result["agent_dir"]
    print(f"Agent '{args.agent_name}' added to project '{args.project_name}':")
    print(f"  {agent_dir}")
    if result["created_files"]:
        for f in result["created_files"]:
            print(f"  + {f}")
    if result["skipped_files"]:
        for f in result["skipped_files"]:
            print(f"  ~ {f} (already exists, skipped)")
    registry = result["registry_update"]
    marker = "+" if registry["action"] == "created" else "~"
    print(
        f"  {marker} {registry['path']} "
        f"(registry {registry['action']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
