#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""oacp_doctor.py — Check environment and workspace health.

Validates CLI tools, workspace structure, inbox health, YAML schemas,
and agent status files. Designed to run with or without a project context.

Usage:
    oacp_doctor.py                              # environment checks only
    oacp_doctor.py --project <name>             # full checks
    oacp_doctor.py --json                       # machine-readable output
    oacp_doctor.py --project <name> --json      # full + JSON
    oacp_doctor.py --project <name> -o report.txt  # save report to file
    oacp_doctor.py --project <name> --fix          # auto-fix safe issues

Exit codes:
    0 — no errors (warnings are non-blocking)
    1 — one or more blocking errors
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from _oacp_constants import ALL_RUNTIMES, CANONICAL_CAPABILITIES, utc_now_iso
from memory_sync import (
    CANONICAL_MEMORY_GITIGNORE,
    MARKER_FILE,
    STALE_MEMORY_DAYS,
    MemorySyncError,
    compute_git_state,
    escaping_overlay_patterns,
    has_commits,
    is_allowed_memory_path,
    is_configured,
    is_git_repo,
    last_commit_age_days,
    normalize_gitignore,
    overlay_gitignores,
    run_command as run_git_command,
    tracked_files,
    untracked_files,
)

Runner = Callable[[Sequence[str]], Tuple[int, str]]
WhichFn = Callable[[str], Optional[str]]

VALID_RUNTIMES = set(ALL_RUNTIMES)
VALID_STATUSES = {"available", "busy", "offline"}
STALE_STATUS_HOURS = 1
STALE_INBOX_HOURS = 24
YAML_EXTENSIONS = {".yaml", ".yml"}


class Severity(Enum):
    ok = "ok"
    warn = "warn"
    error = "error"
    skip = "skip"


SEVERITY_SYMBOL = {
    Severity.ok: "[+]",
    Severity.warn: "[!]",
    Severity.error: "[x]",
    Severity.skip: "[-]",
}

# ANSI colors for terminal output
SEVERITY_COLOR = {
    Severity.ok: "\033[32m",     # green
    Severity.warn: "\033[33m",   # yellow
    Severity.error: "\033[31m",  # red
    Severity.skip: "\033[90m",   # grey
}
COLOR_RESET = "\033[0m"


@dataclass
class DoctorResult:
    name: str
    severity: Severity
    message: str
    fix_hint: str = ""
    fixable: str = ""  # fix action key: "mkdir_inbox", "create_status", "update_status"


@dataclass
class DoctorCategory:
    name: str
    results: List[DoctorResult] = field(default_factory=list)

    @property
    def worst_severity(self) -> Severity:
        if not self.results:
            return Severity.ok
        priority = [Severity.error, Severity.warn, Severity.skip, Severity.ok]
        for sev in priority:
            if any(r.severity == sev for r in self.results):
                return sev
        return Severity.ok


def run_command(command: Sequence[str]) -> Tuple[int, str]:
    """Run a command and return (exit_code, combined_output)."""
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"Command not found: {command[0]}"
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return completed.returncode, combined.strip()


def _get_version(tool: str, runner: Runner) -> Optional[str]:
    """Try to get a version string from a tool."""
    rc, output = runner([tool, "--version"])
    if rc != 0:
        return None
    # Take first line, strip common prefixes
    first_line = output.splitlines()[0] if output else ""
    return first_line.strip()


def _try_yaml_import() -> Optional[Any]:
    """Try to import PyYAML, return the module or None."""
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError:
        return None


# ── Category 1: Environment ──────────────────────────────────────────────


