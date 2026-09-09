#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Generate runtime-specific configuration files in a repo directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from add_agent import (
    CLAUDE_SUPPORTED_MESSAGE_TYPES,
    CODEX_SUPPORTED_MESSAGE_TYPES,
    add_agent,
    ensure_agent_card_message_types,
)
from _oacp_constants import (
    CODEX_SESSION_START_CONTEXT_LIMIT,
    CREATABLE_RUNTIMES,
    _template_path,
    _write_if_missing,
)

# ── Inline defaults (used when no template file exists) ──────────────────────

CODEX_AGENTS_MD = """\
# AGENTS.md — Codex OACP Instructions

## Protocol

This repo uses the **Open Agent Coordination Protocol (OACP)** for multi-agent
coordination. Your inbox is at `$OACP_HOME/projects/<project>/agents/codex/inbox/`.

## Workflow

1. **Load startup context** — after trusting the generated project hook with
   `/hooks`, it verifies the required protocol and project memory files in one
   `SessionStart` command. Read the files named in its developer context before
   normal work and include its `SESSION_INIT_ACK` in the first response.
   Memory sync is the memory tool's startup hook (`agent-memory setup codex`).
   Org memory is retrieved on demand; read applicable org rules and decisions
   before work they govern. Syncing does not load org content into context.
2. **Check inbox when requested** — surface pending state before processing work.
3. **Send messages** via `oacp send <project> --from codex --to <agent> --type <type> --subject "..." --body "..."`.
4. **Update status** in `agents/codex/status.yaml` when starting/finishing tasks.
5. **Follow guardrails** in `docs/protocol/agent_safety_defaults.md`.

## Key Commands

```bash
oacp doctor --project <project>          # health check
oacp session-init --project <project>    # manual hook fallback
oacp send <project> --from codex ...     # send a message
oacp validate <message.yaml>             # validate a message
```
"""

GEMINI_OACP_RULES = """\
# OACP Rules for Gemini

## Protocol

This repo uses the **Open Agent Coordination Protocol (OACP)** for multi-agent
coordination. Your inbox is at `$OACP_HOME/projects/<project>/agents/gemini/inbox/`.

## Workflow

1. **Check inbox** at session start — process any pending messages.
2. **Send messages** via `oacp send <project> --from gemini --to <agent> --type <type> --subject "..." --body "..."`.
3. **Update status** in `agents/gemini/status.yaml` when starting/finishing tasks.
4. **Follow guardrails** in `docs/protocol/agent_safety_defaults.md`.

## Key Commands

```bash
oacp doctor --project <project>          # health check
oacp send <project> --from gemini ...    # send a message
oacp validate <message.yaml>             # validate a message
```
"""

CURSOR_OACP_TODO = """\
# OACP for Cursor

TODO: Cursor-owned onboarding will add check-inbox rules and memory hooks here.

This placeholder only marks the repo as intentionally prepared for OACP. Do not
treat it as a working inbox processor.

Until Cursor-owned rules land, Cursor sessions must set OACP_RUNTIME=cursor or
pass --from explicitly when sending OACP messages.
"""

# Memory hooks belong to the memory tool (`agent-memory setup <runtime>`).
# Earlier kernels wrote these two Claude hook scripts and registered them; a
# regeneration retires the registrations by exact command and removes the
# files only when their bytes are one of the generated texts, digest for
# digest. Anything else at those paths is somebody's own work and is kept.
MEMORY_TOOL = "agent-memory"
CLAUDE_LEGACY_MEMORY_PULL_COMMAND = ".claude/hooks/oacp-memory-pull.sh"
CLAUDE_LEGACY_MEMORY_PUSH_COMMAND = ".claude/hooks/oacp-memory-push.sh"
CLAUDE_LEGACY_MEMORY_PULL_HOOK = """\
#!/usr/bin/env bash
# Claude hook event: SessionStart (startup)
set -u

OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
if [[ ! -f "$OACP_ROOT/.oacp-memory-repo" ]]; then
  exit 0
fi

oacp memory pull --oacp-dir "$OACP_ROOT" || true
"""
CLAUDE_LEGACY_MEMORY_PUSH_HOOK = """\
#!/usr/bin/env bash
# Claude hook event: SessionEnd / wrap-up
set -u

OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
if [[ ! -f "$OACP_ROOT/.oacp-memory-repo" ]]; then
  exit 0
fi

oacp memory push --oacp-dir "$OACP_ROOT" || true
"""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: Registrations retired by exact command, per hook event.
CLAUDE_LEGACY_MEMORY_REGISTRATIONS = {
    "SessionStart": (CLAUDE_LEGACY_MEMORY_PULL_COMMAND,),
    "SessionEnd": (CLAUDE_LEGACY_MEMORY_PUSH_COMMAND,),
}
#: Files removed only when the digest of their raw bytes is one of these, per
#: repo-relative path: a CRLF copy or any edited byte is not the generated file.
CLAUDE_LEGACY_MEMORY_FILES = {
    CLAUDE_LEGACY_MEMORY_PULL_COMMAND: (
        _digest(CLAUDE_LEGACY_MEMORY_PULL_HOOK.encode("utf-8")),
    ),
    CLAUDE_LEGACY_MEMORY_PUSH_COMMAND: (
        _digest(CLAUDE_LEGACY_MEMORY_PUSH_HOOK.encode("utf-8")),
    ),
}

