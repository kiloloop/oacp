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


DEFAULT_PROJECT_FACTS = """# Project Facts

## Agent Roles
- Implementer (<domain>): <agent name>
- QA/Reviewer: <agent name>
- Deploy/Ops: <agent name>

## Protocol
- Shared handoff protocol version: v0.2.0
"""

DIRECTORIES = (
    "agents/codex/inbox",
    "agents/codex/outbox",
    "agents/codex/dead_letter",
    "agents/claude/inbox",
    "agents/claude/outbox",
    "agents/claude/dead_letter",
    "agents/gemini/inbox",
    "agents/gemini/outbox",
    "agents/gemini/dead_letter",
    "packets/review",
    "packets/findings",
    "packets/test",
    "packets/deploy",
    "checkpoints",
    "merges",
    "memory",
    "artifacts",
    "state",
    "logs",
)

GITKEEP_PATHS = (
    "agents/codex/inbox/.gitkeep",
    "agents/codex/outbox/.gitkeep",
    "agents/codex/dead_letter/.gitkeep",
    "agents/claude/inbox/.gitkeep",
    "agents/claude/outbox/.gitkeep",
    "agents/claude/dead_letter/.gitkeep",
    "agents/gemini/inbox/.gitkeep",
    "agents/gemini/outbox/.gitkeep",
    "agents/gemini/dead_letter/.gitkeep",
    "packets/review/.gitkeep",
    "packets/findings/.gitkeep",
    "packets/test/.gitkeep",
    "packets/deploy/.gitkeep",
    "checkpoints/.gitkeep",
    "merges/.gitkeep",
    "artifacts/.gitkeep",
)


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
    return parser.parse_args(list(argv))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _standards_version() -> str:
    candidates = (
        Path(__file__).resolve().parent / "VERSION",
        _repo_root() / "VERSION",
    )
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (FileNotFoundError, IndexError):
            continue
    return "0.1.0"


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


def _validate_project_name(name: str) -> None:
    if name.startswith(".") or "/" in name or "\\" in name:
        raise ValueError("project name must not contain path separators or start with '.'")


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def initialize_workspace(
    project_name: str,
    *,
    oacp_root: Path,
    repo_dir: Optional[Path] = None,
    artifact_links: Sequence[Tuple[str, str]] = (),
) -> Dict[str, object]:
    _validate_project_name(project_name)
    project_root = oacp_root / "projects" / project_name

    for relative_dir in DIRECTORIES:
        (project_root / relative_dir).mkdir(parents=True, exist_ok=True)

    for relative_path in GITKEEP_PATHS:
        (project_root / relative_path).touch()

    _write_if_missing(project_root / "memory" / "project_facts.md", _project_facts_template())
    _write_if_missing(
        project_root / "memory" / "decision_log.md",
        f"# Decision Log\n\n## {dt.date.today().isoformat()}\n- Project workspace initialized.\n",
    )
    _write_if_missing(
        project_root / "memory" / "open_threads.md",
        "# Open Threads\n\n- None yet.\n",
    )

    workspace_path = project_root / "workspace.json"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    workspace = {
        "project_name": project_name,
        "repo_path": str(repo_dir) if repo_dir else None,
        "created_at": now,
        "updated_at": now,
        "standards_version": _standards_version(),
    }
    workspace_path.write_text(json.dumps(workspace, indent=2) + "\n", encoding="utf-8")

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
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif destination.exists():
                raise ValueError(f"artifact destination exists and is not a symlink: {destination}")
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
    oacp_root = resolve_oacp_home()
    repo_dir = Path(args.repo).expanduser().resolve() if args.repo else None

    try:
        result = initialize_workspace(
            args.project_name,
            oacp_root=oacp_root,
            repo_dir=repo_dir,
            artifact_links=args.link,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

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
