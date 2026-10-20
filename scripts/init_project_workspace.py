#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Initialize an OACP project workspace."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from _oacp_constants import (
    AGENT_RE,
    AGENT_SUBDIRS as AGENT_SUBDIRS,
    WORKSPACE_LAYOUT,
    validate_project_name as _validate_project_name,
    workspace_layout,
    CREATABLE_RUNTIMES,
    SPEC_VERSION,
    _template_path,
    _write_if_missing,
)
from agent_profile import upsert_global_profile

DEFAULT_AGENTS = ("claude", "codex", "cursor")


class ProjectExistsError(FileExistsError):
    """The project root existed before this initializer reserved it."""


def _validate_agent_name(name: str) -> None:
    if not AGENT_RE.fullmatch(name):
        raise ValueError(
            f"Invalid agent name '{name}': must start with alphanumeric, "
            f"contain only [A-Za-z0-9._-], and be 1-64 chars"
        )


DEFAULT_PROJECT_FACTS = """# Project Facts

## Agent Roles
- Implementer (<domain>): <agent name>
- QA/Reviewer: <agent name>
- Deploy/Ops: <agent name>

## Protocol
- Shared handoff protocol version: v0.2.0
"""

KNOWN_DEBT_TEMPLATE = """# Known Debt

Use this file to track verified, unresolved project debt that future sessions
should not rediscover from scratch.

| Item | Severity | Date Found | Source | Status |
| --- | --- | --- | --- | --- |
| _None yet._ |  |  |  |  |
"""

# Compatibility views of the shared layout for direct script consumers.
STATIC_DIRECTORIES = tuple(path for scope, path, _ in WORKSPACE_LAYOUT if scope == "project")
STATIC_GITKEEP_PATHS = tuple(
    f"{path}/.gitkeep" for path, keep in workspace_layout(scope="project") if keep
)


def _agent_dirs(agents: Sequence[str]) -> List[str]:
    return [path for path, _ in workspace_layout(agents, scope="agent")]


def _agent_gitkeeps(agents: Sequence[str]) -> List[str]:
    return [f"{path}/.gitkeep" for path, keep in workspace_layout(agents, scope="agent") if keep]


def _parse_link(value: str) -> Tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("link must be SRC:DST")
    src, dst = value.split(":", 1)
    if not src or not dst:
        raise argparse.ArgumentTypeError("link must be SRC:DST")
    return src, dst


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a new project workspace under $OACP_HOME/projects/.",
    )
    parser.add_argument("project_name", help="Project workspace name")
    parser.add_argument(
        "--repo",
        default=None,
        help="Repo root used for artifact symlinks and recorded in workspace.json",
    )
    parser.add_argument(
        "--link",
        action="append",
        type=_parse_link,
        default=[],
        metavar="SRC:DST",
        help="Create artifacts/DST -> REPO/SRC symlink (repeatable; requires --repo)",
    )
    parser.add_argument(
        "--agents",
        default=",".join(DEFAULT_AGENTS),
        help=(
            "Comma-separated list of agents to create "
            f"(default: {','.join(DEFAULT_AGENTS)}). "
            'Pass "" for none.'
        ),
    )
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument("--fail-if-exists", action="store_true",
                        help="Refuse an existing project root (also the default)")
    parser.add_argument("--dry-run", action="store_true", help="Plan without any filesystem writes")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Emit the plan or apply result as JSON")
    return parser.parse_args(list(argv))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _project_facts_template() -> str:
    # Installed wheels ship the default template but not repo examples.
    candidates = (
        Path(__file__).resolve().parent / "examples" / "project_facts.example.md",
        _repo_root() / "examples" / "project_facts.example.md",
    )
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return DEFAULT_PROJECT_FACTS


def _receiver_config_template() -> str:
    with _template_path("receiver_config.template.yaml") as path:
        return path.read_text(encoding="utf-8")


def plan_workspace(
    project_name: str,
    *,
    oacp_root: Path,
    repo_dir: Optional[Path] = None,
    artifact_links: Sequence[Tuple[str, str]] = (),
    agents: Sequence[str] = DEFAULT_AGENTS,
) -> Dict[str, object]:
    """Describe creation and shared-profile effects without creating even locks."""
    _validate_project_name(project_name)
    if not project_name:
        raise ValueError("project name must not be empty")
    for agent in agents:
        _validate_agent_name(agent)
    if artifact_links and repo_dir is None:
        raise ValueError("--link requires --repo")
    destinations = set()
    for _, dst in artifact_links:
        relative = Path(dst)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("artifact destination must be a relative path inside artifacts/")
        normalized = str(relative)
        if normalized in destinations:
            raise ValueError("duplicate artifact destination")
        if relative.parts[0] == ".gitkeep":
            raise ValueError("artifact destination collides with workspace layout")
        if any(relative in Path(other).parents or Path(other) in relative.parents
               for other in destinations):
            raise ValueError("artifact destinations must not contain each other")
        destinations.add(normalized)
    root = oacp_root / "projects" / project_name
    exists = root.exists() or root.is_symlink()
    actions = []
    if not exists:
        directories = {root}
        files = {root / "workspace.json"}
        for relative, keep in workspace_layout(agents):
            directory = root / relative
            directories.add(directory)
            if keep:
                files.add(directory / ".gitkeep")
        files.update(root / "memory" / name for name in (
            "project_facts.md", "decision_log.md", "open_threads.md", "known_debt.md"))
        files.update(root / "agents" / agent / "config.yaml" for agent in agents)
        for path in sorted(directories | files):
            actions.append({"path": str(path), "operation": "create_directory" if path in directories else "create_file"})
        for agent in dict.fromkeys(agents):
            actions.append({"path": str(oacp_root / "agents" / agent / "profile.yaml"),
                            "operation": "ensure_project_membership"})
        for src, dst in artifact_links:
            source = repo_dir / src
            actions.append({"path": str(root / "artifacts" / dst),
                            "operation": "create_symlink" if source.exists() else "skip_missing_source",
                            "target": str(source)})
    return {"version": 1, "project_root": str(root), "can_apply": not exists,
            "reason": "project_exists" if exists else None, "actions": actions}


