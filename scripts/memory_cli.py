#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Namespace CLI for project memory archive/restore operations."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oacp memory",
        description="Archive or restore project memory files.",
    )
    sub = parser.add_subparsers(dest="command")

    archive_parser = sub.add_parser(
        "archive", help="Move a non-standard memory file into memory/archive/"
    )
    archive_parser.add_argument("project_name", help="Project workspace name")
    archive_parser.add_argument("memory_file", help="Active memory file basename to archive")
    archive_parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    archive_parser.add_argument(
        "--dry-run", action="store_true", help="Report actions without renaming"
    )
    archive_parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Emit JSON output"
    )

    restore_parser = sub.add_parser(
        "restore", help="Restore an archived memory file into the active working set"
    )
    restore_parser.add_argument("project_name", help="Project workspace name")
    restore_parser.add_argument("archived_file", help="Archived memory file basename to restore")
    restore_parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    restore_parser.add_argument(
        "--dry-run", action="store_true", help="Report actions without renaming"
    )
    restore_parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Emit JSON output"
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    from _oacp_env import resolve_oacp_home
    from promote_to_archive import archive_memory_file
    from restore_from_archive import restore_memory_file

    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.command is None:
        parser.print_help()
        return 2

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    try:
        if args.command == "archive":
            result = archive_memory_file(
                args.project_name,
                args.memory_file,
                oacp_root=oacp_root,
                dry_run=args.dry_run,
            )
            human = (
                f"{'Would archive' if args.dry_run else 'Archived'} "
                f"memory/{result['memory_file']} -> "
                f"memory/archive/{result['archived_file']}"
            )
        else:
            result = restore_memory_file(
                args.project_name,
                args.archived_file,
                oacp_root=oacp_root,
                dry_run=args.dry_run,
            )
            human = (
                f"{'Would restore' if args.dry_run else 'Restored'} "
                f"memory/archive/{result['archived_file']} -> "
                f"memory/{result['restored_file']}"
            )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(human)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
