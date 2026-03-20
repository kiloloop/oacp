#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Add an agent to an existing OACP project workspace."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from contextlib import nullcontext
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VALID_RUNTIMES = ("claude", "codex", "gemini")
AGENT_SUBDIRS = ("inbox", "outbox", "dead_letter")


def _template_path(relative: str):
    """Resolve a template file — repo tree first, installed package fallback."""
    repo_template = Path(__file__).resolve().parent.parent / "templates" / relative
    if repo_template.is_file():
        return nullcontext(repo_template)
    resource = resources.files("oacp").joinpath("_templates", relative)
    return resources.as_file(resource)


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


def _write_if_missing(path: Path, content: str) -> bool:
    """Write content to *path* only if it does not already exist.

    Returns True if the file was written, False if skipped.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _render_status_yaml(
    agent_name: str, runtime: str, caps: Dict[str, Any]
) -> str:
    """Render a status.yaml for the agent from the capabilities template."""
    lines = [
        f"runtime: {runtime}",
        f"model: {caps.get('model', runtime)}",
        "status: available",
        'current_task: ""',
        "capabilities:",
    ]
    for cap in caps.get("capabilities", []):
        lines.append(f"  - {cap}")
    lines.append(
        f'updated_at: "{dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )
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
    return template


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add an agent to an existing OACP project workspace.",
    )
    parser.add_argument("project_name", help="Project workspace name")
    parser.add_argument("agent_name", help="Agent name (alphanumeric, dots, hyphens, underscores; max 64 chars)")
    parser.add_argument(
        "--runtime",
        choices=VALID_RUNTIMES,
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

    if runtime is not None and runtime not in VALID_RUNTIMES:
        raise ValueError(
            f"Invalid runtime '{runtime}': must be one of {VALID_RUNTIMES}"
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

    # Check for global profile defaults
    global_profile = None
    global_profile_path = oacp_root / "agents" / agent_name / "profile.yaml"
    if global_profile_path.is_file():
        try:
            if yaml is not None:
                global_profile = yaml.safe_load(
                    global_profile_path.read_text(encoding="utf-8")
                )
                if not isinstance(global_profile, dict):
                    global_profile = None
        except Exception as exc:
            print(f"Warning: could not load global profile for '{agent_name}': {exc}", file=sys.stderr)
            global_profile = None

    # Optional runtime-specific files
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

    return {
        "agent_dir": agent_dir,
        "created_files": created_files,
        "skipped_files": skipped_files,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
