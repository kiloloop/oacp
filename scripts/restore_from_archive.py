#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Restore an archived memory file back into the active memory working set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from _oacp_env import resolve_oacp_home
from memory_archive_common import original_name_from_archive, project_memory_paths


def restore_memory_file(
    project_name: str,
    archived_file: str,
    *,
    oacp_root: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    restored_file = original_name_from_archive(archived_file)
    _, memory_dir, archive_dir = project_memory_paths(oacp_root, project_name)
    if not archive_dir.is_dir():
        raise ValueError(f"memory archive directory not found: {archive_dir}")
    source = archive_dir / archived_file
    if not source.is_file():
        raise ValueError(f"archived memory file not found: {source}")

    destination = memory_dir / restored_file
    if destination.exists():
        raise ValueError(f"active memory destination already exists: {destination}")

    if not dry_run:
        source.rename(destination)

    return {
        "project": project_name,
        "action": "restore",
        "archived_file": archived_file,
        "restored_file": restored_file,
        "source": str(source),
        "destination": str(destination),
        "dry_run": dry_run,
        "status": "dry-run" if dry_run else "restored",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore an archived memory file into the active working set."
    )
    parser.add_argument("project_name", help="Project workspace name")
    parser.add_argument("archived_file", help="Archived memory file basename to restore")
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
        result = restore_memory_file(
            args.project_name,
            args.archived_file,
            oacp_root=oacp_root,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    prefix = "Would restore" if args.dry_run else "Restored"
    print(
        f"{prefix} memory/archive/{result['archived_file']} -> "
        f"memory/{result['restored_file']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
