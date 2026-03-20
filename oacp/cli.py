# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Installable CLI entrypoint for the OACP kernel."""

from __future__ import annotations

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
  init           Create a project workspace under $OACP_HOME/projects/
  add-agent      Add an agent to an existing project workspace
  agent          Manage global agent profiles (init, show, list)
  memory         Archive or restore project memory files
  setup          Generate runtime-specific config files in a repo
  send           Send a protocol-compliant inbox message
  org-memory     Initialize org-level memory at $OACP_HOME/org-memory/
  write-event    Write an event to org-memory/events/
  doctor         Check environment and workspace health
  validate       Validate an inbox/outbox YAML message

Examples:
  oacp init my-project --repo /path/to/repo
  oacp init my-project --agents claude,codex
  oacp add-agent my-project alice --runtime claude
  oacp memory archive my-project research_notes.md
  oacp setup claude --project my-project
  oacp send my-project --from codex --to iris --type notification --subject "Done" --body "Completed"
  oacp org-memory init
  oacp write-event --agent claude --project my-project --type decision --slug api-convention --body "Use REST for public APIs"
  oacp doctor
  oacp validate /path/to/message.yaml
"""

SCRIPT_NAMES = {
    "init": "init_project_workspace.py",
    "add-agent": "add_agent.py",
    "agent": "agent_profile.py",
    "memory": "memory_cli.py",
    "setup": "setup_runtime.py",
    "send": "send_inbox_message.py",
    "org-memory": "init_org_memory.py",
    "write-event": "write_event.py",
    "doctor": "oacp_doctor.py",
    "validate": "validate_message.py",
}


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
        return _dispatch(args[1], ["--help"])

    command, rest = args[0], args[1:]
    return _dispatch(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
