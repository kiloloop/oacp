# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Installable CLI entrypoint for the OACP kernel."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from importlib import resources
from pathlib import Path
import runpy
import sys
from typing import Optional, Sequence

from oacp import __version__


HELP_TEXT = """Usage: oacp <command> [args]

Installable Open Agent Coordination Protocol (OACP) CLI.

Commands:
  init           Create a new project workspace; preview with --dry-run
  agent          Add project agents and manage global profiles
  inbox          List pending inbox messages
  watch          Emit inbox delta events for Monitor-friendly polling
  session-init   Verify Codex startup inputs and emit SessionStart context
  setup          Generate runtime-specific config files in a repo
  send           Send a protocol-compliant inbox message
  trust          Manage local keys and project trust (catalog + pins)
  autonomy       Record human outcomes, checkpoints, and terminal audit states
  envelope       Compile, show, or clear the runtime envelope for a task
  doctor         Check environment and workspace health
  validate       Validate an inbox/outbox YAML message
  verify         Verify a message's auth trailer against receiver-local pins

Examples:
  oacp init my-project --repo /path/to/repo
  oacp init my-project --agents claude,codex
  oacp agent add my-project alice --runtime claude
  oacp inbox my-project --agent claude
  oacp watch --agent claude --project my-project --json
  oacp session-init --project my-project
  oacp setup claude --project my-project
  oacp send my-project --to alice --type notification --subject "Done" --body "Completed"
  oacp trust keygen --agent claude
  oacp trust import /path/to/<kid>.pub.json --project my-project --agent claude
  oacp trust revoke <kid> --project my-project --agent claude
  oacp trust pin-script ~/tools/finish.sh --project my-project --agent claude
  oacp autonomy outcome /path/to/audit.yaml --decision approved
  oacp autonomy finalize /path/to/audit.yaml --final-state done --started-at 2026-08-30T10:05:00Z --actual-files-touched 3
  oacp envelope compile /path/to/message.yaml --receiver claude
  oacp envelope show --project my-project
  oacp doctor
  oacp validate /path/to/message.yaml
  oacp verify /path/to/message.yaml --project my-project --receiver claude
"""

SCRIPT_NAMES = {
    "init": "init_project_workspace.py",
    "inbox": "oacp_inbox.py",
    "watch": "oacp_watch.py",
    "session-init": "codex_session_init.py",
    "setup": "setup_runtime.py",
    "send": "send_inbox_message.py",
    "envelope": "envelope_compiler.py",
    "doctor": "oacp_doctor.py",
    "validate": "validate_message.py",
    "verify": "message_verify.py",
}


# The scripts remain the implementation boundary; only the public command
# paths change. Each route carries the script's original subcommand prefix.
COMMAND_GROUPS = {
    "agent": {
        "add": ("add_agent.py", (), "Add an agent to a project workspace"),
        "init": ("agent_profile.py", ("init",), "Create a global agent profile"),
        "sync": ("agent_profile.py", ("sync",), "Backfill the global agent registry"),
        "show": ("agent_profile.py", ("show",), "Show a merged agent profile"),
        "list": ("agent_profile.py", ("list",), "List known agents"),
    },
    "trust": {
        "keygen": ("key_cli.py", ("gen",), "Generate a local signing key"),
        "keys": ("key_cli.py", ("list",), "List local signing keys"),
        "import": ("trust_cli.py", ("import",), "Import a public key into catalog and pins"),
        "list": ("trust_cli.py", ("list",), "List project catalog entries and receiver pins"),
        "revoke": ("trust_cli.py", ("revoke",), "Revoke a receiver pin"),
        "sign-policy": ("trust_cli.py", ("sign-policy",), "Sign receiver policy files"),
        "pin-script": (
            "trust_cli.py",
            ("pin-script",),
            "Pin a script under a receiver's envelope.trusted_scripts and re-sign",
        ),
    },
    "autonomy": {
        "outcome": ("record_autonomy_outcome.py", (), "Record a human approval or decline"),
        "finalize": ("finalize_autonomy_record.py", (), "Record checkpoints and terminal states"),
    },
}


def _dispatch_group(command: str, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"oacp {command}", allow_abbrev=False)
    if command in {"agent", "trust"}:
        parser.add_argument("--oacp-dir", help="Override OACP home directory")
    subparsers = parser.add_subparsers(
        dest="subcommand", required=command != "agent"
    )
    for name, (_, _, description) in COMMAND_GROUPS[command].items():
        # Leaf help and validation belong to the existing implementation.
        leaf = subparsers.add_parser(
            name, help=description, add_help=False, allow_abbrev=False
        )
        if command in {"agent", "trust"}:
            leaf.add_argument("--oacp-dir", default=argparse.SUPPRESS)
    try:
        args, rest = parser.parse_known_args(list(argv))
    except SystemExit as exc:
        return int(exc.code)
    if args.subcommand is None:
        parser.print_help()
        return 0
    script, prefix, _ = COMMAND_GROUPS[command][args.subcommand]
    home = ["--oacp-dir", args.oacp_dir] if getattr(args, "oacp_dir", None) is not None else []
    # Global profile commands take the home before their verb; key and trust
    # scripts take it after. argparse retains the last occurrence across both
    # namespace levels, including the --oacp-dir=value spelling.
    if script == "agent_profile.py":
        return _run_script(script, [*home, *prefix, *rest])
    return _run_script(script, [*prefix, *home, *rest])


def _script_path(script_name: str):
    repo_script = Path(__file__).resolve().parents[1] / "scripts" / script_name
    if repo_script.is_file():
        return nullcontext(repo_script)
    resource = resources.files("oacp").joinpath("_scripts", script_name)
    return resources.as_file(resource)


def _run_script(script_name: str, argv: Sequence[str]) -> int:
    with _script_path(script_name) as script_path:
        script_dir = str(Path(script_path).resolve().parent)
        old_argv = sys.argv[:]
        old_sys_path = sys.path[:]
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        try:
            sys.argv = [Path(script_path).name, *argv]
            try:
                runpy.run_path(str(script_path), run_name="__main__")
            except SystemExit as exc:
                code = exc.code
                if code is None:
                    return 0
                if isinstance(code, int):
                    return code
                return 1
            return 0
        finally:
            sys.argv = old_argv
            sys.path[:] = old_sys_path


def _dispatch(command: str, argv: Sequence[str]) -> int:
    if command in COMMAND_GROUPS:
        return _dispatch_group(command, argv)
    script_name = SCRIPT_NAMES.get(command)
    if script_name is None:
        print(f"ERROR: unknown command '{command}'", file=sys.stderr)
        print("Run `oacp --help` for usage.", file=sys.stderr)
        return 2
    return _run_script(script_name, argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in {"-h", "--help"}:
        print(HELP_TEXT.rstrip())
        return 0

    if args[0] in {"-V", "--version", "version"}:
        print(__version__)
        return 0

    if args[0] == "help":
        if len(args) == 1:
            print(HELP_TEXT.rstrip())
            return 0
        return _dispatch(args[1], [*args[2:], "--help"])

    command, rest = args[0], args[1:]
    return _dispatch(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