CLAUDE_SETTINGS_SCHEMA = "https://json.schemastore.org/claude-code-settings.json"
CLAUDE_HOOK_COMMANDS = {
    # Static envelope shim: per-task constraints live in the
    # compiled active_envelope.json, so this settings entry never changes per
    # dispatch and is a no-op while no envelope is active.
    "PreToolUse": {
        "matcher": "Bash|Edit|Write|NotebookEdit",
        "hooks": [
            {
                "type": "command",
                "command": "oacp-envelope-hook",
                "timeout": 15,
            }
        ],
    },
}

CODEX_HOOKS_DESCRIPTION = "OACP startup verification for this workspace."
# The managed startup command in either era: the prefix, the retired
# `--pull-memory` flag earlier kernels added, and the generated `--project` /
# `--hub-dir` arguments, each at most once. Only that grammar is replaced on
# regeneration; a command carrying any other token (an extra flag, a shell
# operator, an appended command) is a custom hook and is preserved.
CODEX_SESSION_START_COMMAND_PREFIX = (
    "oacp",
    "session-init",
    "--hook",
)
CODEX_LEGACY_PULL_FLAG = "--pull-memory"
CODEX_SESSION_START_VALUE_OPTIONS = ("--project", "--hub-dir")
# A generated option value carries an expansion character only single-quoted,
# the way shlex.join emits it; one spelled any other way was written by hand
# for a shell to expand.
CODEX_SHELL_EXPANSION_CHARS = ("$", "`")


def _load_template(relative: str) -> Optional[str]:
    """Load a template file, returning None if not found."""
    try:
        with _template_path(relative) as path:
            return path.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError):
        return None


def _hook_command_exists(entries: List[Any], command: str) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if isinstance(hook, dict) and hook.get("command") == command:
                return True
    return False