def check_environment(
    runner: Runner = run_command,
    which_fn: WhichFn = shutil.which,
) -> DoctorCategory:
    """Check required and optional CLI tools."""
    cat = DoctorCategory(name="Environment")

    # Required tools
    for tool in ("git", "python3"):
        path = which_fn(tool)
        if path is None:
            cat.results.append(DoctorResult(
                name=tool,
                severity=Severity.error,
                message=f"{tool} — not found",
                fix_hint=f"Install {tool} and ensure it is on PATH",
            ))
        else:
            version = _get_version(tool, runner) or "installed"
            cat.results.append(DoctorResult(
                name=tool,
                severity=Severity.ok,
                message=f"{tool} — {version}",
            ))

    # Optional tools
    for tool, install_hint in [
        ("gh", "https://cli.github.com"),
        ("ruff", "pip install ruff"),
        ("shellcheck", "brew install shellcheck"),
    ]:
        path = which_fn(tool)
        if path is None:
            cat.results.append(DoctorResult(
                name=tool,
                severity=Severity.skip,
                message=f"{tool} — not installed (optional)",
                fix_hint=f"Install: {install_hint}",
            ))
        else:
            version = _get_version(tool, runner) or "installed"
            cat.results.append(DoctorResult(
                name=tool,
                severity=Severity.ok,
                message=f"{tool} — {version}",
            ))

    # pyyaml
    yaml_mod = _try_yaml_import()
    if yaml_mod is None:
        cat.results.append(DoctorResult(
            name="pyyaml",
            severity=Severity.warn,
            message="pyyaml — not importable (optional; needed for YAML validation)",
            fix_hint="Install: pip install pyyaml",
        ))
    else:
        cat.results.append(DoctorResult(
            name="pyyaml",
            severity=Severity.ok,
            message="pyyaml — available",
        ))

    return cat


# ── Category 2: Workspace ────────────────────────────────────────────────


