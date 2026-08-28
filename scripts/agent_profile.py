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
from contextlib import contextmanager
import fcntl
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from _oacp_constants import (
    AGENT_RE,
    ALL_RUNTIMES,
    _template_path,
    is_agent_dir,
)

VALID_RUNTIMES = tuple(runtime for runtime in ALL_RUNTIMES if runtime != "unknown")
NAME_RE = AGENT_RE


def _validate_name(name: str) -> Optional[str]:
    """Return an error message if *name* is not a safe agent identifier."""
    if not NAME_RE.fullmatch(name):
        return f"invalid agent name '{name}': must be 1-64 alphanumeric chars, dots, hyphens, or underscores"
    return None


def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file using PyYAML."""
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
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


@contextmanager
def _locked_profile(profile_path: Path) -> Iterator[None]:
    """Serialize updates to one global profile across concurrent writers."""
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(profile_path.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
    finally:
        os.close(directory_fd)


def _atomic_write_profile(path: Path, data: Dict[str, Any]) -> None:
    """Atomically replace one profile while preserving its existing mode."""
    content = _dump_yaml(data)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temp_path: Optional[Path] = None
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
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _new_profile(
    name: str,
    runtime: str,
    *,
    model: str,
    description: str,
    projects: Sequence[str],
) -> Dict[str, Any]:
    """Build a new profile from the shipped template and identity defaults."""
    with _template_path("agent_profile.template.yaml") as tpl_path:
        data = _load_yaml(tpl_path)
    data["name"] = name
    data["runtime"] = runtime
    data["model"] = model
    data["description"] = description
    data["projects"] = list(projects)
    return data


def upsert_global_profile(
    oacp_root: Path,
    name: str,
    runtime: str,
    *,
    projects: Sequence[str] = (),
    model: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or extend an instance-level agent registration.

    Existing identity values are never replaced. Missing identity keys are
    filled, and new project memberships are appended in caller order. The
    directory lock plus atomic replace prevents concurrent additions from losing
    memberships without adding registry artifacts.
    """
    err = _validate_name(name)
    if err:
        raise ValueError(err)
    if runtime not in ALL_RUNTIMES:
        raise ValueError(
            f"invalid runtime '{runtime}': must be one of {tuple(ALL_RUNTIMES)}"
        )
    for project in projects:
        if project.startswith(".") or "/" in project or "\\" in project:
            raise ValueError(f"invalid project name '{project}'")

    model_default = model if model is not None else (
        runtime if runtime != "unknown" else ""
    )
    description_default = description if description is not None else (
        f"{name} agent ({runtime} runtime)" if runtime != "unknown" else f"{name} agent"
    )
    profile_path = oacp_root / "agents" / name / "profile.yaml"

    with _locked_profile(profile_path):
        if profile_path.is_file():
            data = _load_yaml(profile_path)
            action = "unchanged"
            identity_defaults = {
                "name": name,
                "runtime": runtime,
                "model": model_default,
                "description": description_default,
            }
            for key, value in identity_defaults.items():
                if key not in data:
                    data[key] = value
                    action = "updated"

            memberships = data.get("projects")
            if memberships is None:
                memberships = []
                data["projects"] = memberships
                action = "updated"
            if not isinstance(memberships, list) or not all(
                isinstance(item, str) for item in memberships
            ):
                raise ValueError(
                    f"projects must be a list of strings in {profile_path}"
                )
            for project in projects:
                if project not in memberships:
                    memberships.append(project)
                    action = "updated"

            if action == "updated":
                _atomic_write_profile(profile_path, data)
        else:
            data = _new_profile(
                name,
                runtime,
                model=model_default,
                description=description_default,
                projects=projects,
            )
            _atomic_write_profile(profile_path, data)
            action = "created"

    return {"path": profile_path, "action": action, "profile": data}


def discover_project_memberships(oacp_root: Path) -> Dict[str, List[str]]:
    """Return visible project memberships keyed by agent name."""
    memberships: Dict[str, List[str]] = {}
    projects_dir = oacp_root / "projects"
    if not projects_dir.is_dir():
        return memberships
    for project_dir in sorted(projects_dir.iterdir()):
        if not is_agent_dir(project_dir):
            continue
        agents_dir = project_dir / "agents"
        if not agents_dir.is_dir():
            continue
        for agent_dir in sorted(agents_dir.iterdir()):
            if is_agent_dir(agent_dir):
                memberships.setdefault(agent_dir.name, []).append(project_dir.name)
    return memberships


