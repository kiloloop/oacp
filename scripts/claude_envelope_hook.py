#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Claude PreToolUse hook: enforce the active runtime envelope.

Registered once by ``oacp setup claude`` (static shim). On every Bash / Edit /
Write / NotebookEdit call it looks for ``active_envelope.json`` in the agent
workspace of the project owning the tool call's cwd. No envelope means no
opinion: the hook exits 0 with no output and the harness's normal permission
flow proceeds — exactly today's behavior.

With an envelope active, the hook classifies the call against the compiled
constraints and emits a PreToolUse ``permissionDecision``:

- ``deny``  — the call breaches a declared-false capability, targets a repo
  outside the receiver allowlist, or drifts past ``expected_files_touched``
  (denied with the canonical ``[oacp-envelope] Blocked: autonomy threshold
  exceeded`` opener so the session pivots to the §E checkpoint protocol).
- ``ask``   — the hook cannot confidently classify the call (exotic compound
  command, unresolvable repo). The exact command is escalated for
  just-in-time human review instead of blanket-denied or silently allowed.
- allow     — emitted as *no output*, never as an explicit ``allow`` decision,
  so the envelope can only tighten the harness's permission surface, never
  bypass it.

``oacp send`` is never denied: it is the §E notification pipe. The exemption
is exactly that wide — other oacp subcommands are classified. Envelope
self-modification is denied: ``oacp envelope compile`` always, and ``oacp
envelope clear`` until the task's newest audit record shows a terminal
``result.final_state`` — the protocol's documented completion clear is
validated against that record instead of blanket-denied, so an enforced
session can exit its own envelope exactly once the task lifecycle is over.
Determinable Bash write targets (redirects and common writer programs) pass
through the same secret/dependency/file-counter gate as Edit/Write calls.
Protocol bookkeeping surfaces — the receiver's own audit/inbox/outbox
directories and the runtime scratchpad — are the enforcement layer's
instrumentation, not task scope: they never consume the
``expected_files_touched`` budget (the secret and dependency gates still
apply, and containment is judged on resolved paths so symlinks cannot smuggle
task scope under an exempt root). The receiver's ``state/`` directory is the
opposite of exempt: the active envelope lives there, and writing, removing,
or relocating it from inside the session is denied as self-modification.
Trust roots (receiver pins and the project catalog) are authority-bearing
auth config, gated by ``touches_auth_config_or_secrets``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from autonomy_gate import DESTRUCTIVE_PATTERNS, load_yaml_file
from envelope_compiler import (
    ENVELOPE_FILENAME,
    SESSION_CLAIM_FILENAME,
    envelope_path,
    load_envelope,
    write_envelope,
    write_session_claim,
)

SESSION_CLAIM_STEM = SESSION_CLAIM_FILENAME.rsplit(".", 1)[0]

