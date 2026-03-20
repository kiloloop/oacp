#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Move a non-standard memory file into memory/archive/."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from _oacp_env import resolve_oacp_home
from memory_archive_common import (
    ACTIVE_MEMORY_FILES,
    build_archive_name,
    project_memory_paths,
    validate_memory_basename,
)


def archive_memory_file(
    project_name: str,
    memory_file: str,
    *,
    oacp_root: Path,
    dry_run: bool = False,
    now: dt.datetime | None = None,
) -> Dict[str, Any]:
    validate_memory_basename(memory_file)
    if memory_file in ACTIVE_MEMORY_FILES:
        raise ValueError(f"cannot archive standard active memory file: {memory_file}")

    _, memory_dir, archive_dir = project_memory_paths(oacp_root, project_name)
    source = memory_dir / memory_file
    if not source.is_file():
        raise ValueError(f"memory file not found: {source}")

    archived_file = build_archive_name(memory_file, now=now)
    destination = archive_dir / archived_file
    if destination.exists():
        raise ValueError(f"archive destination already exists: {destination}")

    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
        source.rename(destination)

    return {
        "project": project_name,
        "action": "archive",
        "memory_file": memory_file,
        "archived_file": archived_file,
        "source": str(source),
        "destination": str(destination),
        "dry_run": dry_run,
        "status": "dry-run" if dry_run else "archived",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Move a non-standard memory file into memory/archive/."
    )
    parser.add_argument("project_name", help="Project workspace name")
    parser.add_argument("memory_file", help="Active memory file basename to archive")
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report actions without renaming")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON output")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    try:
        result = archive_memory_file(
            args.project_name,
            args.memory_file,
            oacp_root=oacp_root,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    prefix = "Would archive" if args.dry_run else "Archived"
    print(
        f"{prefix} memory/{result['memory_file']} -> "
        f"memory/archive/{result['archived_file']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