def _identity_from_projects(
    oacp_root: Path,
    name: str,
    projects: Sequence[str],
) -> Dict[str, str]:
    """Infer registry defaults from project cards/status without overriding them."""
    cards: List[Dict[str, Any]] = []
    statuses: List[Dict[str, Any]] = []
    for project in projects:
        agent_dir = oacp_root / "projects" / project / "agents" / name
        for filename, target in (("agent_card.yaml", cards), ("status.yaml", statuses)):
            path = agent_dir / filename
            if not path.is_file():
                continue
            try:
                target.append(_load_yaml(path))
            except (OSError, ValueError):
                continue

    runtime = next(
        (
            str(card["runtime"])
            for card in cards
            if card.get("runtime") in ALL_RUNTIMES
        ),
        "",
    )
    if not runtime and name in ALL_RUNTIMES and name != "unknown":
        runtime = name
    if not runtime:
        runtime = next(
            (
                str(status["runtime"])
                for status in statuses
                if status.get("runtime") in ALL_RUNTIMES
            ),
            "unknown",
        )

    model = next(
        (
            str(source["model"])
            for source in [*cards, *statuses]
            if isinstance(source.get("model"), str) and source["model"].strip()
        ),
        runtime if runtime != "unknown" else "",
    )
    description = next(
        (
            str(card["description"])
            for card in cards
            if isinstance(card.get("description"), str)
            and card["description"].strip()
        ),
        f"{name} agent ({runtime} runtime)" if runtime != "unknown" else f"{name} agent",
    )
    return {"runtime": runtime, "model": model, "description": description}


def sync_agent_registry(oacp_root: Path) -> Dict[str, Any]:
    """Backfill all project agent directories into the instance registry."""
    memberships = discover_project_memberships(oacp_root)
    actions: Dict[str, str] = {}
    for name, projects in memberships.items():
        identity = _identity_from_projects(oacp_root, name, projects)
        result = upsert_global_profile(
            oacp_root,
            name,
            identity["runtime"],
            projects=projects,
            model=identity["model"],
            description=identity["description"],
        )
        actions[name] = str(result["action"])
    return {
        "agents": len(memberships),
        "memberships": sum(len(projects) for projects in memberships.values()),
        "created": sum(action == "created" for action in actions.values()),
        "updated": sum(action == "updated" for action in actions.values()),
        "unchanged": sum(action == "unchanged" for action in actions.values()),
        "actions": actions,
    }
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

    try:
        result = upsert_global_profile(oacp_root, name, runtime)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    profile_path = result["path"]
    if result["action"] == "created":
        print(f"Created global profile: {profile_path}")
    elif result["action"] == "updated":
        print(f"Updated global profile (identity preserved): {profile_path}")
    else:
        print(f"Profile already exists (unchanged): {profile_path}")

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
            if is_agent_dir(d) and (d / "profile.yaml").is_file():
                global_names.add(d.name)

    # Project agents
    project_names: set = set()
    if project:
        project_agents_dir = oacp_root / "projects" / project / "agents"
        if project_agents_dir.is_dir():
            for d in sorted(project_agents_dir.iterdir()):
                if is_agent_dir(d):
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
        try:
            profile = load_global_profile(oacp_root, name) or {}
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        identity = _identity_from_projects(
            oacp_root,
            name,
            [project] if project and name in project_names else [],
        )
        runtime = profile.get("runtime") or identity["runtime"]
        projects = profile.get("projects", [])
        if projects is None:
            projects = []
        if not isinstance(projects, list) or not all(
            isinstance(membership, str) for membership in projects
        ):
            print(
                f"Error: projects must be a list of strings in "
                f"{oacp_root / 'agents' / name / 'profile.yaml'}",
                file=sys.stderr,
            )
            return 1
        project_text = ",".join(projects) if projects else "-"
        print(
            f"  {name}  runtime={runtime}  memberships={project_text}  ({tag_str})"
        )

    return 0


def cmd_sync(args: argparse.Namespace, oacp_root: Path) -> int:
    """Backfill the instance registry from project agent directories."""
    del args
    try:
        result = sync_agent_registry(oacp_root)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        "Agent registry synchronized: "
        f"{result['agents']} agent(s), {result['memberships']} project membership(s); "
        f"created {result['created']}, updated {result['updated']}, "
        f"unchanged {result['unchanged']}."
    )
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
    p_init.add_argument("--runtime", required=True, help="Agent runtime (claude, codex, cursor, gemini, human)")

    # sync
    sub.add_parser("sync", help="Backfill the instance registry from project agents")

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
        "sync": cmd_sync,
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
