#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Compile a task_profile into a runtime envelope.

Turns an admitted inbox message's declared ``task_profile`` plus the
receiver's autonomy config into ``active_envelope.json`` under the agent's
workspace ``state/`` directory. The Claude PreToolUse hook shim
(``claude_envelope_hook.py``) reads that file on every tool call and enforces
the declared constraints at the action layer.

The compiler deliberately imports the autonomy gate's normalization and
pattern constants so the admission spec and the runtime enforcement cannot
drift: they are the same code.

Compile failures are fail-closed: the receiver must pause the task with
reason code ``envelope_compile_error`` instead of executing unenforced.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence

from _oacp_constants import SPEC_VERSION, utc_now_iso
from autonomy_gate import (
    AutonomyConfigError,
    TaskProfileError,
    extract_task_profile,
    load_yaml_file,
    message_sha256,
    normalize_scope_envelope,
    receiver_policy,
)

ENVELOPE_VERSION = 1
ENVELOPE_SPEC_VERSION = SPEC_VERSION
ENVELOPE_FILENAME = "active_envelope.json"
ENVELOPE_COMPILE_ERROR = "envelope_compile_error"

# Safe-ID grammar for the message id embedded in the envelope. The runtime
# adapter compares this id against audit-record content, and it must never
# be able to act as a glob/path metacharacter anywhere downstream.
MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# Constraint keys enforced by runtime adapters. ``estimated_minutes`` and
# ``risk_tier`` are recorded for the §E self-check but carry no hook
# semantics in the MVP.
CONSTRAINT_KEYS = (
    "estimated_minutes",
    "expected_files_touched",
    "risk_tier",
    "target_repo",
    "destructive_ops",
    "external_side_effects",
    "creates_or_updates_pr",
    "comments_on_github",
    "commits_changes",
    "merges_pr",
    "files_issues",
    "sends_oacp_reply_only",
    "touches_auth_config_or_secrets",
    "touches_dependencies",
    "public_visibility",
)


class EnvelopeCompileError(ValueError):
    """Raised when a task_profile cannot be compiled into an envelope."""

    reason_code = ENVELOPE_COMPILE_ERROR