def initialize_workspace(
    project_name: str,
    *,
    oacp_root: Path,
    repo_dir: Optional[Path] = None,
    artifact_links: Sequence[Tuple[str, str]] = (),
    agents: Sequence[str] = DEFAULT_AGENTS,
) -> Dict[str, object]:
    plan = plan_workspace(project_name, oacp_root=oacp_root, repo_dir=repo_dir,
                          artifact_links=artifact_links, agents=agents)
    project_root = oacp_root / "projects" / project_name
    if not plan["can_apply"]:
        raise ProjectExistsError(f"project root already exists: {project_root}")
    project_root.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation is the apply-time no-clobber gate. A dry-run is a
    # snapshot, not a lock: another initializer may win before this mkdir.
    try:
        project_root.mkdir()
    except FileExistsError as exc:
        raise ProjectExistsError(f"project root already exists: {project_root}") from exc

    for relative_dir, keep in workspace_layout(agents):
        directory = project_root / relative_dir
        directory.mkdir(parents=True, exist_ok=True)
        if keep:
            (directory / ".gitkeep").touch()

    _write_if_missing(project_root / "memory" / "project_facts.md", _project_facts_template())
    _write_if_missing(
        project_root / "memory" / "decision_log.md",
        f"# Decision Log\n\n## {dt.date.today().isoformat()}\n- Project workspace initialized.\n",
    )
    _write_if_missing(
        project_root / "memory" / "open_threads.md",
        "# Open Threads\n\n- None yet.\n",
    )
    _write_if_missing(project_root / "memory" / "known_debt.md", KNOWN_DEBT_TEMPLATE)
    receiver_config = _receiver_config_template()
    for agent in agents:
        _write_if_missing(project_root / "agents" / agent / "config.yaml", receiver_config)
        runtime = agent if agent in CREATABLE_RUNTIMES else "unknown"
        upsert_global_profile(
            oacp_root,
            agent,
            runtime,
            projects=[project_name],
        )

    workspace_path = project_root / "workspace.json"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    # spec_version names the protocol contract the workspace was initialized
    # against. Workspaces created before this field carry an orphaned
    # standards_version (a retired VERSION-file stamp); readers should
    # prefer spec_version and tolerate either field's absence.
    workspace = {
        "project_name": project_name,
        "repo_path": str(repo_dir) if repo_dir else None,
        "created_at": now,
        "updated_at": now,
        "spec_version": SPEC_VERSION,
    }
    with workspace_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(workspace, indent=2) + "\n")

    warnings: List[str] = []
    created_links: List[Tuple[str, str]] = []
    if artifact_links:
        if repo_dir is None:
            raise ValueError("--link requires --repo")
        for src, dst in artifact_links:
            source = repo_dir / src
            destination = project_root / "artifacts" / dst
            if not source.exists():
                warnings.append(f"Skipped artifacts/{dst} — {source} not found")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source)
            created_links.append((dst, str(source)))

    return {
        "project_root": project_root,
        "workspace_path": workspace_path,
        "repo_dir": repo_dir,
        "created_links": created_links,
        "warnings": warnings,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    from _oacp_env import resolve_oacp_home
    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)
    repo_dir = Path(args.repo).expanduser().resolve() if args.repo else None

    agents_list: Sequence[str] = (
        [a.strip() for a in args.agents.split(",") if a.strip()]
        if args.agents
        else ()
    )

    try:
        if args.dry_run:
            plan = plan_workspace(args.project_name, oacp_root=oacp_root,
                                  repo_dir=repo_dir, artifact_links=args.link,
                                  agents=agents_list)
            if args.json_output:
                print(json.dumps(plan, indent=2))
            else:
                print(f"Plan: {plan['project_root']}")
                if plan["reason"]:
                    print(f"Refused: {plan['reason']}")
                for action in plan["actions"]:
                    print(f"  {action['operation']}: {action['path']}")
            return 0 if plan["can_apply"] else 3
        result = initialize_workspace(
            args.project_name,
            oacp_root=oacp_root,
            repo_dir=repo_dir,
            artifact_links=args.link,
            agents=agents_list,
        )
    except (ValueError, OSError) as exc:
        code = 3 if isinstance(exc, ProjectExistsError) else 1
        if args.json_output:
            print(json.dumps({"version": 1, "error": "already_exists" if code == 3 else "initialization_failed",
                              "message": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return code

    if args.json_output:
        print(json.dumps(dict(result, version=1), default=str, indent=2))
        return 0

    if result["created_links"]:
        print(f"Creating artifact symlinks from repo: {repo_dir}")
        for dst, source in result["created_links"]:
            print(f"  + artifacts/{dst} -> {source}")
    for warning in result["warnings"]:
        print(f"  ! {warning}")

    project_root = result["project_root"]
    print(f"Initialized project workspace: {project_root}")
    if repo_dir is not None:
        print("  To link a runtime repo, symlink workspace.json into the repo root:")
        print(f"    ln -sf {project_root / 'workspace.json'} <repo>/.oacp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
