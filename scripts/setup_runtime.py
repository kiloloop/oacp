#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Generate runtime-specific configuration files in a repo directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from _oacp_constants import CREATABLE_RUNTIMES, _template_path, _write_if_missing

# ── Inline defaults (used when no template file exists) ──────────────────────

CODEX_AGENTS_MD = """\
# AGENTS.md — Codex OACP Instructions

## Protocol

This repo uses the **Open Agent Coordination Protocol (OACP)** for multi-agent
coordination. Your inbox is at `$OACP_HOME/projects/<project>/agents/codex/inbox/`.

## Workflow

1. **Check inbox** at session start — process any pending messages.
2. **Send messages** via `oacp send <project> --from codex --to <agent> --type <type> --subject "..." --body "..."`.
3. **Update status** in `agents/codex/status.yaml` when starting/finishing tasks.
4. **Follow guardrails** in `docs/protocol/agent_safety_defaults.md`.

## Key Commands

```bash
oacp doctor --project <project>          # health check
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

CLAUDE_MEMORY_PULL_HOOK = """\
#!/usr/bin/env bash
# Claude hook event: SessionStart (startup)
set -u

OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
if [[ ! -f "$OACP_ROOT/.oacp-memory-repo" ]]; then
  exit 0
fi

oacp memory pull --oacp-dir "$OACP_ROOT" || true
"""

CLAUDE_MEMORY_PUSH_HOOK = """\
#!/usr/bin/env bash
# Claude hook event: SessionEnd / wrap-up
set -u

OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
if [[ ! -f "$OACP_ROOT/.oacp-memory-repo" ]]; then
  exit 0
fi

oacp memory push --oacp-dir "$OACP_ROOT" || true
"""

CLAUDE_SETTINGS_SCHEMA = "https://json.schemastore.org/claude-code-settings.json"
CLAUDE_MEMORY_HOOK_COMMANDS = {
    "SessionStart": {
        "matcher": "startup",
        "hooks": [
            {
                "type": "command",
                "command": ".claude/hooks/oacp-memory-pull.sh",
                "timeout": 30,
            }
        ],
    },
    "SessionEnd": {
        "hooks": [
            {
                "type": "command",
                "command": ".claude/hooks/oacp-memory-push.sh",
                "timeout": 30,
            }
        ],
    },
}


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | 0o755)


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


def _warn_claude_settings(settings_file: Path, message: str) -> None:
    print(f"Warning: {settings_file}: {message}", file=sys.stderr)


def _write_claude_memory_settings(repo_dir: Path) -> Optional[bool]:
    """Create or update .claude/settings.json with memory hook registrations."""
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
    for event_name, entry in CLAUDE_MEMORY_HOOK_COMMANDS.items():
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
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return changed


def _detect_repo_root(start: Path) -> Optional[Path]:
    """Walk up from *start* looking for a .git directory."""
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def _detect_project_name(repo_dir: Path) -> Optional[str]:
    """Detect project name from .oacp symlink or workspace.json."""
    for name in (".oacp", "workspace.json"):
        marker = repo_dir / name
        if marker.is_symlink() or marker.is_file():
            try:
                resolved = marker.resolve()
                if resolved.name == "workspace.json":
                    data = json.loads(resolved.read_text(encoding="utf-8"))
                    return data.get("project_name")
            except (OSError, json.JSONDecodeError, KeyError):
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
) -> Dict[str, Any]:
    """Generate runtime-specific configuration files.

    Returns a dict with ``created_files``, ``skipped_files``, and ``warning_files``.
    """
    if runtime not in CREATABLE_RUNTIMES:
        raise ValueError(
            f"Invalid runtime '{runtime}': must be one of {CREATABLE_RUNTIMES}"
        )

    created_files: List[str] = []
    skipped_files: List[str] = []
    warning_files: List[str] = []

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

        pull_hook = repo_dir / ".claude" / "hooks" / "oacp-memory-pull.sh"
        if _write_if_missing(pull_hook, CLAUDE_MEMORY_PULL_HOOK):
            _make_executable(pull_hook)
            created_files.append(str(pull_hook.relative_to(repo_dir)))
        else:
            skipped_files.append(str(pull_hook.relative_to(repo_dir)))

        push_hook = repo_dir / ".claude" / "hooks" / "oacp-memory-push.sh"
        if _write_if_missing(push_hook, CLAUDE_MEMORY_PUSH_HOOK):
            _make_executable(push_hook)
            created_files.append(str(push_hook.relative_to(repo_dir)))
        else:
            skipped_files.append(str(push_hook.relative_to(repo_dir)))

        settings_file = repo_dir / ".claude" / "settings.json"
        settings_result = _write_claude_memory_settings(repo_dir)
        if settings_result is True:
            created_files.append(str(settings_file.relative_to(repo_dir)))
        elif settings_result is False:
            skipped_files.append(str(settings_file.relative_to(repo_dir)))
        else:
            warning_files.append(str(settings_file.relative_to(repo_dir)))

    elif runtime == "codex":
        agents_md = repo_dir / "AGENTS.md"
        if _write_if_missing(agents_md, CODEX_AGENTS_MD):
            created_files.append("AGENTS.md")
        else:
            skipped_files.append("AGENTS.md")

    elif runtime == "gemini":
        rules_file = repo_dir / ".agent" / "rules" / "oacp.md"
        if _write_if_missing(rules_file, GEMINI_OACP_RULES):
            created_files.append(str(rules_file.relative_to(repo_dir)))
        else:
            skipped_files.append(str(rules_file.relative_to(repo_dir)))

    return {
        "created_files": created_files,
        "skipped_files": skipped_files,
        "warning_files": warning_files,
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

    try:
        result = setup_runtime(
            args.runtime,
            repo_dir=repo_dir,
            project_name=project_name,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Runtime '{args.runtime}' setup in {repo_dir}:")
    for f in result["created_files"]:
        print(f"  + {f}")
    for f in result["skipped_files"]:
        print(f"  ~ {f} (already exists, skipped)")
    for f in result["warning_files"]:
        print(f"  ! {f} (warning, skipped)")
    if (
        not result["created_files"]
        and not result["skipped_files"]
        and not result["warning_files"]
    ):
        print("  (no files to create)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