REASON_PREFIX = "[oacp-envelope]"
BLOCKED_OPENER = f"{REASON_PREFIX} Blocked: autonomy threshold exceeded"
# Shell expansion syntax the classifier cannot statically resolve: the shell
# expands globs/braces AFTER classification, so a pattern operand can reach a
# protected path while its literal spelling does not. Such operands escalate.
EXPANSION_SYNTAX_RE = re.compile(r"[*?\[\]{}]")
SUBSTITUTION_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
REDIRECT_TARGET_RE = re.compile(r">>?\s*([^\s;|&]+)")
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
GIT_URL_RE = re.compile(
    r"(?:[/:])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

WRAPPER_COMMANDS = {"command", "builtin", "nohup", "time", "env"}
# Programs that execute arbitrary nested commands the classifier cannot see
# through — always escalated, never allowed through unclassified.
SHELL_INDIRECTION = {
    "sh", "bash", "zsh", "dash", "ksh", "fish", "eval", "exec", "xargs",
    "source", ".",
}
# Known runners whose nested command is visible in argv: recursively
# classified instead of escalated (F-007: `uv run oacp envelope clear`).
RUNNER_VERBS = {"uv": "run", "poetry": "run", "pipx": "run", "npm": "exec",
                "pnpm": "exec", "yarn": "exec"}
DIRECT_RUNNERS = {"npx", "uvx"}
# Interpreter inline-code flags execute uninspectable nested code (F-007).
INTERPRETER_CODE_FLAGS = {
    "python": {"-c"}, "python2": {"-c"}, "python3": {"-c"},
    "node": {"-e", "--eval", "-p"}, "nodejs": {"-e", "--eval", "-p"},
    "ruby": {"-e"}, "perl": {"-e", "-E"},
}
PROTECTED_BRANCHES = {"main", "master"}
# git push flags that can update refs far beyond a single PR branch.
GIT_PUSH_BULK_FLAGS = {
    "--mirror", "--all", "--tags", "--follow-tags", "--delete", "-d", "--prune",
}
# oacp subcommands an enveloped session may always run (the send pipe plus
# read-only surfaces). `envelope compile|--extend` is self-modification of
# the active constraints and is denied; `envelope clear` is the documented
# completion step, sanctioned only by a terminal audit record (see
# _classify_envelope_clear); everything else escalates.
OACP_ALWAYS_ALLOWED = {"send", "inbox", "validate", "doctor", "watch", "help", ""}
# `result.final_state` values that mark the task lifecycle over. `paused`
# is deliberately absent: a checkpoint-paused task keeps its envelope and
# re-authorizes via the §E flow (recompile with --extend), never via clear.
TERMINAL_AUDIT_STATES = {"done", "error"}
# Receiver-workspace directories that are protocol bookkeeping, not task
# scope: audit records, inbox claims, outbox copies. Deliberately NOT the
# receiver's state/ (the active envelope is the policy object — direct
# writes are self-modification and deny) nor trust roots (authority-bearing
# auth config, gated by touches_auth_config_or_secrets). The receiver's
# config.yaml sits above all of these and stays fully gated.
BOOKKEEPING_AGENT_SUBDIRS = ("audit", "inbox", "outbox")
# Runtime scratchpad roots (reply/body-file composition). Deliberately
# narrower than the OS temp root: exempting all of /tmp would let arbitrary
# staging escape the counter, and would swallow test fixtures on CI.
SCRATCHPAD_PREFIXES = ("/tmp/claude-", "/private/tmp/claude-")

DEPENDENCY_FILENAMES = {
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
}
PACKAGE_MANAGERS = {
    "pip",
    "pip3",
    "uv",
    "npm",
    "yarn",
    "pnpm",
    "brew",
    "poetry",
    "cargo",
    "gem",
    "apt",
    "apt-get",
}
DEPENDENCY_VERBS = {"install", "add", "remove", "uninstall"}

GH_READONLY_ACTIONS = {"view", "list", "status", "checks", "diff", "download"}
GH_PR_CLASS = {("pr", "create"), ("pr", "edit"), ("pr", "ready"), ("pr", "update-branch")}
GH_COMMENT_CLASS = {("pr", "comment"), ("pr", "review"), ("issue", "comment")}
# Constraint-gated classes for the granular capabilities beyond PR artifacts
# and comments. `merges_pr` is exactly the merge; `files_issues` is issue
# lifecycle plus `label create` (issue-adjacent metadata — labels must be
# creatable before they can be applied). Everything else in the groups stays
# denied below. These are consulted BEFORE the deny classes: a declared-true
# capability wins over the group-level deny, a declared-false one denies with
# the constraint named.
GH_MERGE_CLASS = {("pr", "merge")}
GH_ISSUE_CLASS = {("issue", "create"), ("issue", "edit"), ("issue", "close"), ("label", "create")}
GH_DENY_CLASS = {
    ("pr", "close"),
    ("pr", "reopen"),
    ("pr", "lock"),
    ("pr", "unlock"),
    ("issue", "reopen"),
    ("issue", "delete"),
    ("issue", "transfer"),
    ("issue", "pin"),
    ("issue", "unpin"),
    ("issue", "lock"),
    ("issue", "unlock"),
}
GH_DENY_GROUPS = {"release", "repo", "gist", "secret", "variable", "label", "ruleset"}
GH_AUTH_MUTATIONS = {"login", "logout", "refresh", "setup-git", "token"}
GH_WORKFLOW_MUTATIONS = {
    ("workflow", "run"),
    ("workflow", "enable"),
    ("workflow", "disable"),
    ("run", "rerun"),
    ("run", "cancel"),
    ("cache", "delete"),
}
# Value-taking gh flags: skip flag AND value when deriving group/action, so a
# flag's value can never be misread as a positional (F-003: `gh --repo X pr
# merge` must classify as pr/merge, not X/pr). Unknown flags skip only
# themselves; if that misparses an exotic value-taking flag, the resulting
# unknown group/action falls into the fail-closed ask below.
GH_VALUE_FLAGS = {
    "-R", "--repo", "-X", "--method", "-H", "--header", "-q", "--jq",
    "-t", "--template", "-b", "--body", "--body-file", "-B", "--base",
    "--head", "--title", "-m", "--milestone", "-a", "--assignee",
    "-l", "--label", "-p", "--project", "--hostname", "-A", "--app",
}
# gh api flags that implicitly switch the request from GET to POST.
GH_API_MUTATION_FLAGS = {"-f", "--field", "-F", "--raw-field", "--input"}
# Bash writer programs whose file targets are determinable from argv.
BASH_WRITERS = {"touch", "tee", "cp", "mv", "install", "truncate"}
# Filesystem mutators whose OPERANDS (sources and destinations alike) must
# honor the state/trust protection: deleting, relocating, copying out, or
# aliasing protected material is as much a mutation as writing it.
FS_MUTATORS = {"rm", "unlink", "mv", "cp", "install", "truncate", "shred", "ln"}


class Decision:
    __slots__ = ("action", "reason", "new_files")

    def __init__(
        self,
        action: str,
        reason: str = "",
        new_files: Optional[List[str]] = None,
    ) -> None:
        self.action = action
        self.reason = reason
        self.new_files = new_files or []


ALLOW = Decision("allow")


def _deny(reason: str) -> Decision:
    return Decision("deny", _prefixed_reason(reason))


def _ask(reason: str) -> Decision:
    return Decision("ask", _prefixed_reason(reason))


def _prefixed_reason(reason: str) -> str:
    """Return one operator-visible reason with the stable OACP tag."""
    if reason.startswith(REASON_PREFIX):
        return reason
    return f"{REASON_PREFIX} {reason}"


def _with_message_id(decision: Decision, message_id: str) -> Decision:
    """Add the active task id to a non-allow decision when it is known."""
    if decision.action == "allow" or not message_id:
        return decision
    return Decision(
        decision.action,
        f"{_prefixed_reason(decision.reason)} [task {message_id}]",
        decision.new_files,
    )


class WorkspaceContext:
    """Workspace facts resolved by ``process()`` that classification needs
    beyond the envelope's own constraints: where the receiver's bookkeeping
    surfaces live, and which audit records can sanction a completion clear.
    ``None`` (no resolved workspace, e.g. direct ``classify`` calls) keeps
    every context-dependent decision fail-closed."""

    __slots__ = ("oacp_root", "project", "receiver", "message_id")

    def __init__(
        self, oacp_root: Path, project: str, receiver: str, message_id: str
    ) -> None:
        self.oacp_root = oacp_root
        self.project = project
        self.receiver = receiver
        self.message_id = message_id

    def agent_dir(self) -> Path:
        return (
            self.oacp_root / "projects" / self.project / "agents" / self.receiver
        )

    def audit_dir(self) -> Path:
        return self.agent_dir() / "audit" / "autonomy_decisions"

    def state_dir(self) -> Path:
        return self.agent_dir() / "state"

    def trust_roots(self) -> List[Path]:
        return [
            self.agent_dir() / "trust",
            self.oacp_root / "projects" / self.project / "trust",
        ]

    def bookkeeping_prefixes(self) -> List[str]:
        agent_dir = self.agent_dir()
        return [str(agent_dir / name) for name in BOOKKEEPING_AGENT_SUBDIRS]


# ── Workspace discovery ───────────────────────────────────────────────────────


def find_project(cwd: Path) -> Optional[str]:
    """Walk upward from cwd looking for an OACP workspace marker."""
    for root in (cwd, *cwd.parents):
        for name in (".oacp", "workspace.json"):
            marker = root / name
            if not (marker.is_file() or marker.is_symlink()):
                continue
            try:
                data = json.loads(marker.resolve().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("project_name"):
                return str(data["project_name"])
    return None


# ── Path classification ───────────────────────────────────────────────────────


def is_secret_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if name == ".env" or name.startswith(".env."):
        return True
    if "credentials" in name.lower():
        return True
    if name.endswith((".pem", ".key")):
        return True
    if "/.ssh/" in normalized or normalized.startswith((".ssh/", "~/.ssh/")):
        return True
    parts = [part for part in normalized.split("/") if part]
    for index in range(len(parts) - 2):
        if parts[index] == "agents" and parts[index + 2] in ("config.yaml", "config.yml"):
            return True
    return False


def is_dependency_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if name in DEPENDENCY_FILENAMES:
        return True
    return name.startswith("requirements") and name.endswith(".txt")


_TOML_TABLE_RE = re.compile(
    r"^[ \t]*\[(?P<name>[^\[\]\r\n]+)\][ \t]*(?:#.*)?(?:\r?\n)?$"
)
_PROJECT_VERSION_RE = re.compile(
    r"^(?P<prefix>[ \t]*version[ \t]*=[ \t]*)"
    r"(?P<literal>\"(?:\\.|[^\"\\])*\"|'[^']*')"
    r"(?P<suffix>[ \t]*(?:#.*)?)(?P<newline>\r?\n)?$"
)


def _canonicalize_project_version(document: str) -> Optional[Tuple[str, str]]:
    """Replace the sole ``[project] version`` literal with a sentinel.

    This is deliberately narrower than a general TOML parser: the exemption
    must prove that every byte outside one ordinary version-string literal is
    unchanged. Unsupported or ambiguous shapes fail closed and keep the
    dependency-manifest denial.
    """
    lines = document.splitlines(keepends=True)
    current_table: Optional[str] = None
    match_index: Optional[int] = None
    version_literal: Optional[str] = None
    canonical_line: Optional[str] = None

    for index, line in enumerate(lines):
        table = _TOML_TABLE_RE.fullmatch(line)
        if table:
            current_table = table.group("name").strip()
            continue
        if line.lstrip().startswith("["):
            current_table = None
            continue
        if current_table != "project":
            continue
        version = _PROJECT_VERSION_RE.fullmatch(line)
        if version is None:
            continue
        if match_index is not None:
            return None
        match_index = index
        version_literal = version.group("literal")
        canonical_line = (
            f"{version.group('prefix')}\"<oacp-project-version>\""
            f"{version.group('suffix')}{version.group('newline') or ''}"
        )

    if match_index is None or version_literal is None or canonical_line is None:
        return None
    lines[match_index] = canonical_line
    return "".join(lines), version_literal


def _preview_file_tool_change(
    tool_name: str,
    tool_input: Dict[str, Any],
    normalized_path: str,
) -> Optional[Tuple[str, str]]:
    """Reconstruct a file tool's pre/post documents without writing them."""
    try:
        before = Path(normalized_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

    if tool_name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            return None
        return before, content
    if tool_name != "Edit":
        return None

    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not old:
        return None
    occurrences = before.count(old)
    if occurrences == 0:
        return None
    replace_all = tool_input.get("replace_all") is True
    if not replace_all and occurrences != 1:
        return None
    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    return before, after


def _is_version_only_pyproject_edit(
    tool_name: str,
    tool_input: Dict[str, Any],
    normalized_path: str,
) -> bool:
    """True only when an Edit/Write changes one project-version literal."""
    if Path(normalized_path).name != "pyproject.toml":
        return False
    preview = _preview_file_tool_change(tool_name, tool_input, normalized_path)
    if preview is None:
        return False
    before, after = preview
    before_version = _canonicalize_project_version(before)
    after_version = _canonicalize_project_version(after)
    if before_version is None or after_version is None:
        return False
    before_canonical, before_literal = before_version
    after_canonical, after_literal = after_version
    return (
        before_literal != after_literal
        and before_canonical == after_canonical
    )


def _normalize_file_path(path: str, cwd: str) -> str:
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.normpath(expanded)


def _within(canonical: str, root: str) -> bool:
    return canonical == root or canonical.startswith(root + os.sep)


def is_bookkeeping_path(
    normalized: str, context: Optional[WorkspaceContext]
) -> bool:
    """True for paths that are enforcement instrumentation, not task scope.

    Covers the runtime scratchpad (reply/body-file composition) and the
    receiver's own workspace bookkeeping directories. Exempt paths never
    consume ``expected_files_touched``; the secret and dependency gates run
    before this predicate and still apply to them. Containment is checked on
    the resolved filesystem target (realpath on both sides), so a symlink
    planted under an exempt root that points into ordinary task scope stays
    counted.
    """
    canonical = os.path.realpath(normalized)
    if canonical.startswith(SCRATCHPAD_PREFIXES):
        return True
    if context is None:
        return False
    for prefix in context.bookkeeping_prefixes():
        if _within(canonical, os.path.realpath(prefix)):
            return True
    return False


# ── Repo resolution ───────────────────────────────────────────────────────────


def parse_repo_from_url(url: str) -> Optional[str]:
    match = GIT_URL_RE.search(url.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def resolve_cwd_repo(git_dir: str, remote: str = "origin") -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", git_dir, "remote", "get-url", remote],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_repo_from_url(completed.stdout)


def resolve_current_branch(git_dir: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", git_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _repo_gate(
    repo: Optional[str],
    constraints: Dict[str, Any],
    action_label: str,
) -> Optional[Decision]:
    """Shared allowlist/visibility gate for gh and git mutations."""
    if constraints.get("public_visibility"):
        return _deny(
            f"envelope declares public_visibility: true is not allowed at "
            f"runtime; {action_label} paused for review"
        )
    if repo is None:
        return _ask(
            f"cannot resolve the target repository for {action_label}; "
            "escalating for review"
        )
    target = str(constraints.get("target_repo") or "")
    allowlist = constraints.get("private_repo_allowlist") or []
    if target and repo != target:
        return _deny(
            f"{action_label} targets {repo}, but the envelope pins "
            f"target_repo {target}"
        )
    if repo not in allowlist:
        return _deny(
            f"{action_label} targets {repo}, which is outside the receiver's "
            "private_repo_allowlist"
        )
    return None


# ── Bash classification ───────────────────────────────────────────────────────


def _strip_wrappers(tokens: List[str]) -> Tuple[List[str], bool, List[str]]:
    """Return (remaining tokens, needs_ask, stripped env assignments).

    needs_ask is True when a wrapper carries its own flags (`env -i gh ...`):
    flag/value consumption differs per wrapper, so the real program cannot be
    identified reliably — escalate instead of guessing (F-002). The stripped
    assignments are preserved because they change the wrapped program's
    behavior (`OACP_HOME=… oacp envelope clear` retargets the clear) and the
    classifier must be able to see that.
    """
    index = 0
    saw_wrapper = False
    assignments: List[str] = []
    while index < len(tokens):
        token = tokens[index]
        if ENV_ASSIGNMENT_RE.match(token):
            assignments.append(token)
            index += 1
            continue
        if token in WRAPPER_COMMANDS:
            saw_wrapper = True
            index += 1
            continue
        if saw_wrapper and token.startswith("-"):
            return tokens[index:], True, assignments
        break
    return tokens[index:], False, assignments


def _flag_value(tokens: List[str], *flags: str) -> Optional[str]:
    for index, token in enumerate(tokens):
        if token in flags and index + 1 < len(tokens):
            return tokens[index + 1]
        for flag in flags:
            if token.startswith(flag + "="):
                return token.split("=", 1)[1]
            # Attached short-flag value: `-Rowner/repo` (F-006).
            if (
                not flag.startswith("--")
                and len(flag) == 2
                and len(token) > 2
                and token.startswith(flag)
            ):
                return token[2:]
    return None


GH_URL_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$",
    re.IGNORECASE,
)


def _gh_repo_selectors(
    tokens: List[str],
    positionals: List[str],
) -> Tuple[List[str], Optional[str]]:
    """Every explicit repository selector on a gh mutation command.

    gh lets a positional URL (`gh pr merge https://github.com/o/r/pull/7`)
    or a later `-R/--repo` occurrence retarget the mutation away from the
    first flag value and the cwd repo, so the repo gate must judge the same
    repository gh will actually mutate: every flag occurrence and every
    URL-shaped positional is collected, and the caller escalates unless
    they agree on a single repository. Returns (selectors, unresolved)
    where unresolved carries a URL-shaped positional whose repository
    could not be parsed (e.g. a non-github host).
    """
    selectors: List[str] = []
    unresolved: Optional[str] = None
    for index, token in enumerate(tokens):
        if token in ("-R", "--repo"):
            if index + 1 < len(tokens):
                selectors.append(tokens[index + 1])
            continue
        if token.startswith("--repo="):
            selectors.append(token.split("=", 1)[1])
            continue
        if token.startswith("-R") and not token.startswith("--") and len(token) > 2:
            selectors.append(token[2:])
            continue
    for word in positionals:
        url_shaped = "://" in word or GH_URL_REPO_RE.match(word)
        if not url_shaped:
            continue
        match = GH_URL_REPO_RE.match(word)
        if match:
            selectors.append(f"{match.group(1)}/{match.group(2)}")
        else:
            unresolved = word
    return selectors, unresolved


def _last_flag_value(tokens: List[str], flag: str) -> Optional[str]:
    """Last-occurrence flag value — argparse semantics.

    The completion-clear validation must judge the same effective value the
    CLI will use; first-occurrence parsing lets a duplicated flag validate
    one target and clear another.
    """
    value: Optional[str] = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == flag and index + 1 < len(tokens):
            value = tokens[index + 1]
            index += 2
            continue
        if token.startswith(flag + "="):
            value = token.split("=", 1)[1]
        index += 1
    return value


def _git_subcommand(tokens: List[str]) -> Tuple[Optional[str], List[str], Optional[str]]:
    """Return (subcommand, args-after-subcommand, -C directory)."""
    git_dir: Optional[str] = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-C" and index + 1 < len(tokens):
            git_dir = tokens[index + 1]
            index += 2
            continue
        if token == "-c" and index + 1 < len(tokens):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, tokens[index + 1:], git_dir
    return None, [], git_dir


def _classify_git_push(
    args: List[str],
    git_dir: str,
    constraints: Dict[str, Any],
) -> Decision:
    if not constraints.get("external_side_effects"):
        return _deny(
            "git push is an external side effect the envelope declares false"
        )
    if not constraints.get("creates_or_updates_pr"):
        return _deny(
            "git push requires creates_or_updates_pr in the envelope "
            "(branch pushes are folded into PR-artifact creation)"
        )

    bulk = sorted(set(args) & GIT_PUSH_BULK_FLAGS)
    if bulk:
        return _deny(
            f"git push {bulk[0]} can update refs beyond a single PR branch "
            "and is never inside a runtime envelope"
        )

    positional = [arg for arg in args if not arg.startswith("-")]
    remote = positional[0] if positional else "origin"
    refspecs = positional[1:]
    needs_current_branch = not refspecs
    for refspec in refspecs:
        # Wildcard refspecs can update many branches at once, including
        # protected ones (F-006: `refs/heads/*:refs/heads/*`).
        if "*" in refspec:
            return _deny(
                f"git push with wildcard refspec {refspec!r} can update "
                "multiple branches and is never inside a runtime envelope"
            )
        # `+refspec` is a per-refspec force marker — strip it before parsing
        # so `+main` is still recognized as a protected branch (F-003).
        destination = refspec.lstrip("+").split(":", 1)[-1]
        branch = destination.rsplit("/", 1)[-1]
        if branch == "HEAD":
            needs_current_branch = True
            continue
        if branch in PROTECTED_BRANCHES:
            return _deny(
                f"git push targeting protected branch {branch!r} is never "
                "inside a runtime envelope"
            )
    if needs_current_branch:
        branch = resolve_current_branch(git_dir)
        if branch is None:
            return _ask("cannot resolve the branch for a bare git push")
        if branch in PROTECTED_BRANCHES:
            return _deny(
                f"git push from protected branch {branch!r} is never inside "
                "a runtime envelope"
            )

    repo = resolve_cwd_repo(git_dir, remote)
    gate = _repo_gate(repo, constraints, "git push")
    if gate is not None:
        return gate
    return ALLOW


def _gh_positionals(tokens: List[str]) -> Tuple[List[str], bool]:
    """Extract gh positionals with flag/value pairs consumed (F-003).

    Returns (positionals, api_mutation_flags): value-taking flags skip their
    value so it can never be misread as group/action; `-f`/`--field`-style
    flags are recorded because they implicitly switch `gh api` to POST.
    """
    words: List[str] = []
    api_mutation = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in GH_API_MUTATION_FLAGS:
            api_mutation = True
            index += 2
            continue
        if token in GH_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("--"):
            if "=" in token and token.split("=", 1)[0] in GH_API_MUTATION_FLAGS:
                api_mutation = True
            index += 1
            continue
        if token.startswith("-") and len(token) > 2:
            # Attached short-flag value (`-Rowner/repo`, `-fkey=val`): the
            # whole token is flag+value — never a positional (F-006).
            if token[:2] in GH_API_MUTATION_FLAGS:
                api_mutation = True
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        words.append(token)
        index += 1
    return words, api_mutation


def _gh_env_repo_selector(
    env_assignments: List[str],
    ambient: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Repository selector carried by the effective GH_REPO / GH_HOST.

    `gh help environment`: GH_REPO selects the repository for commands
    that would otherwise use the local one, and GH_HOST retargets the
    hostname entirely — both retarget a mutation after the cwd-based
    gate approved it. The effective environment is what the Bash child
    inherits: ambient hook-process values seeded first, inline
    assignments applied over them with shell precedence. Returns
    (selector, escalate_reason); any effective GH_HOST, or a GH_REPO
    value that does not resolve to a github.com owner/repo, cannot be
    validated here and escalates.
    """
    if ambient is None:
        ambient = {
            key: os.environ[key]
            for key in ("GH_REPO", "GH_HOST")
            if key in os.environ
        }
    effective: Dict[str, str] = dict(ambient)
    origin = {key: "ambient" for key in effective}
    for assignment in env_assignments:
        name, _, value = assignment.partition("=")
        if name in ("GH_REPO", "GH_HOST"):
            effective[name] = value
            origin[name] = "inline"
    if "GH_HOST" in effective:
        return None, (
            f"an {origin['GH_HOST']} GH_HOST value "
            f"{effective['GH_HOST']!r}"
        )
    value = effective.get("GH_REPO")
    if value is None:
        return None, None
    # GH_REPO accepts [HOST/]OWNER/REPO.
    parts = value.split("/")
    if len(parts) == 2 and all(parts):
        return value, None
    if len(parts) == 3 and parts[0].lower() in ("github.com", "www.github.com"):
        return f"{parts[1]}/{parts[2]}", None
    return None, f"an {origin['GH_REPO']} GH_REPO value {value!r}"


def _classify_gh(
    tokens: List[str],
    cwd: str,
    constraints: Dict[str, Any],
    *,
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Decision:
    words, api_mutation = _gh_positionals(tokens)
    group = words[0] if words else ""
    action = words[1] if len(words) > 1 else ""
    label = f"gh {group} {action}".strip()

    if group == "api":
        explicit = _flag_value(tokens, "-X", "--method")
        method = (explicit or ("POST" if api_mutation else "GET")).upper()
        if method == "GET":
            return ALLOW
        return _ask(f"gh api with method {method} cannot be classified")

    if group == "auth":
        # Allowlist, not blocklist: `status` is the only read-only auth verb
        # (F-006 — `gh auth switch` mutates authentication configuration).
        if action == "status":
            return ALLOW
        if not constraints.get("touches_auth_config_or_secrets"):
            return _deny(
                f"{label} mutates auth state the envelope declares false"
            )
        return ALLOW

    if action in GH_READONLY_ACTIONS:
        return ALLOW

    key = (group, action)
    constraint_classes = (
        (GH_PR_CLASS, "creates_or_updates_pr"),
        (GH_COMMENT_CLASS, "comments_on_github"),
        (GH_MERGE_CLASS, "merges_pr"),
        (GH_ISSUE_CLASS, "files_issues"),
    )
    for class_keys, required in constraint_classes:
        if key not in class_keys:
            continue
        if not constraints.get("external_side_effects"):
            return _deny(
                f"{label} is an external side effect the envelope declares false"
            )
        if not constraints.get(required):
            return _deny(f"{label} requires {required} in the envelope")
        # The gate must judge the repository gh will actually mutate: a
        # positional URL, a repeated -R/--repo, a GH_REPO/GH_HOST
        # assignment, or shell state mutated by an earlier compound
        # segment (cd, export — including builtin-prefixed spellings this
        # classifier cannot enumerate) can all retarget the command after
        # the cwd-based gate approved it. Only a standalone simple command
        # is judged, and every selector it carries must agree.
        if not standalone:
            return _ask(
                f"{label} inside a compound command cannot be validated "
                "(earlier segments may retarget it); run it as a "
                "standalone command"
            )
        env_selector, env_escalate = _gh_env_repo_selector(env_assignments or [])
        if env_escalate is not None:
            return _ask(
                f"{label} carries {env_escalate}, which retargets gh away "
                "from the validated repository; escalating for review"
            )
        selectors, unresolved = _gh_repo_selectors(tokens, words[2:])
        if env_selector is not None:
            selectors.append(env_selector)
        if unresolved is not None:
            return _ask(
                f"{label} references {unresolved!r}, whose repository "
                "cannot be resolved; escalating for review"
            )
        distinct = sorted(set(selectors))
        if len(distinct) > 1:
            return _ask(
                f"{label} carries conflicting repository selectors "
                f"({', '.join(distinct)}); escalating for review"
            )
        repo = distinct[0] if distinct else resolve_cwd_repo(cwd)
        gate = _repo_gate(repo, constraints, label)
        if gate is not None:
            return gate
        return ALLOW

    if key in GH_DENY_CLASS or group in GH_DENY_GROUPS or key in GH_WORKFLOW_MUTATIONS:
        return _deny(
            f"{label} is outside every envelope allow class "
            "(PR create/update, comments, declared merges, and declared "
            "issue filing only)"
        )

    # Fail closed: gh verbs outside the known read-only and mutation classes
    # escalate rather than pass (F-003 — e.g. `gh run delete`).
    return _ask(f"cannot classify {label} under an active envelope")


def _classify_envelope_clear(
    tokens: List[str],
    context: Optional[WorkspaceContext],
    cwd: str,
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Decision:
    """Sanction the documented completion clear against the audit record.

    The clear is allowed exactly when the newest audit record whose CONTENT
    identity (``message_id`` + ``receiver``) matches the active envelope
    shows a terminal ``result.final_state``: the task lifecycle is over and
    the enforcement window may close from inside the session. ``pending``
    and ``paused`` keep the envelope active — a paused task re-authorizes
    via the §E checkpoint, never via clear.

    The validation judges the same effective target the CLI will use: the
    clear must be a standalone simple command (compound commands escalate —
    an earlier ``export`` or ``cd`` segment could retarget it), flag values
    use last-occurrence (argparse) semantics, relative ``--oacp-dir``
    resolves against the call's cwd, and an ``OACP_HOME=`` assignment on the
    command escalates outright (it retargets the CLI's home resolution). A
    clear aimed at a different project/receiver/home than the active
    envelope, or an unreadable audit record, cannot be validated here and
    escalates. Filenames are never trusted and sender data never reaches a
    filesystem glob.
    """
    if context is None or not context.message_id:
        return _deny(
            "oacp envelope clear cannot be validated without a resolved "
            "workspace; the completion clear is sanctioned by the task's "
            "audit record"
        )
    if not standalone:
        # Earlier segments of a compound command can mutate cwd or the
        # environment (`export OACP_HOME=…;`, `cd …;`) and retarget the
        # clear after this validation runs — only a standalone simple
        # command is judged, everything else escalates.
        return _ask(
            "oacp envelope clear inside a compound command cannot be "
            "validated (earlier segments may retarget it); run the clear "
            "as a standalone command"
        )
    for assignment in env_assignments or []:
        if assignment.startswith("OACP_HOME="):
            return _ask(
                "OACP_HOME override on an envelope clear retargets the CLI's "
                "home resolution and cannot be validated in-session; "
                "escalating for review"
            )
    target_project = _last_flag_value(tokens, "--project") or context.project
    target_receiver = _last_flag_value(tokens, "--receiver") or "claude"
    if target_project != context.project or target_receiver != context.receiver:
        return _ask(
            f"oacp envelope clear targets {target_project}/{target_receiver}, "
            f"not the active envelope ({context.project}/{context.receiver}); "
            "escalating for review"
        )
    oacp_dir = _last_flag_value(tokens, "--oacp-dir")
    if oacp_dir is not None:
        declared = os.path.realpath(_normalize_file_path(oacp_dir, cwd))
        if declared != os.path.realpath(str(context.oacp_root)):
            return _ask(
                "oacp envelope clear targets a different OACP home than the "
                "active envelope; escalating for review"
            )
    audit_dir = context.audit_dir()
    candidates = sorted(audit_dir.glob("*.yaml")) if audit_dir.is_dir() else []
    matches: List[Tuple[str, Dict[str, Any]]] = []
    for candidate in candidates:
        try:
            record = load_yaml_file(candidate)
        except Exception as exc:
            # Any unreadable record could be the authoritative newest one —
            # fail closed on the whole directory rather than guess.
            return _ask(
                f"unreadable audit record {candidate.name!r} while "
                f"validating envelope clear: {exc}"
            )
        if (
            str(record.get("message_id") or "") == context.message_id
            and str(record.get("receiver") or "") == context.receiver
        ):
            matches.append((candidate.name, record))
    if not matches:
        return _deny(
            "oacp envelope clear before any audit record matches "
            f"message_id {context.message_id!r} and receiver "
            f"{context.receiver!r}; the completion clear is sanctioned once "
            "the record's result.final_state is terminal (done/error)"
        )
    record = max(matches, key=lambda item: item[0])[1]
    result = record.get("result")
    state = result.get("final_state") if isinstance(result, dict) else None
    if state in TERMINAL_AUDIT_STATES:
        return ALLOW
    return _deny(
        f"oacp envelope clear while the audit record for "
        f"{context.message_id} shows result.final_state {state!r}; the "
        "envelope stays active until the task completes (done/error) — a "
        "paused task re-authorizes via the §E checkpoint, and completion "
        "requires the audit record update first"
    )


def _classify_oacp(
    tokens: List[str],
    context: Optional[WorkspaceContext] = None,
    cwd: str = "",
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Decision:
    """Scope the oacp exemption to what the protocol actually promises.

    Only `oacp send` (the §E notification pipe) plus read-only surfaces are
    exempt. `oacp envelope compile` from inside an enveloped session is
    self-modification of the active constraints (F-001); `oacp envelope
    clear` is the documented completion step and is sanctioned exactly when
    the task's audit record shows a terminal outcome; other mutating
    subcommands escalate.
    """
    words = [token for token in tokens[1:] if not token.startswith("-")]
    sub = words[0] if words else ""
    action = words[1] if len(words) > 1 else ""

    if sub in OACP_ALWAYS_ALLOWED:
        return ALLOW
    if sub == "envelope":
        if action == "show":
            return ALLOW
        if action == "clear":
            return _classify_envelope_clear(
                tokens, context, cwd, env_assignments, standalone
            )
        return _deny(
            f"oacp envelope {action} modifies the active envelope from inside "
            "the enveloped session; re-authorization goes through the §E "
            "checkpoint, not self-service recompilation"
        )
    if sub == "agent" and action in ("show", "list"):
        return ALLOW
    return _ask(
        f"cannot classify oacp {sub} under an active envelope; only send and "
        "read-only subcommands are exempt"
    )


def _classify_dependency_command(
    prog: str,
    tokens: List[str],
    constraints: Dict[str, Any],
) -> Optional[Decision]:
    if prog.startswith("python"):
        if "-m" in tokens:
            module_index = tokens.index("-m") + 1
            if module_index < len(tokens) and tokens[module_index] in ("pip", "pip3"):
                prog = "pip"
                tokens = tokens[module_index:]
            else:
                return None
        else:
            return None
    if prog not in PACKAGE_MANAGERS:
        return None
    verbs = [token for token in tokens[1:] if not token.startswith("-")]
    # `uv pip install`, `uv tool install`: scan the first two positionals.
    for verb in verbs[:2]:
        if verb in DEPENDENCY_VERBS:
            if constraints.get("touches_dependencies"):
                return ALLOW
            return _deny(
                f"{prog} {verb} changes dependencies the envelope declares false"
            )
    return None


def _mutator_operands(tokens: List[str]) -> List[str]:
    """Effective path operands of a filesystem-mutator invocation.

    Judged the way the utility parses argv, not by dash-prefix alone: `--`
    ends option processing (everything after is an operand even if it starts
    with a dash), and the GNU target-directory spellings (`-t DIR`, `-tDIR`,
    `--target-directory DIR`, `--target-directory=DIR`) contribute their
    directory value. Capturing `-t` generically across the mutator set is a
    conservative superset — a value misread from a tool that lacks the flag
    only adds a checked operand, never removes one.
    """
    operands: List[str] = []
    options_ended = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if options_ended or not token.startswith("-"):
            operands.append(token)
            index += 1
            continue
        if token == "--":
            options_ended = True
            index += 1
            continue
        if token in ("-t", "--target-directory") and index + 1 < len(tokens):
            operands.append(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--target-directory="):
            operands.append(token.split("=", 1)[1])
            index += 1
            continue
        if token.startswith("-t") and not token.startswith("--") and len(token) > 2:
            operands.append(token[2:])
            index += 1
            continue
        index += 1
    return operands


def _segment_write_targets(tokens: List[str], segment: str) -> List[str]:
    """Determinable file-write targets of one shell segment (F-004).

    Covers redirects plus common writer programs whose destinations are
    parseable from argv. Read-only or unrecognized programs contribute no
    targets — the secret/dependency/counter gate then simply does not fire.
    """
    targets = [
        match.group(1).strip("'\"")
        for match in REDIRECT_TARGET_RE.finditer(segment)
    ]
    if not tokens:
        return targets
    prog = tokens[0].rsplit("/", 1)[-1]
    positional = [token for token in tokens[1:] if not token.startswith("-")]
    if prog in ("touch", "tee"):
        targets.extend(positional)
    elif prog in ("cp", "mv", "install") and len(positional) >= 2:
        targets.append(positional[-1])
    elif prog == "truncate":
        targets.extend(positional)
    elif prog == "sed" and any(token.startswith("-i") for token in tokens[1:]):
        # First positional is the script; the rest are edited in place.
        targets.extend(positional[1:])
    elif prog == "dd":
        targets.extend(
            token[len("of="):] for token in tokens[1:] if token.startswith("of=")
        )
    return targets


def _gate_write_paths(
    paths: List[str],
    cwd: str,
    constraints: Dict[str, Any],
    counters: Dict[str, Any],
    verb: str,
    context: Optional[WorkspaceContext] = None,
    dependency_exemption: Optional[Callable[[str], bool]] = None,
) -> Decision:
    """Shared secret/dependency/file-counter gate for file tools and Bash
    writes. Counts distinct paths cumulatively so a single call cannot jump
    the `expected_files_touched` ceiling (F-004: `touch a b c`). Bookkeeping
    surfaces pass the secret/dependency gates but never reach the counter:
    completion instrumentation must not compete with the task's declared
    file budget."""
    touched = list(counters.get("files_touched") or [])
    expected = int(constraints.get("expected_files_touched") or 0)
    new_files: List[str] = []
    for path in paths:
        normalized = _normalize_file_path(path, cwd)
        if normalized.startswith("/dev/"):
            continue
        if not constraints.get("touches_auth_config_or_secrets") and is_secret_path(
            normalized
        ):
            return _deny(
                f"{verb} of secret-class path {normalized!r} is outside the "
                "envelope (touches_auth_config_or_secrets: false)"
            )
        if context is not None:
            canonical = os.path.realpath(normalized)
            if _within(canonical, os.path.realpath(str(context.state_dir()))):
                return _deny(
                    f"{verb} of envelope state {normalized!r} from inside the "
                    "enveloped session is envelope self-modification; the "
                    "active envelope is never written directly"
                )
            if not constraints.get("touches_auth_config_or_secrets"):
                for root in context.trust_roots():
                    if _within(canonical, os.path.realpath(str(root))):
                        return _deny(
                            f"{verb} of trust root {normalized!r} is outside "
                            "the envelope (authority-bearing auth config; "
                            "touches_auth_config_or_secrets: false)"
                        )
        if (
            not constraints.get("touches_dependencies")
            and is_dependency_path(normalized)
            and not (
                dependency_exemption is not None
                and dependency_exemption(normalized)
            )
        ):
            return _deny(
                f"{verb} of dependency manifest {normalized!r} is outside the "
                "envelope (touches_dependencies: false)"
            )
        if is_bookkeeping_path(normalized, context):
            continue
        if normalized in touched or normalized in new_files:
            continue
        if len(touched) + len(new_files) >= expected:
            return _deny(
                f"{BLOCKED_OPENER} — files_touched expected {expected}, "
                f"now {len(touched) + len(new_files) + 1}"
            )
        new_files.append(normalized)
    if new_files:
        return Decision("allow", new_files=new_files)
    return ALLOW


def _split_shell_segments(command: str) -> List[str]:
    """Split on shell operators outside quotes and substitutions.

    This is deliberately a small classifier scanner, not a shell parser. It
    preserves raw segment text for the existing redirect checks, joins shell
    line continuations, and rejects unterminated quoting/substitution instead
    of guessing. Separator characters inside quoted arguments stay data.
    """
    segments: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    in_backticks = False
    substitution_depth = 0
    index = 0

    def flush() -> None:
        segment = "".join(current).strip()
        if segment:
            segments.append(segment)
        current.clear()

    while index < len(command):
        char = command[index]
        next_char = command[index + 1] if index + 1 < len(command) else ""

        if char == "\\" and quote != "'":
            if next_char == "\n":
                index += 2
                continue
            current.append(char)
            if next_char:
                current.append(next_char)
                index += 2
            else:
                index += 1
            continue

        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue

        if in_backticks:
            current.append(char)
            if char == "`":
                in_backticks = False
            index += 1
            continue

        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "`":
            in_backticks = True
            current.append(char)
            index += 1
            continue
        if char == "$" and next_char == "(":
            substitution_depth += 1
            current.extend((char, next_char))
            index += 2
            continue
        if substitution_depth:
            if char == "(":
                substitution_depth += 1
            elif char == ")":
                substitution_depth -= 1
            current.append(char)
            index += 1
            continue

        if char in (";", "\n", "|"):
            flush()
            index += 1
            if char == "|" and index < len(command) and command[index] in ("|", "&"):
                index += 1
            continue
        if char == "&":
            # `2>&1`, `<&0`, and `&>file` are redirections, not command
            # boundaries. Their targets remain visible to the redirect gate.
            if (current and current[-1] in ("<", ">")) or next_char == ">":
                current.append(char)
                index += 1
                continue
            flush()
            index += 1
            if index < len(command) and command[index] == "&":
                index += 1
            continue

        current.append(char)
        index += 1

    if quote is not None or in_backticks or substitution_depth:
        raise ValueError("unterminated shell quote or command substitution")
    flush()
    return segments


def _segments_of(command: str) -> List[str]:
    """Return top-level and command-substitution segments (F-002)."""
    segments = _split_shell_segments(command)
    substitutions = list(SUBSTITUTION_RE.finditer(command))
    simple_dollar_substitutions = sum(
        match.group(1) is not None for match in substitutions
    )
    if command.count("$(") != simple_dollar_substitutions:
        # The deliberately small extraction regex cannot prove the contents
        # of nested or parenthesized substitutions. Escalate the whole call
        # instead of letting an uninspected inner mutation hide under a
        # read-only outer command.
        raise ValueError("nested or parenthesized command substitution")
    for match in substitutions:
        inner = match.group(1) or match.group(2) or ""
        segments.extend(_split_shell_segments(inner))
    return segments


DEGRADED_READONLY_COMMANDS = {
    "cat",
    "cmp",
    "diff",
    "grep",
    "head",
    "jq",
    "ls",
    "pwd",
    "rg",
    "stat",
    "tail",
    "test",
    "wc",
}


def _degraded_readonly_segment(segment: str) -> bool:
    """Conservatively recognize a direct read-only head after argv failure.

    No shell control, redirect, expansion, or substitution syntax is allowed
    on this fallback. Ripgrep's command-executing ``--pre`` mode is excluded.
    The quote-aware scanner has already rejected unbalanced shell syntax.
    """
    if re.search(r"[;|&<>()`$\n]", segment) or EXPANSION_SYNTAX_RE.search(segment):
        return False
    words = segment.strip().split()
    while words and ENV_ASSIGNMENT_RE.match(words[0]):
        words.pop(0)
    if words and words[0] in ("command", "builtin"):
        words.pop(0)
    if not words or words[0].startswith("-"):
        return False
    program = Path(words[0]).name
    if program == "rg" and any(
        word == "--pre" or word.startswith("--pre=") for word in words[1:]
    ):
        return False
    return program in DEGRADED_READONLY_COMMANDS


def _classify_segment(
    tokens: List[str],
    segment: str,
    cwd: str,
    constraints: Dict[str, Any],
    write_targets: List[str],
    context: Optional[WorkspaceContext] = None,
    depth: int = 0,
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Optional[Decision]:
    """Classify one segment's tokens; return a non-allow Decision or None.

    Write targets are collected BEFORE program dispatch so a redirect on a
    recognized program (`gh pr view > .env`) still reaches the shared write
    gate (F-005). Known runners recurse into their nested command (F-007).
    Env assignments stripped from the segment (or inherited from an outer
    runner) accumulate so target-sensitive classification can inspect them.
    """
    if depth > 3:
        return _ask(f"cannot classify deeply nested command: {segment!r}")

    tokens, wrapper_needs_ask, stripped = _strip_wrappers(tokens)
    env_assignments = list(env_assignments or []) + stripped
    if wrapper_needs_ask:
        return _ask(f"cannot classify wrapper invocation: {segment!r}")
    if not tokens:
        return None

    # Redirect targets are a property of the segment, writer-program targets
    # of the argv — both must be gated regardless of which branch handles
    # the program below (only collect segment redirects once, at depth 0).
    write_targets.extend(
        _segment_write_targets(tokens, segment if depth == 0 else "")
    )

    prog = tokens[0].rsplit("/", 1)[-1]

    if prog == "sudo":
        return _ask(f"cannot classify privileged command: {segment!r}")
    if prog in SHELL_INDIRECTION:
        return _ask(f"cannot classify shell indirection: {segment!r}")

    code_flags = INTERPRETER_CODE_FLAGS.get(prog)
    if code_flags and any(token in code_flags for token in tokens[1:]):
        return _ask(f"cannot classify inline interpreter code: {segment!r}")

    runner_verb = RUNNER_VERBS.get(prog)
    nested: Optional[List[str]] = None
    if prog in DIRECT_RUNNERS:
        nested = tokens[1:]
    elif runner_verb is not None and len(tokens) > 1 and tokens[1] == runner_verb:
        nested = tokens[2:]
    if nested is not None:
        if not nested or nested[0].startswith("-"):
            return _ask(f"cannot classify runner invocation: {segment!r}")
        return _classify_segment(
            nested,
            segment,
            cwd,
            constraints,
            write_targets,
            context,
            depth + 1,
            env_assignments,
            standalone,
        )

    if prog == "oacp":
        decision = _classify_oacp(
            tokens, context, cwd, env_assignments, standalone
        )
        if decision.action != "allow":
            return decision
        return None
    if prog in ("cp", "mv", "install") and not constraints.get(
        "touches_auth_config_or_secrets"
    ):
        # Sources too: copying a secret out is as bad as writing one.
        for arg in tokens[1:]:
            if not arg.startswith("-") and is_secret_path(arg):
                return _deny(
                    f"command touching secret-class path {arg!r} is outside "
                    "the envelope (touches_auth_config_or_secrets: false)"
                )
    if prog in FS_MUTATORS and context is not None:
        # Operand gate, role-agnostic: deleting, relocating, copying out, or
        # aliasing envelope state is self-modification just as much as
        # writing it, and the same for trust-root material under the auth
        # gate — the write-target gate alone sees only destinations. The
        # operands judged must be the ones the utility will actually use:
        # GNU target-directory spellings are parsed, `--` ends option
        # processing, and expansion syntax escalates (the shell expands it
        # after classification, so its literal spelling proves nothing).
        state_root = os.path.realpath(str(context.state_dir()))
        trust_roots = [os.path.realpath(str(r)) for r in context.trust_roots()]
        for operand in _mutator_operands(tokens):
            if EXPANSION_SYNTAX_RE.search(operand) or "$" in operand:
                return _ask(
                    f"{prog} operand {operand!r} contains shell expansion or "
                    "variable syntax that cannot be statically resolved; use "
                    "explicit literal paths"
                )
            canonical = os.path.realpath(_normalize_file_path(operand, cwd))
            if _within(canonical, state_root):
                return _deny(
                    f"{prog} touching envelope state {operand!r} from inside "
                    "the enveloped session is envelope self-modification"
                )
            if not constraints.get("touches_auth_config_or_secrets"):
                for root in trust_roots:
                    if _within(canonical, root):
                        return _deny(
                            f"{prog} touching trust root {operand!r} is "
                            "outside the envelope (authority-bearing auth "
                            "config; touches_auth_config_or_secrets: false)"
                        )
    if prog == "find" and any(
        token in ("-exec", "-execdir", "-ok", "-okdir", "-delete")
        for token in tokens[1:]
    ):
        return _ask(f"cannot classify find with action predicates: {segment!r}")
    if prog == "git":
        subcommand, args, git_dir = _git_subcommand(tokens)
        base_dir = git_dir or cwd
        if not os.path.isabs(base_dir):
            base_dir = os.path.normpath(os.path.join(cwd, base_dir))
        if subcommand == "push":
            decision = _classify_git_push(args, base_dir, constraints)
            if decision.action != "allow":
                return decision
        elif subcommand == "commit":
            if not constraints.get("commits_changes"):
                return _deny(
                    "git commit is outside the envelope "
                    "(commits_changes: false)"
                )
        return None
    if prog == "gh":
        decision = _classify_gh(
            tokens,
            cwd,
            constraints,
            env_assignments=env_assignments,
            standalone=standalone,
        )
        if decision.action != "allow":
            return decision
        return None

    dependency = _classify_dependency_command(prog, tokens, constraints)
    if dependency is not None and dependency.action != "allow":
        return dependency
    return None


def classify_bash(
    command: str,
    cwd: str,
    constraints: Dict[str, Any],
    counters: Dict[str, Any],
    context: Optional[WorkspaceContext] = None,
) -> Decision:
    if not constraints.get("destructive_ops"):
        for label, pattern in DESTRUCTIVE_PATTERNS:
            if pattern.search(command):
                return _deny(
                    f"destructive command token {label!r} is outside the "
                    "envelope (destructive_ops: false)"
                )

    write_targets: List[str] = []
    try:
        segments = _segments_of(command)
    except ValueError:
        return _ask(f"cannot segment shell command: {command!r}")
    # A command with exactly one segment has no earlier shell state (cd,
    # export) that could retarget a target-sensitive subcommand after
    # validation — the completion clear is sanctioned only in that form.
    standalone = len(segments) == 1
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            if _degraded_readonly_segment(segment):
                continue
            return _ask(f"cannot classify shell segment: {segment!r}")
        decision = _classify_segment(
            tokens,
            segment,
            cwd,
            constraints,
            write_targets,
            context,
            standalone=standalone,
        )
        if decision is not None:
            return decision

    for target in write_targets:
        # Bash-derived targets are shell-expanded after classification —
        # a pattern can reach a path its literal spelling does not. File-tool
        # paths (Edit/Write) never pass here and may contain literal brackets.
        if EXPANSION_SYNTAX_RE.search(target):
            return _ask(
                f"write target {target!r} contains shell expansion syntax "
                "that cannot be statically resolved; use explicit literal "
                "paths"
            )
    return _gate_write_paths(
        write_targets, cwd, constraints, counters, "write", context
    )


# ── File-tool classification ──────────────────────────────────────────────────


def classify_file_write(
    file_path: str,
    cwd: str,
    constraints: Dict[str, Any],
    counters: Dict[str, Any],
    context: Optional[WorkspaceContext] = None,
    tool_name: str = "",
    tool_input: Optional[Dict[str, Any]] = None,
) -> Decision:
    dependency_exemption: Optional[Callable[[str], bool]] = None
    if tool_input is not None and tool_name in ("Edit", "Write"):
        def version_exemption(normalized: str) -> bool:
            return _is_version_only_pyproject_edit(
                tool_name, tool_input, normalized
            )

        dependency_exemption = version_exemption
    return _gate_write_paths(
        [file_path],
        cwd,
        constraints,
        counters,
        "edit",
        context,
        dependency_exemption,
    )


# ── Dispatch + I/O ────────────────────────────────────────────────────────────


def classify(
    tool_name: str,
    tool_input: Dict[str, Any],
    cwd: str,
    envelope: Dict[str, Any],
    context: Optional[WorkspaceContext] = None,
) -> Decision:
    constraints = envelope.get("constraints") or {}
    counters = envelope.get("counters") or {}

    if tool_name == "Bash":
        return classify_bash(
            str(tool_input.get("command") or ""), cwd, constraints, counters, context
        )
    if tool_name in ("Edit", "Write"):
        file_path = str(tool_input.get("file_path") or "")
        if not file_path:
            return ALLOW
        return classify_file_write(
            file_path,
            cwd,
            constraints,
            counters,
            context,
            tool_name,
            tool_input,
        )
    if tool_name == "NotebookEdit":
        notebook = str(tool_input.get("notebook_path") or "")
        if not notebook:
            return ALLOW
        return classify_file_write(notebook, cwd, constraints, counters, context)
    return ALLOW


def emit(decision: Decision) -> None:
    if decision.action == "allow":
        # Silence keeps the harness's own permission flow authoritative; the
        # envelope must never widen it.
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision.action,
                    "permissionDecisionReason": decision.reason,
                }
            }
        )
    )


def _compile_message_name(payload: Dict[str, Any]) -> Optional[str]:
    """Return the message filename when this tool call runs an envelope compile.

    Recognizes ``oacp envelope compile <message> ...`` (and the script-path
    spelling ``... envelope_compiler.py compile <message> ...``) anywhere in a
    Bash command. Best-effort by design: a miss degrades to an unbound
    envelope, never to a wrong binding — the compiler still checks the
    claimed filename against the message it actually compiles.
    """
    if str(payload.get("tool_name") or "") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if "compile" not in command:
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    # Compile options that consume a value: their value token must never be
    # mistaken for the positional message (mirrors the compiler's argparse
    # surface — keep in sync with envelope_compiler.py's compile subparser).
    value_options = {"--receiver", "--project", "--oacp-dir", "--config", "--audit"}
    for index, token in enumerate(tokens[:-1]):
        # Front ends for the same compile operation: the `oacp` executable
        # (any path spelling), the module CLI (`python3 -m oacp.cli`), and
        # the compiler script invoked directly.
        is_cli = (
            token == "envelope"
            and index > 0
            and tokens[index - 1].endswith(("oacp", "oacp.cli"))
        )
        is_script = token.endswith("envelope_compiler.py")
        if not (is_cli or is_script):
            continue
        rest = tokens[index + 1 :]
        if not rest or rest[0] != "compile":
            continue
        skip_next = False
        for operand in rest[1:]:
            if skip_next:
                skip_next = False
                continue
            if operand.startswith("-"):
                if operand in value_options:
                    skip_next = True
                continue
            return Path(operand).name
    return None


def _shared_state_affinity(
    payload: Dict[str, Any], state_root: str, cwd: str
) -> str:
    """Classify a foreign-session tool call's relation to shared envelope state.

    Returns ``"affine"`` when the call names the shared envelope state that
    protects the bound session (an ``oacp envelope`` invocation, a protected
    state filename anywhere in the command, or an operand resolving into the
    receiver's ``state/`` directory — through any symlinked spelling),
    ``"uncertain"`` when the target cannot be determined statically
    (unresolved shell expansion, substitution, unparseable input), and
    ``"clear"`` for ordinary foreign work. Callers deny mutation-capable
    affine calls outright — the classifier itself allows some mutation
    spellings (e.g. plain ``rm`` with a ``$VAR`` operand), so falling
    through is not a safe backstop — but keep pre-envelope behavior for
    provably read-only inspection (``_readonly_state_inspection``), keep
    uncertain calls enforced without budget accounting, and bypass only
    clear ones.
    """
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    # Compare against the canonical spelling of the state root: OACP homes
    # reached through a symlink otherwise never match a canonicalized
    # candidate, and the guard would wave the real path through.
    state_root = os.path.realpath(state_root)
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        file_path = str(
            tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        )
        if not file_path:
            return "uncertain"
        canonical = os.path.realpath(_normalize_file_path(file_path, cwd))
        return "affine" if _within(canonical, state_root) else "clear"
    if tool_name != "Bash":
        return "clear"
    command = str(tool_input.get("command") or "")
    lowered = command.lower()
    if "envelope" in lowered and (
        "oacp" in lowered or "envelope_compiler" in lowered or "oacp-envelope" in lowered
    ):
        return "affine"
    # Protected state filenames anywhere in the raw command — catches bare
    # names after a `cd`, operands inside substitutions, and mv/rm targets.
    if ENVELOPE_FILENAME in command or SESSION_CLAIM_STEM in command:
        return "affine"
    if state_root in command:
        return "affine"
    parse_failed = False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
        parse_failed = True
    for token in tokens:
        # An env-assignment token can smuggle the path in its value part.
        value = token.split("=", 1)[1] if ENV_ASSIGNMENT_RE.match(token) else token
        candidate = os.path.realpath(_normalize_file_path(value, cwd))
        if _within(candidate, state_root):
            return "affine"
    if (
        parse_failed
        or "$" in command
        or "`" in command
        or EXPANSION_SYNTAX_RE.search(command)
    ):
        return "uncertain"
    return "clear"


# Commands that cannot mutate or execute anything when output redirection,
# process substitution, and shell expansion are all absent (each is ruled
# out before this set is consulted). Membership requires the tool to have
# no write mode AND no command-execution flag in any common implementation:
# `sed` (-i), `sort` (-o), `find` (-delete/-exec), `tee`, `rg` (--pre),
# `less`/`more` (shell escapes, `less -o`), and `file` (-C writes a magic
# cache) all stay out.
READONLY_INSPECTION_COMMANDS = {
    "cat",
    "cd",
    "cmp",
    "diff",
    "echo",
    "grep",
    "head",
    "jq",
    "ls",
    "pwd",
    "stat",
    "tail",
    "test",
    "wc",
}


def _readonly_state_inspection(payload: Dict[str, Any]) -> bool:
    """True when an affine Bash call is provably read-only inspection.

    State relation is not the same as mutation capability: a foreign
    session reading shared state (``oacp envelope show``, ``cat``/``stat``
    on state files) cannot disturb the bound session, so it keeps
    pre-envelope behavior. Conservative by construction — any parse
    failure, unresolved expansion, redirection, process substitution, or
    command outside a small inspection allowlist means the affine deny
    stands: the exemption must prove the complete command cannot spawn,
    embed, or execute a mutation.
    """
    if str(payload.get("tool_name") or "") != "Bash":
        return False
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        return False
    if "$" in command or "`" in command or ">" in command:
        return False
    # Process substitution executes its body: `cat <(rm state)` mutates
    # under a read-only head. (`>(...)` is already caught by the `>` check.)
    if "<(" in command:
        return False
    if EXPANSION_SYNTAX_RE.search(command):
        return False
    try:
        segments = _segments_of(command)
    except ValueError:
        return False
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        while tokens and ENV_ASSIGNMENT_RE.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            return False
        if Path(tokens[0]).name in READONLY_INSPECTION_COMMANDS:
            continue
        if _is_envelope_show_invocation(tokens):
            continue
        return False
    return True


# `envelope show` takes exactly these value-carrying options (keep in sync
# with envelope_compiler.py's common parser). Anything else denies, so a
# grammar change fails safe.
SHOW_VALUE_OPTIONS = {"--receiver", "--project", "--oacp-dir"}
SHOW_FLAG_OPTIONS = {"-h", "--help"}


def _is_envelope_show_invocation(tokens: List[str]) -> bool:
    """Anchored recognition of the read-only ``envelope show`` form.

    The front end must be the command head — a subsequence scan would let
    trailing spoof tokens bless a different leading command (`rm state
    oacp envelope show`) — and the remaining argv must fit show's option
    grammar exactly.
    """
    head = Path(tokens[0]).name
    if head == "oacp":
        if tokens[1:3] != ["envelope", "show"]:
            return False
        rest = tokens[3:]
    elif head.startswith("python"):
        if len(tokens) >= 3 and tokens[1] == "-m":
            # Module names are exact — an endswith match would bless
            # look-alike modules.
            if tokens[2] not in ("oacp.cli", "oacp"):
                return False
            if tokens[3:5] != ["envelope", "show"]:
                return False
            rest = tokens[5:]
        elif len(tokens) >= 2 and tokens[1].endswith("envelope_compiler.py"):
            if tokens[2:3] != ["show"]:
                return False
            rest = tokens[3:]
        else:
            return False
    elif tokens[0].endswith("envelope_compiler.py"):
        if tokens[1:2] != ["show"]:
            return False
        rest = tokens[2:]
    else:
        return False
    expect_value = False
    for token in rest:
        if expect_value:
            expect_value = False
            continue
        if token in SHOW_FLAG_OPTIONS:
            continue
        option, separator, _value = token.partition("=")
        matches = [
            candidate
            for candidate in SHOW_VALUE_OPTIONS
            if candidate.startswith(option)
        ]
        if len(matches) != 1:
            return False
        if not separator:
            expect_value = True
    return not expect_value


def process(payload: Dict[str, Any], receiver: str = "claude") -> Decision:
    from _oacp_env import resolve_oacp_home

    cwd = str(payload.get("cwd") or os.getcwd())
    session_id = str(payload.get("session_id") or "")
    project = find_project(Path(cwd))
    if project is None:
        return ALLOW
    oacp_root = resolve_oacp_home(cwd=Path(cwd))
    target = envelope_path(oacp_root, project, receiver)

    from envelope_compiler import envelope_lock

    if not target.is_file():
        # No active envelope: if this very call is about to compile one,
        # record which session it belongs to. Only the hook sees the harness
        # session id (it is absent from the Bash environment), so this
        # observation is the binding's sole source. Best-effort: a failed
        # claim write must never affect the call's outcome.
        if session_id:
            message_name = _compile_message_name(payload)
            if message_name:
                try:
                    with envelope_lock(target):
                        if not target.is_file():
                            write_session_claim(target, session_id, message_name)
                except OSError:
                    pass
        return ALLOW

    with envelope_lock(target):
        envelope = load_envelope(target)
        if envelope is None:
            return ALLOW
        bound_session = str(envelope.get("session_id") or "")
        foreign_session = bool(
            bound_session and session_id and bound_session != session_id
        )
        if foreign_session:
            # Session-bound envelope, different session: pre-envelope
            # behavior — no classification, no files_touched accounting.
            # An unbound envelope (legacy compile, or a caller the harness
            # gave no session id) keeps the historical (project, agent)
            # scope: enforcement never silently narrows.
            affinity = _shared_state_affinity(payload, str(target.parent), cwd)
            if affinity == "affine":
                # Pure inspection of shared state cannot disturb the bound
                # session — only mutation capability loses the bypass.
                if _readonly_state_inspection(payload):
                    return ALLOW
                # A foreign session has no legitimate mutation of the state
                # protecting the bound session — deny outright rather than
                # trusting classification, which allows some mutation
                # spellings (e.g. plain rm with a $VAR operand).
                return _with_message_id(
                    _deny(
                        "this call can modify envelope state bound to another "
                        "session; a concurrent session must not compile over, "
                        "clear, or edit the active envelope (read-only "
                        "inspection is exempt)"
                    ),
                    str(envelope.get("message_id") or ""),
                )
            if affinity == "clear":
                return ALLOW
            # Uncertain target (unresolved expansion / unparseable): stay
            # enforced through full classification, but never charge the
            # bound session's files_touched budget for foreign work.
        context = WorkspaceContext(
            oacp_root=oacp_root,
            project=str(envelope.get("project") or project),
            receiver=str(envelope.get("receiver") or receiver),
            message_id=str(envelope.get("message_id") or ""),
        )
        decision = classify(
            str(payload.get("tool_name") or ""),
            payload.get("tool_input") or {},
            cwd,
            envelope,
            context,
        )
        if decision.action == "allow" and decision.new_files and not foreign_session:
            counters = envelope.setdefault("counters", {})
            touched = list(counters.get("files_touched") or [])
            counters["files_touched"] = sorted(set(touched) | set(decision.new_files))
            write_envelope(target, envelope)
    return _with_message_id(decision, context.message_id)


def main(argv: Optional[Sequence[str]] = None) -> int:
    receiver = os.environ.get("OACP_ENVELOPE_RECEIVER", "claude")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        decision = process(payload, receiver=receiver)
    except Exception as exc:  # fail closed in the interior: escalate, never allow
        decision = _ask(f"envelope hook could not evaluate this call: {exc}")
    emit(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