def _shell_words(command: str) -> Optional[List[str]]:
    """Split like a POSIX shell, with operators as words of their own.

    `shlex.split` keeps `demo;true` as one word; the punctuation-aware lexer
    yields `demo`, `;`, `true`, so an attached operator, pipe or redirection
    surfaces as a token the generated grammar does not contain. Quoted words
    stay whole, so a generated `--hub-dir '/srv/oacp home'` still parses, and
    `#` is an ordinary character rather than a comment, as in `shlex.split`.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _is_codex_session_start_command(command: Any) -> bool:
    """True only for a command the kernel generated, in either era."""
    if not isinstance(command, str):
        return False
    argv = _shell_words(command)
    if argv is None:
        return False
    # shlex.join reproduces its own output exactly, so a command that survives
    # the round trip carries every `$` and backtick as a quoted literal the
    # shell will not expand; the exclusion below is for the spellings that
    # do not (`--hub-dir $HOME/x`, `--hub-dir "$HOME/x"`).
    literal = shlex.join(argv) == command
    prefix = list(CODEX_SESSION_START_COMMAND_PREFIX)
    if argv[: len(prefix)] != prefix:
        return False
    rest = argv[len(prefix) :]
    seen = set()
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in seen:
            return False
        seen.add(token)
        if token == CODEX_LEGACY_PULL_FLAG:
            index += 1
            continue
        if token in CODEX_SESSION_START_VALUE_OPTIONS and index + 1 < len(rest):
            value = rest[index + 1]
            if value.startswith("-") or (
                not literal
                and any(char in value for char in CODEX_SHELL_EXPANSION_CHARS)
            ):
                return False
            index += 2
            continue
        return False
    return True


def _has_codex_managed_entry(entries: List[Any]) -> bool:
    """True when any SessionStart entry already holds a kernel-generated command."""
    for existing in entries:
        if not isinstance(existing, dict):
            continue
        hooks = existing.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if isinstance(hook, dict) and _is_codex_session_start_command(
                hook.get("command")
            ):
                return True
    return False


def _replace_codex_session_start_entry(
    entries: List[Any],
    replacement: Dict[str, Any],
    *,
    create_when_absent: bool = True,
) -> bool:
    """Replace all OACP-managed startup hooks while preserving custom entries.

    With ``create_when_absent`` false, a list holding no managed entry is left
    untouched: setup regenerates the entry it owns, it never introduces one.
    """
    updated_entries: List[Any] = []
    replacement_added = False

    for existing in entries:
        if not isinstance(existing, dict):
            updated_entries.append(existing)
            continue
        hooks = existing.get("hooks")
        if not isinstance(hooks, list):
            updated_entries.append(existing)
            continue
        retained_hooks = [
            hook
            for hook in hooks
            if not (
                isinstance(hook, dict)
                and _is_codex_session_start_command(hook.get("command"))
            )
        ]
        if len(retained_hooks) == len(hooks):
            updated_entries.append(existing)
            continue

        if not replacement_added:
            updated_entries.append(replacement)
            replacement_added = True
        if retained_hooks:
            retained_entry = dict(existing)
            retained_entry["hooks"] = retained_hooks
            updated_entries.append(retained_entry)

    if not replacement_added:
        if not create_when_absent:
            return False
        updated_entries.append(replacement)
    if updated_entries == entries:
        return False
    entries[:] = updated_entries
    return True


def _remove_hook_command(entries: List[Any], command: str) -> bool:
    """Remove one exact generated command while preserving all custom hooks."""
    changed = False
    retained_entries: List[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            retained_entries.append(entry)
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            retained_entries.append(entry)
            continue
        retained_hooks = [
            hook
            for hook in hooks
            if not (isinstance(hook, dict) and hook.get("command") == command)
        ]
        if len(retained_hooks) == len(hooks):
            retained_entries.append(entry)
            continue
        changed = True
        if retained_hooks:
            updated = dict(entry)
            updated["hooks"] = retained_hooks
            retained_entries.append(updated)
    if changed:
        entries[:] = retained_entries
    return changed


def _warn_claude_settings(settings_file: Path, message: str) -> None:
    print(f"Warning: {settings_file}: {message}", file=sys.stderr)


def _write_claude_memory_settings(repo_dir: Path) -> Optional[bool]:
    """Register the envelope hook and retire the generated memory hook entries."""
    settings_file = repo_dir / ".claude" / "settings.json"
    if settings_file.is_file():
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _warn_claude_settings(
                settings_file,
                f"invalid JSON ({exc.msg}); skipping hook registration.",
            )
            return None
        if not isinstance(data, dict):
            _warn_claude_settings(
                settings_file,
                "expected a JSON object; skipping hook registration.",
            )
            return None
    else:
        data = {"$schema": CLAUDE_SETTINGS_SCHEMA}

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        _warn_claude_settings(
            settings_file,
            "expected hooks to be a JSON object; skipping hook registration.",
        )
        return None

    changed = False
    for event_name, commands in CLAUDE_LEGACY_MEMORY_REGISTRATIONS.items():
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        for command in commands:
            if _remove_hook_command(entries, command):
                changed = True
        if not entries:
            del hooks[event_name]

    for event_name, entry in CLAUDE_HOOK_COMMANDS.items():
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            _warn_claude_settings(
                settings_file,
                f"expected hooks.{event_name} to be a list; skipping hook registration.",
            )
            return None
        command = str(entry["hooks"][0]["command"])
        if not _hook_command_exists(entries, command):
            entries.append(entry)
            changed = True

    if changed:
        try:
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _warn_claude_settings(
                settings_file,
                f"could not write ({exc.strerror or exc}); skipping hook registration.",
            )
            return None
    return changed


def _retire_claude_legacy_memory_files(repo_dir: Path) -> Tuple[List[str], List[str]]:
    """Delete the generated memory hook scripts, byte for byte; keep anything else.

    Runs after the settings file has been written back. Returns (retired, kept):
    repo-relative paths removed, and paths that exist but are kept because
    their bytes are not the generated text, the settings file still names
    them (a registration in a shape the migration does not manage), or the
    path reaches them through a symlink.
    """
    retired: List[str] = []
    kept: List[str] = []
    settings_file = repo_dir / ".claude" / "settings.json"
    try:
        settings_text = (
            settings_file.read_text(encoding="utf-8") if settings_file.is_file() else ""
        )
    except (OSError, UnicodeDecodeError):
        settings_text = None
    for relative, digests in CLAUDE_LEGACY_MEMORY_FILES.items():
        path = repo_dir / relative
        if not path.is_file():
            continue
        if (
            settings_text is None
            or relative in settings_text
            or path.resolve() != (repo_dir.resolve() / relative)
        ):
            kept.append(relative)
            continue
        try:
            generated = _digest(path.read_bytes()) in digests
            if generated:
                path.unlink()
        except OSError:
            kept.append(relative)
            continue
        if generated:
            retired.append(relative)
        else:
            kept.append(relative)
    return retired, kept


def _codex_session_start_command(
    *, project_name: Optional[str], oacp_root: Optional[Path]
) -> str:
    argv = ["oacp", "session-init", "--hook"]
    if project_name:
        argv.extend(["--project", project_name])
    if oacp_root is not None:
        argv.extend(["--hub-dir", str(oacp_root)])
    return shlex.join(argv)


def _codex_session_start_entry(
    *, project_name: Optional[str], oacp_root: Optional[Path]
) -> Dict[str, Any]:
    return {
        "matcher": "^startup$",
        "hooks": [
            {
                "type": "command",
                "command": _codex_session_start_command(
                    project_name=project_name,
                    oacp_root=oacp_root,
                ),
                "timeout": 60,
                "statusMessage": "Checking OACP startup context",
                "additionalContextLimit": CODEX_SESSION_START_CONTEXT_LIMIT,
            }
        ],
    }


def _warn_codex_hooks(hooks_file: Path, message: str) -> None:
    print(f"Warning: {hooks_file}: {message}", file=sys.stderr)


def _write_codex_hooks(
    repo_dir: Path,
    *,
    project_name: Optional[str],
    oacp_root: Optional[Path],
) -> Optional[str]:
    """Create or regenerate the repo-local Codex SessionStart hook definition.

    Returns ``"created"`` when the file was written, ``"unchanged"`` when the
    managed entry was already current, ``"unmanaged"`` when an existing file
    carries no managed entry (left byte-identical — setup regenerates the entry
    it owns and never adds one), or ``None`` when the file was unreadable.
    """
    hooks_file = repo_dir / ".codex" / "hooks.json"
    file_existed = hooks_file.is_file()
    if file_existed:
        try:
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _warn_codex_hooks(
                hooks_file,
                f"invalid JSON ({exc.msg}); skipping hook registration.",
            )
            return None
        if not isinstance(data, dict):
            _warn_codex_hooks(
                hooks_file,
                "expected a JSON object; skipping hook registration.",
            )
            return None
    else:
        data = {"description": CODEX_HOOKS_DESCRIPTION}

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        _warn_codex_hooks(
            hooks_file,
            "expected hooks to be a JSON object; skipping hook registration.",
        )
        return None
    entries = hooks.setdefault("SessionStart", [])
    if not isinstance(entries, list):
        _warn_codex_hooks(
            hooks_file,
            "expected hooks.SessionStart to be a list; skipping hook registration.",
        )
        return None

    # An existing hooks file with no managed entry is the on-disk encoding of a
    # deliberate "startup stays off" choice; regenerating is setup's job, and
    # creating one here would silently reverse that policy.
    if file_existed and not _has_codex_managed_entry(entries):
        return "unmanaged"

    entry = _codex_session_start_entry(
        project_name=project_name,
        oacp_root=oacp_root,
    )
    if not _replace_codex_session_start_entry(
        entries, entry, create_when_absent=not file_existed
    ):
        return "unchanged"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return "created"


def _detect_repo_root(start: Path) -> Optional[Path]:
    """Walk up from *start* looking for a .git directory."""
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def _detect_project_name(repo_dir: Path) -> Optional[str]:
    """Detect project name from .oacp or workspace.json."""
    for name in (".oacp", "workspace.json"):
        marker = repo_dir / name
        if marker.is_symlink() or marker.is_file():
            try:
                resolved = marker.resolve()
                data = json.loads(resolved.read_text(encoding="utf-8"))
                project_name = data.get("project_name")
                if project_name:
                    return project_name
            except (OSError, json.JSONDecodeError):
                pass
    return None


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate runtime-specific OACP configuration files in a repo.",
    )
    parser.add_argument(
        "runtime",
        choices=CREATABLE_RUNTIMES,
        help="Target runtime to configure",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Project name (auto-detected from .oacp if not given)",
    )
    parser.add_argument(
        "--repo-dir",
        default=None,
        help="Repo root directory (auto-detected from .git if not given)",
    )
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    return parser.parse_args(list(argv))


def setup_runtime(
    runtime: str,
    *,
    repo_dir: Path,
    project_name: Optional[str] = None,
    oacp_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate runtime-specific configuration files.

    Returns a dict with ``created_files``, ``skipped_files``, ``unmanaged_files``
    and ``warning_files``.
    """
    if runtime not in CREATABLE_RUNTIMES:
        raise ValueError(
            f"Invalid runtime '{runtime}': must be one of {CREATABLE_RUNTIMES}"
        )

    created_files: List[str] = []
    retired_files: List[str] = []
    skipped_files: List[str] = []
    unmanaged_files: List[str] = []
    warning_files: List[str] = []
    project_created_files: List[str] = []
    project_skipped_files: List[str] = []

    project_label = project_name or "<project>"

    if runtime == "claude":
        # .claude/agents/<project>.md from template
        template_content = _load_template("claude/agents/role_agent.template.md")
        if template_content is None:
            template_content = (
                "---\n"
                f"name: {project_label}\n"
                "description: \"OACP agent role\"\n"
                "tools: Read, Write, Edit, Bash, Glob, Grep\n"
                "model: opus\n"
                "---\n\n"
                "# OACP Agent\n\n"
                "Configure this agent role for your project.\n"
            )
        agent_file = repo_dir / ".claude" / "agents" / f"{project_label}.md"
        if _write_if_missing(agent_file, template_content):
            created_files.append(str(agent_file.relative_to(repo_dir)))
        else:
            skipped_files.append(str(agent_file.relative_to(repo_dir)))

        # .claude/skills/ directory
        skills_dir = repo_dir / ".claude" / "skills"
        if not skills_dir.exists():
            skills_dir.mkdir(parents=True, exist_ok=True)
            created_files.append(".claude/skills/")
        else:
            skipped_files.append(".claude/skills/")

        settings_file = repo_dir / ".claude" / "settings.json"
        settings_result = _write_claude_memory_settings(repo_dir)
        if settings_result is True:
            created_files.append(str(settings_file.relative_to(repo_dir)))
        elif settings_result is False:
            skipped_files.append(str(settings_file.relative_to(repo_dir)))
        else:
            warning_files.append(str(settings_file.relative_to(repo_dir)))

        # The generated hook scripts go only after their registrations are
        # out of a settings file that was read, validated and written back
        # (or needed no change). A refused or unwritable settings file keeps
        # every script beside its still-live registration.
        if settings_result is not None:
            retired, kept = _retire_claude_legacy_memory_files(repo_dir)
            retired_files.extend(retired)
            skipped_files.extend(kept)

        if project_name:
            if oacp_root is None:
                from _oacp_env import resolve_oacp_home

                oacp_root = resolve_oacp_home()
            project_dir = oacp_root / "projects" / project_name
            card_path = project_dir / "agents" / "claude" / "agent_card.yaml"
            if card_path.is_file():
                relative_card = str(card_path.relative_to(project_dir))
                try:
                    card_changed = ensure_agent_card_message_types(
                        card_path,
                        CLAUDE_SUPPORTED_MESSAGE_TYPES,
                    )
                except (OSError, ValueError) as exc:
                    _warn_claude_settings(card_path, str(exc))
                    warning_files.append(str(card_path))
                else:
                    target = (
                        project_created_files if card_changed else project_skipped_files
                    )
                    target.append(relative_card)

    elif runtime == "codex":
        agents_md = repo_dir / "AGENTS.md"
        if _write_if_missing(agents_md, CODEX_AGENTS_MD):
            created_files.append("AGENTS.md")
        else:
            skipped_files.append("AGENTS.md")

        hooks_file = repo_dir / ".codex" / "hooks.json"
        hooks_result = _write_codex_hooks(
            repo_dir,
            project_name=project_name,
            oacp_root=oacp_root,
        )
        if hooks_result == "created":
            created_files.append(str(hooks_file.relative_to(repo_dir)))
        elif hooks_result == "unchanged":
            skipped_files.append(str(hooks_file.relative_to(repo_dir)))
        elif hooks_result == "unmanaged":
            unmanaged_files.append(str(hooks_file.relative_to(repo_dir)))
        else:
            warning_files.append(str(hooks_file.relative_to(repo_dir)))

        if project_name:
            if oacp_root is None:
                from _oacp_env import resolve_oacp_home

                oacp_root = resolve_oacp_home()
            project_dir = oacp_root / "projects" / project_name
            card_path = project_dir / "agents" / "codex" / "agent_card.yaml"
            if card_path.is_file():
                relative_card = str(card_path.relative_to(project_dir))
                try:
                    card_changed = ensure_agent_card_message_types(
                        card_path,
                        CODEX_SUPPORTED_MESSAGE_TYPES,
                    )
                except (OSError, ValueError) as exc:
                    _warn_codex_hooks(card_path, str(exc))
                    warning_files.append(str(card_path))
                else:
                    target = (
                        project_created_files if card_changed else project_skipped_files
                    )
                    target.append(relative_card)

    elif runtime == "gemini":
        rules_file = repo_dir / ".agent" / "rules" / "oacp.md"
        if _write_if_missing(rules_file, GEMINI_OACP_RULES):
            created_files.append(str(rules_file.relative_to(repo_dir)))
        else:
            skipped_files.append(str(rules_file.relative_to(repo_dir)))

    elif runtime == "cursor":
        if project_name:
            if oacp_root is None:
                from _oacp_env import resolve_oacp_home

                oacp_root = resolve_oacp_home()
            project_dir = oacp_root / "projects" / project_name
            if not project_dir.is_dir():
                raise ValueError(
                    f"Project '{project_name}' not found at {project_dir}. "
                    f"Run `oacp init {project_name}` first."
                )
            result = add_agent(
                project_name,
                "cursor",
                oacp_root=oacp_root,
                runtime="cursor",
            )
            project_created_files.extend(result["created_files"])
            project_skipped_files.extend(result["skipped_files"])

        todo_file = repo_dir / ".cursor" / "rules" / "oacp.todo.mdc"
        if _write_if_missing(todo_file, CURSOR_OACP_TODO):
            created_files.append(str(todo_file.relative_to(repo_dir)))
        else:
            skipped_files.append(str(todo_file.relative_to(repo_dir)))

    return {
        "created_files": created_files,
        "retired_files": retired_files,
        "skipped_files": skipped_files,
        "unmanaged_files": unmanaged_files,
        "warning_files": warning_files,
        "project_created_files": project_created_files,
        "project_skipped_files": project_skipped_files,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # Resolve repo dir
    if args.repo_dir:
        repo_dir = Path(args.repo_dir).expanduser().resolve()
    else:
        detected = _detect_repo_root(Path.cwd())
        if detected is None:
            print("Error: could not detect repo root (no .git found). Use --repo-dir.", file=sys.stderr)
            return 1
        repo_dir = detected

    # Resolve project name
    project_name = args.project or _detect_project_name(repo_dir)
    from _oacp_env import resolve_oacp_home

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    try:
        result = setup_runtime(
            args.runtime,
            repo_dir=repo_dir,
            project_name=project_name,
            oacp_root=oacp_root,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Runtime '{args.runtime}' setup in {repo_dir}:")
    for f in result["created_files"]:
        print(f"  + {f}")
    for f in result["skipped_files"]:
        print(f"  ~ {f} (already exists, skipped)")
    for f in result["unmanaged_files"]:
        print(f"  ~ {f} (no managed entry; skipped)")
    for f in result["warning_files"]:
        print(f"  ! {f} (warning, skipped)")
    for f in result["retired_files"]:
        print(f"  - {f} (retired; memory hooks belong to {MEMORY_TOOL})")
    if args.runtime in ("claude", "codex"):
        print(f"  Memory startup hook: run `{MEMORY_TOOL} setup {args.runtime}` (pip install agent-memory-cli).")
    if args.runtime == "codex" and ".codex/hooks.json" not in (
        result["warning_files"] + result["unmanaged_files"]
    ):
        print("  Review and trust the project hook with `/hooks` before relying on it.")
    if result["project_created_files"] or result["project_skipped_files"]:
        print(f"Project agent '{args.runtime}' setup in {oacp_root / 'projects' / str(project_name)}:")
        for f in result["project_created_files"]:
            print(f"  + {f}")
        for f in result["project_skipped_files"]:
            print(f"  ~ {f} (already exists, skipped)")
    if (
        not result["created_files"]
        and not result["retired_files"]
        and not result["skipped_files"]
        and not result["warning_files"]
        and not result["project_created_files"]
        and not result["project_skipped_files"]
    ):
        print("  (no files to create)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
