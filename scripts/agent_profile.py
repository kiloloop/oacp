#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Two-tier agent profile management.

Global profiles live at $OACP_HOME/agents/<name>/profile.yaml and provide
identity defaults inherited by project-level agent cards. Project cards
override global fields using shallow dict merge with list replacement.

Subcommands:
    agent init <name> --runtime <rt>   Scaffold a global profile
    agent show <name> [--project <p>]  Print merged profile YAML
    agent list [--project <p>]         List known agents

Usage:
    agent_profile.py init <name> --runtime <runtime>
    agent_profile.py show <name> [--project <project>]
    agent_profile.py list [--project <project>]
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from contextlib import nullcontext
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_RUNTIMES = ("claude", "codex", "gemini", "human")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_name(name: str) -> Optional[str]:
    """Return an error message if *name* is not a safe agent identifier."""
    if not NAME_RE.fullmatch(name):
        return f"invalid agent name '{name}': must be 1-64 alphanumeric chars, dots, hyphens, or underscores"
    return None


def _template_path(relative: str):
    """Resolve a template file — repo tree first, installed package fallback."""
    repo_template = Path(__file__).resolve().parent.parent / "templates" / relative
    if repo_template.is_file():
        return nullcontext(repo_template)
    resource = resources.files("oacp").joinpath("_templates", relative)
    return resources.as_file(resource)


