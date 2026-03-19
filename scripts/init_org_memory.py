#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Initialize the org-level memory directory at $OACP_HOME/org-memory/.

Creates the directory structure with scaffold files:
  org-memory/
    recent.md
    decisions.md
    rules.md
    events/

Usage:
    init_org_memory.py [--oacp-dir <path>]
"""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path
from typing import Optional, Sequence

# Template files to copy into org-memory/
_TEMPLATE_FILES = (
    "recent.md",
    "decisions.md",
    "rules.md",
)


def _find_template_dir() -> Optional[Path]:
    """Locate the org-memory template directory."""
    # Development: templates/ in repo root
    repo_root = Path(__file__).resolve().parent.parent
    repo_templates = repo_root / "templates" / "org-memory"
    if repo_templates.is_dir():
        return repo_templates

    # Installed: bundled in package
    try:
        ref = resources.files("oacp").joinpath("_templates", "org-memory")
        # resources.files returns a Traversable; check if it's a real path
        if hasattr(ref, "_path"):
            p = Path(str(ref._path))
            if p.is_dir():
                return p
    except Exception:
        pass

    return None


def initialize_org_memory(oacp_root: Path) -> dict:
    """Create org-memory/ directory structure under oacp_root.

    Returns a report dict with created/skipped files.
    """
    org_memory_dir = oacp_root / "org-memory"
    events_dir = org_memory_dir / "events"

    # Create directories
    org_memory_dir.mkdir(parents=True, exist_ok=True)
    events_dir.mkdir(parents=True, exist_ok=True)

    template_dir = _find_template_dir()

    created = []
    skipped = []

    for filename in _TEMPLATE_FILES:
        target = org_memory_dir / filename
        if target.exists():
            skipped.append(filename)
            continue

        # Try to copy from template
        if template_dir and (template_dir / filename).is_file():
            content = (template_dir / filename).read_text(encoding="utf-8")
        else:
            # Minimal fallback
            title = filename.replace(".md", "").replace("_", " ").title()
            content = f"# {title}\n"

        target.write_text(content, encoding="utf-8")
        created.append(filename)

    return {
        "org_memory_dir": org_memory_dir,
        "created": created,
        "skipped": skipped,
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="oacp org-memory",
        description="Org-level memory commands.",
    )
    sub = parser.add_subparsers(dest="subcommand")
    init_parser = sub.add_parser("init", help="Initialize org-level memory directory")
    init_parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    # Default to "init" when no subcommand given
    argv_list = list(argv)
    if not argv_list or argv_list[0].startswith("-"):
        argv_list = ["init"] + argv_list
    return parser.parse_args(argv_list)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    from _oacp_env import resolve_oacp_home

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    result = initialize_org_memory(oacp_root)

    org_dir = result["org_memory_dir"]
    if result["created"]:
        print(f"Initialized org-level memory: {org_dir}")
        for f in result["created"]:
            print(f"  + {f}")
    else:
        print(f"Org-level memory already exists: {org_dir}")

    if result["skipped"]:
        for f in result["skipped"]:
            print(f"  (exists) {f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
