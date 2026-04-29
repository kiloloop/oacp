#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Namespace CLI for project memory archive/restore and sync operations."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oacp memory",
        description="Archive, restore, or sync OACP memory files.",
    )
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser("init", help="Initialize git sync at $OACP_HOME")
    init_parser.add_argument(
        "--remote",
        default=None,
        help="Optional git remote URL for cross-machine sync",
    )
    init_parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )

    clone_parser = sub.add_parser("clone", help="Clone a memory repo into $OACP_HOME")
    clone_parser.add_argument("url", help="Git remote URL to clone")
    clone_parser.add_argument(
        "--force",
        action="store_true",
        help="Move a non-empty OACP_HOME aside before cloning",
    )
    clone_parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )

    pull_parser = sub.add_parser("pull", help="Fetch and fast-forward memory sync")
    pull_parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )

    push_parser = sub.add_parser("push", help="Commit allowlisted memory and push")
    push_parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )

    disable_parser = sub.add_parser("disable", help="Disable memory sync hooks locally")
    disable_parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )

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
    from memory_sync import (
        MemorySyncError,
        clone_memory_repo,
        disable_memory_repo,
        init_memory_repo,
        pull_memory,
        push_memory,
    )
    from promote_to_archive import archive_memory_file
    from restore_from_archive import restore_memory_file

    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.command is None:
        parser.print_help()
        return 2

    json_output = bool(getattr(args, "json_output", False))
    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    try:
        if args.command == "init":
            result = {"action": "init"}
            lines = init_memory_repo(oacp_root, remote=args.remote)
            human = "\n".join(lines)
        elif args.command == "clone":
            result = {"action": "clone", "url": args.url, "oacp_root": str(oacp_root)}
            lines = clone_memory_repo(oacp_root, args.url, force=args.force)
            human = "\n".join(lines)
        elif args.command == "pull":
            result = {"action": "pull"}
            lines = pull_memory(oacp_root)
            human = "\n".join(lines)
        elif args.command == "push":
            result = {"action": "push"}
            lines, code = push_memory(oacp_root)
            human = "\n".join(lines)
            if json_output:
                result["exit_code"] = code
            if not json_output and human:
                print(human)
            elif json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            return code
        elif args.command == "disable":
            result = {"action": "disable"}
            human = "\n".join(disable_memory_repo(oacp_root))
        elif args.command == "archive":
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
        elif args.command == "restore":
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
        else:
            parser.print_help()
            return 2
    except (MemorySyncError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif human:
        print(human)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