def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file using PyYAML."""
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"top-level YAML must be a mapping: {path}")
    return data


def _dump_yaml(data: Dict[str, Any]) -> str:
    """Dump a dict to YAML string using PyYAML."""
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _write_if_missing(path: Path, content: str) -> bool:
    """Write content to *path* only if it does not already exist."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def _is_empty(value: Any) -> bool:
    """Return True if the value should be treated as 'not set' in merge."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    return False


# Fields where the value is a list — project replaces global entirely
_LIST_FIELDS = {"skills"}

# Fields within dict sections where the value is a list — project replaces
_LIST_SUBFIELDS = {
    ("capabilities", "tools"),
    ("capabilities", "languages"),
    ("capabilities", "domains"),
    ("routing_rules", "primary"),
    ("routing_rules", "avoid"),
}


def merge_profiles(
    global_data: Dict[str, Any], project_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge global profile with project-level card.

    Merge rules (shallow dict merge, list replacement):
    - Scalars (name, runtime, model, trust_level, etc.): project wins if non-empty
    - Dict sections (capabilities, permissions, routing_rules, quota): key-level
      merge within the dict — project keys override, global-only keys preserved
    - Lists (skills, capabilities.tools): full replacement — project list replaces
      global entirely
    - Missing sections: global's value used verbatim
    """
    merged = copy.deepcopy(global_data)

    for key, value in project_data.items():
        if _is_empty(value):
            # Empty/null project value — keep global
            continue

        if key in _LIST_FIELDS:
            # Top-level list field — project replaces global entirely
            merged[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            # Dict section — key-level merge
            for sub_key, sub_value in value.items():
                if _is_empty(sub_value):
                    continue
                if (key, sub_key) in _LIST_SUBFIELDS:
                    # List sub-field — project replaces
                    merged[key][sub_key] = copy.deepcopy(sub_value)
                elif isinstance(sub_value, dict) and isinstance(
                    merged[key].get(sub_key), dict
                ):
                    merged[key][sub_key].update(copy.deepcopy(sub_value))
                else:
                    merged[key][sub_key] = copy.deepcopy(sub_value)
        else:
            # Scalar — project wins
            merged[key] = copy.deepcopy(value)

    return merged


# ---------------------------------------------------------------------------
# Profile I/O
# ---------------------------------------------------------------------------


def load_global_profile(oacp_root: Path, name: str) -> Optional[Dict[str, Any]]:
    """Load a global agent profile from $OACP_HOME/agents/<name>/profile.yaml."""
    path = oacp_root / "agents" / name / "profile.yaml"
    if not path.is_file():
        return None
    return _load_yaml(path)


def load_project_card(
    oacp_root: Path, project: str, name: str
) -> Optional[Dict[str, Any]]:
    """Load a project-level agent card."""
    path = oacp_root / "projects" / project / "agents" / name / "agent_card.yaml"
    if not path.is_file():
        return None
    return _load_yaml(path)


def resolve_agent_profile(
    oacp_root: Path, name: str, project: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return the merged agent profile.

    If *project* is given, merge global profile with project card.
    Otherwise return the global profile alone.
    """
    global_data = load_global_profile(oacp_root, name)
    if project is None:
        return global_data

    project_data = load_project_card(oacp_root, project, name)
    if global_data is None and project_data is None:
        return None
    if global_data is None:
        return project_data
    if project_data is None:
        return global_data

    return merge_profiles(global_data, project_data)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, oacp_root: Path) -> int:
    """Scaffold a global agent profile."""
    name = args.name
    runtime = args.runtime

    err = _validate_name(name)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    if runtime not in VALID_RUNTIMES:
        print(f"Error: invalid runtime '{runtime}': must be one of {VALID_RUNTIMES}", file=sys.stderr)
        return 1

    with _template_path("agent_profile.template.yaml") as tpl_path:
        template = tpl_path.read_text(encoding="utf-8")

    # Fill in identity fields
    template = template.replace('name: ""', f'name: "{name}"', 1)
    template = template.replace('runtime: ""', f'runtime: "{runtime}"', 1)

    profile_dir = oacp_root / "agents" / name
    profile_path = profile_dir / "profile.yaml"

    if _write_if_missing(profile_path, template):
        print(f"Created global profile: {profile_path}")
    else:
        print(f"Profile already exists (skipped): {profile_path}")

    return 0


def cmd_show(args: argparse.Namespace, oacp_root: Path) -> int:
    """Print a merged agent profile."""
    name = args.name
    project = getattr(args, "project", None)

    err = _validate_name(name)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    profile = resolve_agent_profile(oacp_root, name, project=project)
    if profile is None:
        label = f"agent '{name}'"
        if project:
            label += f" in project '{project}'"
        print(f"Error: no profile found for {label}", file=sys.stderr)
        return 1

    print(_dump_yaml(profile), end="")
    return 0


def cmd_list(args: argparse.Namespace, oacp_root: Path) -> int:
    """List known agents."""
    project = getattr(args, "project", None)

    # Global agents
    global_agents_dir = oacp_root / "agents"
    global_names: set = set()
    if global_agents_dir.is_dir():
        for d in sorted(global_agents_dir.iterdir()):
            if d.is_dir() and (d / "profile.yaml").is_file():
                global_names.add(d.name)

    # Project agents
    project_names: set = set()
    if project:
        project_agents_dir = oacp_root / "projects" / project / "agents"
        if project_agents_dir.is_dir():
            for d in sorted(project_agents_dir.iterdir()):
                if d.is_dir():
                    project_names.add(d.name)

    all_names = sorted(global_names | project_names)

    if not all_names:
        label = "agents"
        if project:
            label = f"agents for project '{project}'"
        print(f"No {label} found.")
        return 0

    for name in all_names:
        tags = []
        if name in global_names:
            tags.append("global")
        if name in project_names:
            tags.append("project")
        tag_str = ", ".join(tags)
        print(f"  {name}  ({tag_str})")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Two-tier agent profile management.",
    )
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )

    sub = parser.add_subparsers(dest="subcommand")

    # init
    p_init = sub.add_parser("init", help="Create a global agent profile")
    p_init.add_argument("name", help="Agent name")
    p_init.add_argument("--runtime", required=True, help="Agent runtime (claude, codex, gemini, human)")

    # show
    p_show = sub.add_parser("show", help="Show merged agent profile")
    p_show.add_argument("name", help="Agent name")
    p_show.add_argument("--project", default=None, help="Project name for merged view")

    # list
    p_list = sub.add_parser("list", help="List known agents")
    p_list.add_argument("--project", default=None, help="Project name")

    args = parser.parse_args(list(argv))
    if args.subcommand is None:
        parser.print_help()
        return args

    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.subcommand is None:
        return 0

    from _oacp_env import resolve_oacp_home

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    dispatch = {
        "init": cmd_init,
        "show": cmd_show,
        "list": cmd_list,
    }

    handler = dispatch.get(args.subcommand)
    if handler is None:
        print(f"Unknown subcommand: {args.subcommand}", file=sys.stderr)
        return 2

    return handler(args, oacp_root)


if __name__ == "__main__":
    raise SystemExit(main())
