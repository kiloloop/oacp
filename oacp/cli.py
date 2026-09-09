# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Installable CLI entrypoint for the OACP kernel."""

from __future__ import annotations

from contextlib import nullcontext
from importlib import resources
import os
from pathlib import Path
import runpy
import shutil
import sys
from typing import Dict, List, Optional, Sequence

from oacp import __version__


HELP_TEXT = """Usage: oacp <command> [args]

Installable Open Agent Coordination Protocol (OACP) CLI.

Commands:
  init           Create a project workspace under $OACP_HOME/projects/
  add-agent      Add an agent to an existing project workspace
  agent          Manage global agent profiles (init, sync, show, list)
  inbox          List pending inbox messages
  watch          Emit inbox delta events for Monitor-friendly polling
  retention      Prune project message history by age and count
  memory         Run agent-memory (sync, archive, restore); shim until 0.5.2
  session-init   Verify Codex startup inputs and emit SessionStart context
  setup          Generate runtime-specific config files in a repo
  send           Send a protocol-compliant inbox message
  key            Generate and inspect message-signing keys
  trust          Import, inspect, and revoke trust-root entries (catalog + pins)
  org-memory     Run agent-memory org (init); shim until 0.5.2
  write-event    Write an event to org-memory/events/
  autonomy-outcome  Record a human approval/decline in an autonomy audit
  autonomy-finalize  Record checkpoints and terminal states in an autonomy audit
  envelope       Compile, show, or clear the runtime envelope for a task
  doctor         Check environment and workspace health
  validate       Validate an inbox/outbox YAML message
  verify         Verify a message's auth trailer against receiver-local pins

Examples:
  oacp init my-project --repo /path/to/repo
  oacp init my-project --agents claude,codex
  oacp add-agent my-project alice --runtime claude
  oacp inbox my-project --agent claude
  oacp watch --agent claude --project my-project --json
  oacp retention my-project --dry-run
  oacp memory archive my-project research_notes.md   # runs: agent-memory archive ...
  oacp session-init --project my-project
  oacp setup claude --project my-project
  oacp send my-project --to iris --type notification --subject "Done" --body "Completed"
  oacp key gen --agent claude
  oacp trust import /path/to/<kid>.pub.json --project my-project --agent claude
  oacp trust revoke <kid> --project my-project --agent claude
  oacp org-memory init                               # runs: agent-memory org init
  oacp write-event --agent claude --project my-project --type decision --slug api-convention --body "Use REST for public APIs"
  oacp autonomy-outcome /path/to/audit.yaml --decision approved
  oacp autonomy-finalize /path/to/audit.yaml --final-state done --started-at 2026-08-30T10:05:00Z --actual-files-touched 3
  oacp envelope compile /path/to/message.yaml --receiver claude
  oacp envelope show --project my-project
  oacp doctor
  oacp validate /path/to/message.yaml
  oacp verify /path/to/message.yaml --project my-project --receiver claude
"""

SCRIPT_NAMES = {
    "init": "init_project_workspace.py",
    "add-agent": "add_agent.py",
    "agent": "agent_profile.py",
    "inbox": "oacp_inbox.py",
    "watch": "oacp_watch.py",
    "retention": "retention.py",
    "session-init": "codex_session_init.py",
    "setup": "setup_runtime.py",
    "send": "send_inbox_message.py",
    "key": "key_cli.py",
    "trust": "trust_cli.py",
    "write-event": "write_event.py",
    "autonomy-outcome": "record_autonomy_outcome.py",
    "autonomy-finalize": "finalize_autonomy_record.py",
    "envelope": "envelope_compiler.py",
    "doctor": "oacp_doctor.py",
    "validate": "validate_message.py",
    "verify": "message_verify.py",
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


# The memory engine lives in the `agent-memory` tool (`agent-memory-cli` on
# PyPI). `oacp memory …` and `oacp org-memory …` delegate to it by exec: argv
# passes through, `--oacp-dir X` becomes `--home X`, and `oacp memory init`
# maps to `agent-memory enable` (the tool's own `init` scaffolds a home
# without git; `enable` is what the kernel's `init` did). The kernel bundles
# no fallback engine. This shim lasts through 0.5.1 and is removed in 0.5.2.
MEMORY_TOOL = "agent-memory"
MEMORY_TOOL_DISTRIBUTION = "agent-memory-cli"
DELEGATED_COMMANDS: Dict[str, Sequence[str]] = {
    "memory": (),
    "org-memory": ("org",),
}
_DELEGATED_VERBS: Dict[str, Dict[str, str]] = {"memory": {"init": "enable"}}
_HOME_FLAG, _LEGACY_HOME_FLAG = "--home", "--oacp-dir"


def delegated_argv(command: str, argv: Sequence[str]) -> List[str]:
    """Return the `agent-memory` argv for an `oacp <command> <argv>` call."""
    rest = list(argv)
    verbs = _DELEGATED_VERBS.get(command, {})
    if rest and rest[0] in verbs:
        rest[0] = verbs[rest[0]]
    rewritten: List[str] = []
    for arg in rest:
        if arg == _LEGACY_HOME_FLAG:
            rewritten.append(_HOME_FLAG)
        elif arg.startswith(_LEGACY_HOME_FLAG + "="):
            rewritten.append(_HOME_FLAG + arg[len(_LEGACY_HOME_FLAG) :])
        else:
            rewritten.append(arg)
    return [MEMORY_TOOL, *DELEGATED_COMMANDS[command], *rewritten]


def _delegate(command: str, argv: Sequence[str]) -> int:
    tool = shutil.which(MEMORY_TOOL)
    if tool is None:
        print(
            f"ERROR: `oacp {command}` delegates to `{MEMORY_TOOL}`, which is not on PATH; "
            f"install it with `pip install {MEMORY_TOOL_DISTRIBUTION}`.",
            file=sys.stderr,
        )
        return 127
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(tool, delegated_argv(command, argv))
    return 0  # pragma: no cover - execvp does not return


def _dispatch(command: str, argv: Sequence[str]) -> int:
    if command in DELEGATED_COMMANDS:
        return _delegate(command, argv)
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