def check_workspace(project_dir: Path) -> DoctorCategory:
    """Check workspace.json and directory structure."""
    cat = DoctorCategory(name="Workspace")

    ws_file = project_dir / "workspace.json"
    if not ws_file.is_file():
        cat.results.append(DoctorResult(
            name="workspace.json",
            severity=Severity.error,
            message="workspace.json — not found",
            fix_hint=f"Run: make init PROJECT={project_dir.name}",
        ))
    else:
        try:
            data = json.loads(ws_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root must be a JSON object")
            cat.results.append(DoctorResult(
                name="workspace.json",
                severity=Severity.ok,
                message="workspace.json — valid",
            ))
        except (json.JSONDecodeError, ValueError) as exc:
            cat.results.append(DoctorResult(
                name="workspace.json",
                severity=Severity.error,
                message=f"workspace.json — invalid: {exc}",
                fix_hint="Fix JSON syntax in workspace.json",
            ))

    agents_dir = project_dir / "agents"
    if not agents_dir.is_dir():
        cat.results.append(DoctorResult(
            name="agents/",
            severity=Severity.error,
            message="agents/ directory — not found",
            fix_hint=f"Run: make init PROJECT={project_dir.name}",
        ))
    else:
        agent_count = sum(1 for d in agents_dir.iterdir() if d.is_dir())
        cat.results.append(DoctorResult(
            name="agents/",
            severity=Severity.ok,
            message=f"agents/ directory — {agent_count} agent(s)",
        ))

    return cat


# ── Category 3: Inbox Health ─────────────────────────────────────────────


def _count_inbox_messages(inbox_dir: Path) -> Tuple[int, Optional[dt.datetime]]:
    """Count messages and find the oldest timestamp."""
    count = 0
    oldest: Optional[dt.datetime] = None
    if not inbox_dir.is_dir():
        return 0, None
    for f in inbox_dir.iterdir():
        if f.is_file() and f.suffix in YAML_EXTENSIONS:
            count += 1
            mtime = dt.datetime.fromtimestamp(f.stat().st_mtime, tz=dt.timezone.utc)
            if oldest is None or mtime < oldest:
                oldest = mtime
    return count, oldest


def check_inbox_health(
    project_dir: Path,
    now_fn: Optional[Callable[[], dt.datetime]] = None,
) -> DoctorCategory:
    """Check per-agent inbox directories and message staleness."""
    cat = DoctorCategory(name="Inbox Health")
    agents_dir = project_dir / "agents"
    if not agents_dir.is_dir():
        return cat

    now = now_fn() if now_fn is not None else dt.datetime.now(dt.timezone.utc)
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        inbox_dir = agent_dir / "inbox"
        if not inbox_dir.is_dir():
            cat.results.append(DoctorResult(
                name=f"{agent_name}/inbox",
                severity=Severity.warn,
                message=f"{agent_name}/inbox — directory missing",
                fix_hint=f"mkdir -p {inbox_dir}",
                fixable="mkdir_inbox",
            ))
            continue

        count, oldest = _count_inbox_messages(inbox_dir)
        if count == 0:
            cat.results.append(DoctorResult(
                name=f"{agent_name}/inbox",
                severity=Severity.ok,
                message=f"{agent_name}/inbox — empty",
            ))
        elif oldest is not None:
            age_hours = (now - oldest).total_seconds() / 3600
            if age_hours > STALE_INBOX_HOURS:
                cat.results.append(DoctorResult(
                    name=f"{agent_name}/inbox",
                    severity=Severity.warn,
                    message=f"{agent_name}/inbox — {count} message(s), oldest {int(age_hours)}h stale",
                    fix_hint="Process or archive stale inbox messages",
                ))
            else:
                cat.results.append(DoctorResult(
                    name=f"{agent_name}/inbox",
                    severity=Severity.ok,
                    message=f"{agent_name}/inbox — {count} message(s)",
                ))
        else:
            cat.results.append(DoctorResult(
                name=f"{agent_name}/inbox",
                severity=Severity.ok,
                message=f"{agent_name}/inbox — {count} message(s)",
            ))

    return cat


# ── Category 4: Schemas ──────────────────────────────────────────────────


def check_schemas(
    project_dir: Path,
    yaml_loader: Optional[Any] = None,
) -> DoctorCategory:
    """Validate YAML files in packets/ and status.yaml files."""
    cat = DoctorCategory(name="Schemas")

    loader = yaml_loader
    if loader is None:
        yaml_mod = _try_yaml_import()
        if yaml_mod is not None:
            loader = yaml_mod.safe_load

    if loader is None:
        cat.results.append(DoctorResult(
            name="yaml-loader",
            severity=Severity.skip,
            message="YAML validation skipped — pyyaml not available",
            fix_hint="Install: pip install pyyaml",
        ))
        return cat

    # Validate packets/ YAML
    packets_dir = project_dir / "packets"
    if packets_dir.is_dir():
        yaml_files = [
            f for f in sorted(packets_dir.rglob("*"))
            if f.is_file() and f.suffix in YAML_EXTENSIONS
        ]
        errors: List[str] = []
        for yf in yaml_files:
            try:
                loader(yf.read_text(encoding="utf-8"))
            except Exception as exc:
                rel = yf.relative_to(project_dir)
                errors.append(f"{rel}: {exc}")

        if errors:
            cat.results.append(DoctorResult(
                name="packets-yaml",
                severity=Severity.error,
                message=f"packets/ — {len(errors)} invalid YAML file(s): {errors[0]}",
            ))
        elif yaml_files:
            cat.results.append(DoctorResult(
                name="packets-yaml",
                severity=Severity.ok,
                message=f"packets/ — {len(yaml_files)} YAML file(s) valid",
            ))

    # Validate status.yaml files
    agents_dir = project_dir / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            status_file = agent_dir / "status.yaml"
            if not status_file.is_file():
                continue
            agent_name = agent_dir.name
            try:
                data = loader(status_file.read_text(encoding="utf-8"))
                errs = _validate_status_data(data, agent_name)
                if errs:
                    cat.results.append(DoctorResult(
                        name=f"{agent_name}/status.yaml",
                        severity=Severity.error,
                        message=f"{agent_name}/status.yaml — {'; '.join(errs)}",
                    ))
                else:
                    cat.results.append(DoctorResult(
                        name=f"{agent_name}/status.yaml",
                        severity=Severity.ok,
                        message=f"{agent_name}/status.yaml — valid",
                    ))
            except Exception as exc:
                cat.results.append(DoctorResult(
                    name=f"{agent_name}/status.yaml",
                    severity=Severity.error,
                    message=f"{agent_name}/status.yaml — parse error: {exc}",
                ))

    return cat


def _validate_status_data(data: Any, agent_name: str) -> List[str]:
    """Validate a parsed status.yaml against the protocol schema."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["root must be a YAML mapping"]

    # Required fields
    required = ("runtime", "status", "capabilities", "updated_at")
    for key in required:
        if key not in data:
            errors.append(f"missing required field '{key}'")

    runtime = data.get("runtime")
    if runtime is not None and str(runtime) not in VALID_RUNTIMES:
        errors.append(f"runtime '{runtime}' not in {sorted(VALID_RUNTIMES)}")

    status = data.get("status")
    if status is not None and str(status) not in VALID_STATUSES:
        errors.append(f"status '{status}' not in {sorted(VALID_STATUSES)}")

    caps = data.get("capabilities")
    if caps is not None:
        if not isinstance(caps, list):
            errors.append("capabilities must be a list")
        else:
            unknown = [c for c in caps if str(c) not in CANONICAL_CAPABILITIES]
            if unknown:
                errors.append(f"unknown capabilities: {unknown}")

    updated_at = data.get("updated_at")
    if updated_at is not None:
        try:
            dt.datetime.strptime(str(updated_at), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            errors.append(f"updated_at '{updated_at}' is not valid ISO 8601 UTC")

    return errors


# ── Category 5: Agent Status ─────────────────────────────────────────────


def check_agent_status(
    project_dir: Path,
    yaml_loader: Optional[Any] = None,
    now_fn: Optional[Callable[[], dt.datetime]] = None,
) -> DoctorCategory:
    """Check status.yaml presence and staleness per agent."""
    cat = DoctorCategory(name="Agent Status")
    agents_dir = project_dir / "agents"
    if not agents_dir.is_dir():
        return cat

    now = now_fn() if now_fn is not None else dt.datetime.now(dt.timezone.utc)

    loader = yaml_loader
    if loader is None:
        yaml_mod = _try_yaml_import()
        if yaml_mod is not None:
            loader = yaml_mod.safe_load

    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        status_file = agent_dir / "status.yaml"

        if not status_file.is_file():
            cat.results.append(DoctorResult(
                name=f"{agent_name}/status.yaml",
                severity=Severity.warn,
                message=f"{agent_name}/status.yaml — not found",
                fix_hint="Create status.yaml from templates/agent_status.template.yaml",
                fixable="create_status",
            ))
            continue

        # Check staleness via updated_at field
        if loader is not None:
            try:
                data = loader(status_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    updated_at = data.get("updated_at")
                    if updated_at:
                        ts = dt.datetime.strptime(
                            str(updated_at), "%Y-%m-%dT%H:%M:%SZ"
                        ).replace(tzinfo=dt.timezone.utc)
                        age_hours = (now - ts).total_seconds() / 3600
                        if age_hours > STALE_STATUS_HOURS:
                            cat.results.append(DoctorResult(
                                name=f"{agent_name}/status.yaml",
                                severity=Severity.warn,
                                message=(
                                    f"{agent_name}/status.yaml — stale "
                                    f"(updated {int(age_hours)}h ago)"
                                ),
                                fix_hint="Agent may have exited without clean close",
                                fixable="update_status",
                            ))
                            continue
            except Exception:
                pass  # Schema errors caught in check_schemas

        cat.results.append(DoctorResult(
            name=f"{agent_name}/status.yaml",
            severity=Severity.ok,
            message=f"{agent_name}/status.yaml — present",
        ))

    return cat


# ── Category 6: Memory Sync ──────────────────────────────────────────────


def _summarize_paths(paths: List[str], *, limit: int = 3) -> str:
    if not paths:
        return ""
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += f", +{len(paths) - limit} more"
    return shown


def check_memory_sync(
    oacp_dir: Path,
    *,
    runner: Runner = run_command,
    now_fn: Optional[Callable[[], dt.datetime]] = None,
) -> DoctorCategory:
    """Check OACP_HOME memory sync configuration and git state."""
    cat = DoctorCategory(name="Memory Sync")
    marker = oacp_dir / MARKER_FILE

    if runner is run_command:
        def git_runner(command: Sequence[str]) -> Tuple[int, str]:
            return run_git_command(command, cwd=oacp_dir)
    else:
        git_runner = runner

    if not is_configured(oacp_dir):
        cat.results.append(
            DoctorResult(
                name="memory-marker",
                severity=Severity.skip,
                message=f"{MARKER_FILE} — not configured; memory sync hooks are disabled",
                fix_hint="Run: oacp memory init [--remote URL]",
            )
        )
        return cat

    cat.results.append(
        DoctorResult(
            name="memory-marker",
            severity=Severity.ok,
            message=f"{MARKER_FILE} — present",
        )
    )

    if not is_git_repo(oacp_dir, git_runner):
        cat.results.append(
            DoctorResult(
                name="memory-git",
                severity=Severity.warn,
                message=f"{marker} is present, but OACP_HOME is not a git repo",
                fix_hint="Run `oacp memory init` or remove the marker to disable hooks",
            )
        )
        return cat

    root_gitignore = oacp_dir / ".gitignore"
    if not root_gitignore.is_file():
        cat.results.append(
            DoctorResult(
                name="root-gitignore",
                severity=Severity.warn,
                message=".gitignore — missing canonical memory allowlist",
                fix_hint="Run: oacp memory init",
            )
        )
    else:
        content = normalize_gitignore(root_gitignore.read_text(encoding="utf-8"))
        if content == CANONICAL_MEMORY_GITIGNORE:
            cat.results.append(
                DoctorResult(
                    name="root-gitignore",
                    severity=Severity.ok,
                    message=".gitignore — canonical memory allowlist",
                )
            )
        else:
            cat.results.append(
                DoctorResult(
                    name="root-gitignore",
                    severity=Severity.warn,
                    message=".gitignore — drifted from canonical memory allowlist",
                    fix_hint="Run `oacp memory init` to rewrite the root allowlist",
                )
            )

    tracked: Optional[List[str]] = None
    try:
        tracked = tracked_files(oacp_dir, git_runner)
        outside = [path for path in tracked if not is_allowed_memory_path(path)]
    except MemorySyncError as exc:
        cat.results.append(
            DoctorResult(
                name="tracked-allowlist",
                severity=Severity.warn,
                message=f"tracked allowlist check failed: {exc}",
            )
        )
    else:
        if outside:
            cat.results.append(
                DoctorResult(
                    name="tracked-allowlist",
                    severity=Severity.warn,
                    message=(
                        f"{len(outside)} tracked file(s) outside memory allowlist: "
                        f"{_summarize_paths(outside)}"
                    ),
                    fix_hint="Remove runtime state from the memory repo index",
                )
            )
        else:
            cat.results.append(
                DoctorResult(
                    name="tracked-allowlist",
                    severity=Severity.ok,
                    message=f"tracked files — {len(tracked)} inside memory allowlist",
                )
            )

    try:
        untracked = [
            path
            for path in untracked_files(oacp_dir, git_runner)
            if is_allowed_memory_path(path)
        ]
    except MemorySyncError as exc:
        cat.results.append(
            DoctorResult(
                name="untracked-memory",
                severity=Severity.warn,
                message=f"untracked memory check failed: {exc}",
            )
        )
    else:
        if untracked:
            cat.results.append(
                DoctorResult(
                    name="untracked-memory",
                    severity=Severity.warn,
                    message=(
                        f"{len(untracked)} untracked memory-shaped file(s): "
                        f"{_summarize_paths(untracked)}"
                    ),
                    fix_hint="Run: oacp memory push",
                )
            )
        else:
            cat.results.append(
                DoctorResult(
                    name="untracked-memory",
                    severity=Severity.ok,
                    message="untracked memory files — none",
                )
            )

    try:
        state = compute_git_state(oacp_dir, runner=git_runner, fetch=True)
    except MemorySyncError as exc:
        cat.results.append(
            DoctorResult(
                name="working-tree",
                severity=Severity.warn,
                message=f"memory git state check failed: {exc}",
            )
        )
        state = None

    if state is not None:
        if state.dirty:
            cat.results.append(
                DoctorResult(
                    name="working-tree",
                    severity=Severity.warn,
                    message="working tree — DIRTY memory changes present",
                    fix_hint="Run `oacp memory push` or resolve changes manually",
                )
            )
        else:
            cat.results.append(
                DoctorResult(
                    name="working-tree",
                    severity=Severity.ok,
                    message="working tree — clean",
                )
            )

        if not state.has_remote:
            cat.results.append(
                DoctorResult(
                    name="sync-state",
                    severity=Severity.ok,
                    message="sync state — local-only; no remote configured",
                )
            )
            cat.results.append(
                DoctorResult(
                    name="remote",
                    severity=Severity.skip,
                    message="remote — skipped; local-only memory repo",
                )
            )
        elif state.fetch_failed:
            cat.results.append(
                DoctorResult(
                    name="sync-state",
                    severity=Severity.warn,
                    message=f"sync state — remote fetch failed: {state.fetch_output}",
                    fix_hint="Check network access and remote permissions",
                )
            )
            cat.results.append(
                DoctorResult(
                    name="remote",
                    severity=Severity.warn,
                    message="remote — not reachable",
                    fix_hint="Check network access and remote permissions",
                )
            )
        elif not state.has_upstream:
            cat.results.append(
                DoctorResult(
                    name="sync-state",
                    severity=Severity.warn,
                    message="sync state — remote exists but no upstream branch is configured",
                    fix_hint="Run: git -C $OACP_HOME push -u <remote> <branch>",
                )
            )
            cat.results.append(
                DoctorResult(
                    name="remote",
                    severity=Severity.ok,
                    message="remote — reachable",
                )
            )
        elif state.diverged:
            cat.results.append(
                DoctorResult(
                    name="sync-state",
                    severity=Severity.warn,
                    message=(
                        "sync state — DIVERGED from upstream "
                        f"({state.ahead} ahead, {state.behind} behind)"
                    ),
                    fix_hint="Resolve manually; OACP never auto-merges memory",
                )
            )
            cat.results.append(
                DoctorResult(
                    name="remote",
                    severity=Severity.ok,
                    message="remote — reachable",
                )
            )
        elif state.behind:
            cat.results.append(
                DoctorResult(
                    name="sync-state",
                    severity=Severity.warn,
                    message=f"sync state — BEHIND upstream by {state.behind} commit(s)",
                    fix_hint="Run: oacp memory pull",
                )
            )
            cat.results.append(
                DoctorResult(
                    name="remote",
                    severity=Severity.ok,
                    message="remote — reachable",
                )
            )
        elif state.ahead:
            cat.results.append(
                DoctorResult(
                    name="sync-state",
                    severity=Severity.warn,
                    message=f"sync state — ahead by {state.ahead} unpushed commit(s)",
                    fix_hint="Run: oacp memory push",
                )
            )
            cat.results.append(
                DoctorResult(
                    name="remote",
                    severity=Severity.ok,
                    message="remote — reachable",
                )
            )
        else:
            cat.results.append(
                DoctorResult(
                    name="sync-state",
                    severity=Severity.ok,
                    message="sync state — synced with upstream",
                )
            )
            cat.results.append(
                DoctorResult(
                    name="remote",
                    severity=Severity.ok,
                    message="remote — reachable",
                )
            )

    if not has_commits(oacp_dir, git_runner):
        cat.results.append(
            DoctorResult(
                name="last-commit",
                severity=Severity.warn,
                message="last commit — none",
                fix_hint="Run: oacp memory push",
            )
        )
    else:
        now = now_fn() if now_fn is not None else dt.datetime.now(dt.timezone.utc)
        age_days = last_commit_age_days(oacp_dir, now=now, runner=git_runner)
        if age_days is None:
            cat.results.append(
                DoctorResult(
                    name="last-commit",
                    severity=Severity.warn,
                    message="last commit — timestamp unavailable",
                )
            )
        elif age_days > STALE_MEMORY_DAYS:
            cat.results.append(
                DoctorResult(
                    name="last-commit",
                    severity=Severity.warn,
                    message=f"last commit — stale ({age_days} day(s) old)",
                    fix_hint="Run: oacp memory push",
                )
            )
        else:
            cat.results.append(
                DoctorResult(
                    name="last-commit",
                    severity=Severity.ok,
                    message=f"last commit — fresh ({age_days} day(s) old)",
                )
            )

    if tracked is not None:
        agents_tracked = [
            path
            for path in tracked
            if path.startswith("agents/")
            or (path.startswith("projects/") and "/agents/" in path)
        ]
        if agents_tracked:
            cat.results.append(
                DoctorResult(
                    name="agents-tracked",
                    severity=Severity.warn,
                    message=(
                        f"{len(agents_tracked)} agents/ file(s) tracked: "
                        f"{_summarize_paths(agents_tracked)}"
                    ),
                    fix_hint="Remove per-instance agent state from the memory repo",
                )
            )
        else:
            cat.results.append(
                DoctorResult(
                    name="agents-tracked",
                    severity=Severity.ok,
                    message="agents/ tracked files — none",
                )
            )

    bad_overlays: List[str] = []
    overlays = list(overlay_gitignores(oacp_dir))
    for overlay in overlays:
        for pattern in escaping_overlay_patterns(overlay):
            bad_overlays.append(f"{overlay.relative_to(oacp_dir)}: {pattern}")
    if bad_overlays:
        cat.results.append(
            DoctorResult(
                name="memory-overlays",
                severity=Severity.warn,
                message=(
                    "memory .gitignore overlays can escape memory/**: "
                    f"{_summarize_paths(bad_overlays)}"
                ),
                fix_hint="Remove overlay unignore patterns containing '..'",
            )
        )
    else:
        cat.results.append(
            DoctorResult(
                name="memory-overlays",
                severity=Severity.ok,
                message=f"memory .gitignore overlays — {len(overlays)} safe",
            )
        )

    return cat


# ── Orchestrator ──────────────────────────────────────────────────────────


def run_doctor(
    *,
    project: Optional[str] = None,
    oacp_dir: Path,
    include_memory: bool = False,
    runner: Runner = run_command,
    yaml_loader: Optional[Any] = None,
    which_fn: WhichFn = shutil.which,
    now_fn: Optional[Callable[[], dt.datetime]] = None,
) -> List[DoctorCategory]:
    """Run all doctor checks and return categorized results."""
    categories: List[DoctorCategory] = []

    # Always run environment checks
    categories.append(check_environment(runner=runner, which_fn=which_fn))

    # Workspace checks require a project
    if project:
        project_dir = oacp_dir / "projects" / project
        if not project_dir.is_dir():
            ws_cat = DoctorCategory(name="Workspace")
            ws_cat.results.append(DoctorResult(
                name="project-dir",
                severity=Severity.error,
                message=f"Project directory not found: {project_dir}",
                fix_hint=f"Run: make init PROJECT={project}",
            ))
            categories.append(ws_cat)
        else:
            categories.append(check_workspace(project_dir))
            categories.append(check_inbox_health(project_dir, now_fn=now_fn))
            categories.append(check_schemas(project_dir, yaml_loader=yaml_loader))
            categories.append(check_agent_status(project_dir, yaml_loader=yaml_loader, now_fn=now_fn))

    if include_memory:
        categories.append(check_memory_sync(oacp_dir, runner=runner, now_fn=now_fn))

    return categories


def has_errors(categories: List[DoctorCategory]) -> bool:
    """Return True if any result has error severity."""
    return any(
        r.severity == Severity.error
        for cat in categories
        for r in cat.results
    )


# ── Output ────────────────────────────────────────────────────────────────


def _use_color() -> bool:
    """Check if stdout supports color."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _write_report(
    categories: List[DoctorCategory],
    fh: Any,
    *,
    color: bool = False,
    fixed: Optional[List[str]] = None,
) -> None:
    """Write flutter-doctor-style report to a file handle."""
    if fixed:
        fh.write("Auto-fixed:\n")
        for desc in fixed:
            fh.write(f"  - {desc}\n")
        fh.write("\n")
    for i, cat in enumerate(categories):
        if i > 0:
            fh.write("\n")
        cat_sev = cat.worst_severity
        sym = SEVERITY_SYMBOL[cat_sev]
        if color:
            c = SEVERITY_COLOR[cat_sev]
            fh.write(f"{c}{sym}{COLOR_RESET} {cat.name}\n")
        else:
            fh.write(f"{sym} {cat.name}\n")

        for result in cat.results:
            sym = SEVERITY_SYMBOL[result.severity]
            if color:
                c = SEVERITY_COLOR[result.severity]
                fh.write(f"    {c}{sym}{COLOR_RESET} {result.message}\n")
            else:
                fh.write(f"    {sym} {result.message}\n")
            if result.fix_hint and result.severity in (Severity.error, Severity.warn, Severity.skip):
                hint_prefix = "        "
                if color:
                    fh.write(f"{hint_prefix}\033[2m{result.fix_hint}{COLOR_RESET}\n")
                else:
                    fh.write(f"{hint_prefix}{result.fix_hint}\n")

    errs = has_errors(categories)
    fh.write("\n")
    if errs:
        msg = "Doctor found issues that need attention."
        fh.write(f"\033[31m{msg}{COLOR_RESET}\n" if color else f"{msg}\n")
    else:
        msg = "No issues found."
        fh.write(f"\033[32m{msg}{COLOR_RESET}\n" if color else f"{msg}\n")