def build_envelope(
    message: Dict[str, Any],
    config: Dict[str, Any],
    *,
    receiver: str,
    project: str,
    message_path: Optional[Path] = None,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Return an envelope dict for an admitted message, or raise
    :class:`EnvelopeCompileError`.

    The compiler does not re-run admission gates — it normalizes the declared
    profile with the gate's own functions and embeds the receiver-side
    allowlist so the hook can enforce repo boundaries without re-reading
    config.
    """
    try:
        _mode, policy = receiver_policy(config)
    except AutonomyConfigError as exc:
        raise EnvelopeCompileError(f"receiver config malformed: {exc}") from exc

    body = str(message.get("body") or "")
    profile, profile_error = extract_task_profile(body)
    if profile_error:
        raise EnvelopeCompileError("task_profile is unparsable")
    if profile is None:
        raise EnvelopeCompileError("message has no task_profile block")

    try:
        scope = normalize_scope_envelope(profile)
    except TaskProfileError as exc:
        raise EnvelopeCompileError(str(exc)) from exc

    constraints = {key: scope[key] for key in CONSTRAINT_KEYS}
    constraints["private_repo_allowlist"] = list(policy["private_repo_allowlist"])

    message_id = str(message.get("id") or "")
    if not message_id:
        raise EnvelopeCompileError("message has no id")
    if not MESSAGE_ID_RE.match(message_id):
        raise EnvelopeCompileError(
            f"message id {message_id!r} is outside the safe-id grammar"
        )

    return {
        "envelope_version": ENVELOPE_VERSION,
        "spec_version": ENVELOPE_SPEC_VERSION,
        "compiler": "envelope_compiler.py",
        "compiled_at_utc": now_iso or utc_now_iso(),
        "project": project,
        "receiver": receiver,
        "message_id": message_id,
        "message_sha256": message_sha256(message, message_path),
        "constraints": constraints,
        "counters": {
            "files_touched": [],
        },
        "enforcement": "hooks",
    }


# ── Envelope state I/O ────────────────────────────────────────────────────────


def envelope_path(oacp_root: Path, project: str, receiver: str) -> Path:
    return oacp_root / "projects" / project / "agents" / receiver / "state" / ENVELOPE_FILENAME


@contextmanager
def envelope_lock(path: Path) -> Iterator[None]:
    """Serialize envelope read-modify-write cycles across processes.

    A sibling lockfile survives the atomic replace of the envelope itself,
    so flocks always target a stable inode.
    """
    lock_path = path.parent / (path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_envelope(path: Path) -> Optional[Dict[str, Any]]:
    """Return the parsed envelope, or None when no envelope is active."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_envelope(path: Path, envelope: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(envelope, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


# ── CLI ───────────────────────────────────────────────────────────────────────


def _infer_project_from_message_path(message_path: Path) -> Optional[str]:
    parts = message_path.resolve().parts
    for index, part in enumerate(parts):
        if part == "projects" and index + 2 < len(parts) and parts[index + 2] == "agents":
            return parts[index + 1]
    return None


def _resolve_project(args: argparse.Namespace, message_path: Optional[Path]) -> str:
    if args.project:
        return str(args.project)
    if message_path is not None:
        inferred = _infer_project_from_message_path(message_path)
        if inferred:
            return inferred
    raise EnvelopeCompileError(
        "cannot infer project; pass --project or a message path inside "
        "$OACP_HOME/projects/<project>/"
    )


def _cmd_compile(args: argparse.Namespace, oacp_root: Path) -> int:
    message_path = Path(args.message)
    message = load_yaml_file(message_path)
    project = _resolve_project(args, message_path)

    if args.config:
        config_path = Path(args.config)
    else:
        config_path = oacp_root / "projects" / project / "agents" / args.receiver / "config.yaml"
    if not config_path.is_file():
        raise EnvelopeCompileError(f"receiver config not found: {config_path}")
    config = load_yaml_file(config_path)

    envelope = build_envelope(
        message,
        config,
        receiver=args.receiver,
        project=project,
        message_path=message_path,
    )

    target = envelope_path(oacp_root, project, args.receiver)
    with envelope_lock(target):
        existing = load_envelope(target)
        if existing is not None:
            same_message = existing.get("message_id") == envelope["message_id"]
            if not (same_message or args.extend or args.force):
                raise EnvelopeCompileError(
                    f"an active envelope for message "
                    f"{existing.get('message_id')!r} already exists at {target}; "
                    "run `oacp envelope clear`, or pass --extend to recompile "
                    "with prior counters preserved"
                )
            if same_message or args.extend:
                prior = existing.get("counters")
                if isinstance(prior, dict) and isinstance(
                    prior.get("files_touched"), list
                ):
                    envelope["counters"]["files_touched"] = list(
                        prior["files_touched"]
                    )
        write_envelope(target, envelope)

    if args.json:
        print(json.dumps(envelope, indent=2, sort_keys=True))
    else:
        print(f"OK: envelope compiled for {envelope['message_id']} -> {target}")
    return 0


def _cmd_show(args: argparse.Namespace, oacp_root: Path) -> int:
    project = _resolve_project(args, None)
    target = envelope_path(oacp_root, project, args.receiver)
    envelope = load_envelope(target)
    if envelope is None:
        print(f"No active envelope at {target}")
        return 1
    print(json.dumps(envelope, indent=2, sort_keys=True))
    return 0


def _cmd_clear(args: argparse.Namespace, oacp_root: Path) -> int:
    project = _resolve_project(args, None)
    target = envelope_path(oacp_root, project, args.receiver)
    with envelope_lock(target):
        try:
            target.unlink()
        except FileNotFoundError:
            print(f"No active envelope at {target}")
            return 0
    print(f"OK: cleared envelope at {target}")
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="oacp envelope",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--receiver", default="claude")
    common.add_argument("--project", default=None)
    common.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )

    compile_parser = sub.add_parser(
        "compile",
        parents=[common],
        help="Compile a message's task_profile into active_envelope.json",
    )
    compile_parser.add_argument("message", help="Path to the admitted inbox message")
    compile_parser.add_argument(
        "--config",
        default=None,
        help="Receiver config path (default: agents/<receiver>/config.yaml)",
    )
    compile_parser.add_argument(
        "--extend",
        action="store_true",
        help="Recompile over an existing envelope, preserving its counters "
        "(re-authorization flow)",
    )
    compile_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing envelope for a different message, "
        "resetting counters",
    )
    compile_parser.add_argument("--json", action="store_true")

    sub.add_parser("show", parents=[common], help="Print the active envelope")
    sub.add_parser("clear", parents=[common], help="Remove the active envelope")

    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    from _oacp_env import resolve_oacp_home

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    handlers = {
        "compile": _cmd_compile,
        "show": _cmd_show,
        "clear": _cmd_clear,
    }
    try:
        return handlers[args.command](args, oacp_root)
    except EnvelopeCompileError as exc:
        print(f"ERROR ({ENVELOPE_COMPILE_ERROR}): {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