def print_report(categories: List[DoctorCategory], *, fixed: Optional[List[str]] = None) -> None:
    """Print flutter-doctor-style report to stdout."""
    _write_report(categories, sys.stdout, color=_use_color(), fixed=fixed)


def _build_json(categories: List[DoctorCategory], *, fixed: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build JSON-serializable dict from categories."""
    output: Dict[str, Any] = {
        "has_errors": has_errors(categories),
        "fixed": fixed or [],
        "categories": [],
    }
    for cat in categories:
        cat_dict: Dict[str, Any] = {
            "name": cat.name,
            "worst_severity": cat.worst_severity.value,
            "results": [],
        }
        for r in cat.results:
            result_dict: Dict[str, Any] = {
                "name": r.name,
                "severity": r.severity.value,
                "message": r.message,
            }
            if r.fix_hint:
                result_dict["fix_hint"] = r.fix_hint
            cat_dict["results"].append(result_dict)
        output["categories"].append(cat_dict)
    return output


def print_json(categories: List[DoctorCategory], *, fixed: Optional[List[str]] = None) -> None:
    """Print machine-readable JSON output to stdout."""
    print(json.dumps(_build_json(categories, fixed=fixed), indent=2))


# ── Fix ───────────────────────────────────────────────────────────────────


def apply_fixes(
    categories: List[DoctorCategory],
    oacp_dir: Path,
    project: str,
) -> List[str]:
    """Apply auto-fixes for fixable results. Returns list of fix descriptions."""
    fixed: List[str] = []
    project_dir = oacp_dir / "projects" / project
    template_path = oacp_dir / "templates" / "agent_status.template.yaml"
    # Fall back to repo-bundled template
    repo_template = Path(__file__).resolve().parent.parent / "templates" / "agent_status.template.yaml"

    for cat in categories:
        for result in cat.results:
            if not result.fixable:
                continue

            # Extract agent name from result.name (e.g. "claude/inbox" → "claude")
            agent_name = result.name.split("/")[0]
            agent_dir = project_dir / "agents" / agent_name

            if result.fixable == "mkdir_inbox":
                inbox_dir = agent_dir / "inbox"
                inbox_dir.mkdir(parents=True, exist_ok=True)
                result.severity = Severity.ok
                result.message = f"{agent_name}/inbox — created"
                result.fix_hint = ""
                result.fixable = ""
                fixed.append(f"Created {agent_name}/inbox/")

            elif result.fixable == "create_status":
                status_file = agent_dir / "status.yaml"
                # Find template: workspace copy first, then repo-bundled
                tmpl = template_path if template_path.is_file() else repo_template
                if tmpl.is_file():
                    content = tmpl.read_text(encoding="utf-8")
                    now_str = utc_now_iso()
                    # Set runtime to agent name (if recognized) or "unknown"
                    runtime = agent_name if agent_name in VALID_RUNTIMES else "unknown"
                    content = re.sub(
                        r'^runtime:.*$',
                        f'runtime: {runtime}',
                        content,
                        flags=re.MULTILINE,
                    )
                    content = content.replace(
                        'updated_at: "2026-02-16T20:00:00Z"',
                        f'updated_at: "{now_str}"',
                    )
                    status_file.write_text(content, encoding="utf-8")
                    result.severity = Severity.ok
                    result.message = f"{agent_name}/status.yaml — created from template"
                    result.fix_hint = ""
                    result.fixable = ""
                    fixed.append(f"Created {agent_name}/status.yaml")

            elif result.fixable == "update_status":
                status_file = agent_dir / "status.yaml"
                if status_file.is_file():
                    now_str = utc_now_iso()
                    text = status_file.read_text(encoding="utf-8")
                    text = re.sub(
                        r'^updated_at:.*$',
                        f'updated_at: "{now_str}"',
                        text,
                        flags=re.MULTILINE,
                    )
                    status_file.write_text(text, encoding="utf-8")
                    result.severity = Severity.ok
                    result.message = f"{agent_name}/status.yaml — timestamp updated"
                    result.fix_hint = ""
                    result.fixable = ""
                    fixed.append(f"Updated {agent_name}/status.yaml timestamp")

    return fixed


# ── CLI ───────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check environment and workspace health.",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Project name under <oacp-dir>/projects/ (enables workspace checks)",
    )
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output machine-readable JSON",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-fix safe issues (missing inbox dirs, missing/stale status.yaml)",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Run advisory checks for OACP_HOME memory git sync",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Save report to file (in addition to stdout)",
    )
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    from _oacp_env import resolve_oacp_home
    oacp_dir = resolve_oacp_home(args.oacp_dir).resolve()

    categories = run_doctor(
        project=args.project,
        oacp_dir=oacp_dir,
        include_memory=args.memory,
    )

    fixed: List[str] = []
    if args.fix and args.project:
        fixed = apply_fixes(categories, oacp_dir, args.project)

    if args.json_output:
        print_json(categories, fixed=fixed)
    else:
        print_report(categories, fixed=fixed)

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            if args.json_output:
                json.dump(_build_json(categories, fixed=fixed), fh, indent=2)
                fh.write("\n")
            else:
                _write_report(categories, fh, fixed=fixed)
        print(f"\nReport saved to {out_path}", file=sys.stderr)

    return 1 if has_errors(categories) else 0


if __name__ == "__main__":
    raise SystemExit(main())
