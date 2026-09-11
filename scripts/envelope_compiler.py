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

Enforcement is earned, not assumed: the compiler detects whether the
receiver's runtime has a live envelope adapter -- by console name, never by
module path -- and stamps ``enforcement: hooks`` only when it resolves. The
``none`` states are named (``enforcement_reason``), the loud ones carry a
stderr advisory, the compile still succeeds, and ``--audit`` writes the same
state into the admission record.

Compile failures are fail-closed: the receiver must pause the task with
reason code ``envelope_compile_error`` instead of executing unenforced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from _oacp_constants import ALL_RUNTIMES, SPEC_VERSION, locked_audit, utc_now_iso
from autonomy_gate import (
    AutonomyConfigError,
    TaskProfileError,
    extract_task_profile,
    message_sha256,
    normalize_scope_envelope,
    receiver_policy,
)

ENVELOPE_VERSION = 1
ENVELOPE_SPEC_VERSION = SPEC_VERSION
ENVELOPE_FILENAME = "active_envelope.json"
ENVELOPE_COMPILE_ERROR = "envelope_compile_error"
# The named none-by-rule marker for admitted public-visibility tasks whose
# human admission approval is the runtime control: no envelope compiles,
# and the audit record says so explicitly rather than staying silent.
ENFORCEMENT_REASON_PUBLIC_APPROVED = "public_visibility_admission_approved"

# ── Adapter detection ─────────────────────────────────────────────────────────

# The console script each adapter-capable runtime registers as its envelope
# adapter. Detection resolves the console NAME -- on PATH, and in the hook
# registration ``oacp setup <runtime>`` writes -- never a module path, so the
# adapter can move between distributions without touching the compiler.
ADAPTER_CONSOLE_BY_RUNTIME: Dict[str, str] = {"claude": "oacp-envelope-hook"}
# The hook event the registration must name the console under.
ADAPTER_HOOK_EVENT = "PreToolUse"
# Card runtimes that carry no runtime identity to detect against.
ADAPTER_INDETERMINATE_RUNTIMES = ("unknown",)

ADAPTER_RESOLVED = "resolved"
ADAPTER_UNSUPPORTED = "unsupported"
ADAPTER_EXPECTED_MISSING = "expected_missing"
ADAPTER_FAILED = "failed"
ADAPTER_STATES = (
    ADAPTER_RESOLVED,
    ADAPTER_UNSUPPORTED,
    ADAPTER_EXPECTED_MISSING,
    ADAPTER_FAILED,
)

ENFORCEMENT_HOOKS = "hooks"
ENFORCEMENT_NONE = "none"
# Reason vocabulary for the ``none`` states, in the same shape as the
# public-visibility marker above so the envelope and the audit record tell
# one story. ``resolved`` carries no reason: enforcement is ``hooks``.
ENFORCEMENT_REASON_BY_ADAPTER_STATE: Dict[str, str] = {
    ADAPTER_UNSUPPORTED: "adapter_unsupported",
    ADAPTER_EXPECTED_MISSING: "adapter_expected_missing",
    ADAPTER_FAILED: "adapter_detection_failed",
}

WhichFn = Callable[[str], Optional[str]]
# Registration probe: given the console name, True when the receiver's
# workspace registers it as a hook, False when it does not. Raises
# :class:`AdapterDetectionError` when the registration cannot be read.
RegistrationFn = Callable[[str], bool]


class AdapterDetectionError(RuntimeError):
    """Raised by a probe that cannot determine what it was asked to read."""


@dataclass(frozen=True)
class AdapterDetection:
    """One compile's adapter verdict: exactly one of :data:`ADAPTER_STATES`."""

    state: str
    runtime: Optional[str]
    console: Optional[str]
    detail: str

    @property
    def enforcement(self) -> str:
        return ENFORCEMENT_HOOKS if self.state == ADAPTER_RESOLVED else ENFORCEMENT_NONE

    @property
    def enforcement_reason(self) -> Optional[str]:
        return ENFORCEMENT_REASON_BY_ADAPTER_STATE.get(self.state)

    @property
    def advisory(self) -> bool:
        """True for the loud states: an adapter that should be there is not."""
        return self.state in (ADAPTER_EXPECTED_MISSING, ADAPTER_FAILED)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "runtime": self.runtime,
            "state": self.state,
            "console": self.console,
            "detail": self.detail,
        }


def detect_adapter(
    runtime: Optional[str],
    *,
    which_fn: WhichFn = shutil.which,
    registration_fn: RegistrationFn,
    runtime_error: Optional[str] = None,
) -> AdapterDetection:
    """Resolve whether the receiver's runtime has a live envelope adapter.

    Every call yields exactly one of four states:

    - ``resolved``: an adapter-capable runtime whose console script is on
      PATH *and* registered as a hook in the receiver's workspace --
      enforcement ``hooks``, earned rather than assumed;
    - ``unsupported``: a known runtime with no envelope adapter by design
      (``codex`` and the other card runtimes) -- enforcement ``none``;
    - ``expected_missing``: an adapter-capable runtime whose console or
      registration did not resolve -- enforcement ``none``, loud;
    - ``failed``: the runtime cannot be determined (no agent card,
      ``unknown``, a value outside the card enum) or a probe itself
      errored -- enforcement ``none``, loud.

    Detection never raises: a compile must still succeed, with the
    degradation named, when detection itself breaks.
    """
    if not runtime:
        return AdapterDetection(
            ADAPTER_FAILED, None, None, runtime_error or "receiver runtime unknown"
        )
    runtime = str(runtime).strip()
    console = ADAPTER_CONSOLE_BY_RUNTIME.get(runtime)
    if console is None:
        if runtime in ALL_RUNTIMES and runtime not in ADAPTER_INDETERMINATE_RUNTIMES:
            return AdapterDetection(
                ADAPTER_UNSUPPORTED,
                runtime,
                None,
                f"runtime {runtime!r} has no envelope adapter",
            )
        return AdapterDetection(
            ADAPTER_FAILED,
            runtime,
            None,
            f"runtime {runtime!r} is not a runtime an adapter can be resolved for",
        )
    try:
        console_path = which_fn(console)
    except Exception as exc:
        return AdapterDetection(
            ADAPTER_FAILED, runtime, console, f"PATH lookup for {console!r} failed: {exc}"
        )
    try:
        registered = bool(registration_fn(console))
    except AdapterDetectionError as exc:
        return AdapterDetection(ADAPTER_FAILED, runtime, console, str(exc))
    except Exception as exc:
        return AdapterDetection(
            ADAPTER_FAILED,
            runtime,
            console,
            f"hook registration probe for {console!r} failed: {exc}",
        )
    if console_path and registered:
        return AdapterDetection(
            ADAPTER_RESOLVED,
            runtime,
            console,
            f"console script {console!r} on PATH at {console_path}; "
            f"registered as a {ADAPTER_HOOK_EVENT} hook",
        )
    missing: List[str] = []
    if not console_path:
        missing.append(f"console script {console!r} not found on PATH")
    if not registered:
        missing.append(
            f"{console!r} not registered as a {ADAPTER_HOOK_EVENT} hook in the "
            "receiver's workspace"
        )
    return AdapterDetection(ADAPTER_EXPECTED_MISSING, runtime, console, "; ".join(missing))


def read_receiver_runtime(
    oacp_root: Path, project: str, receiver: str
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(runtime, error)`` from the receiver's agent card."""
    import yaml  # type: ignore

    card_path = oacp_root / "projects" / project / "agents" / receiver / "agent_card.yaml"
    try:
        data = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"agent card not found: {card_path}"
    # UnicodeDecodeError is a ValueError, not an OSError: a card that
    # cannot be decoded is as unreadable as one that cannot be opened.
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return None, f"agent card unreadable: {card_path}: {exc}"
    if not isinstance(data, dict):
        return None, f"agent card must contain a YAML mapping: {card_path}"
    runtime = str(data.get("runtime") or "").strip()
    if not runtime:
        return None, f"agent card names no runtime: {card_path}"
    return runtime, None


def workspace_hook_registration(oacp_root: Path, project: str, console: str) -> bool:
    """True when the project's repo registers ``console`` as a hook.

    The registration ``oacp setup claude`` writes lives in the project
    repo's ``.claude/settings.json``; the repo is the ``repo_path`` the
    workspace marker records. A missing settings file or hook list is an
    absent registration (False); a workspace marker or settings file that
    cannot be read is indeterminate and raises
    :class:`AdapterDetectionError`.
    """
    workspace_file = oacp_root / "projects" / project / "workspace.json"
    try:
        workspace = json.loads(workspace_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AdapterDetectionError(
            f"workspace marker not found: {workspace_file}"
        ) from None
    except (OSError, ValueError) as exc:
        raise AdapterDetectionError(
            f"workspace marker unreadable: {workspace_file}: {exc}"
        ) from exc
    repo_path = workspace.get("repo_path") if isinstance(workspace, dict) else None
    if not repo_path:
        raise AdapterDetectionError(
            f"workspace marker names no repo_path: {workspace_file}"
        )
    repo_dir = Path(str(repo_path)).expanduser()
    if not repo_dir.is_dir():
        raise AdapterDetectionError(f"repo_path is not a directory: {repo_dir}")
    settings_file = repo_dir / ".claude" / "settings.json"
    if not settings_file.is_file():
        return False
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdapterDetectionError(
            f"hook settings unreadable: {settings_file}: {exc}"
        ) from exc
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    entries = hooks.get(ADAPTER_HOOK_EVENT) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return False
    # The same reader ``oacp setup claude`` uses to decide whether the
    # registration is already present, so "registered" means one thing.
    from setup_runtime import _hook_command_exists

    return _hook_command_exists(entries, console)


def detect_receiver_adapter(
    oacp_root: Path,
    project: str,
    receiver: str,
    *,
    which_fn: WhichFn = shutil.which,
    registration_fn: Optional[RegistrationFn] = None,
) -> AdapterDetection:
    """Card read plus detection for one receiver. Never raises.

    Anything that escapes the card reader or a probe is the ``failed``
    state with the error named, so no detection defect can turn a
    compile into a crash: the grammar is closed at this boundary.
    """
    if registration_fn is None:
        registration_fn = functools.partial(
            workspace_hook_registration, oacp_root, project
        )
    try:
        runtime, runtime_error = read_receiver_runtime(oacp_root, project, receiver)
        return detect_adapter(
            runtime,
            which_fn=which_fn,
            registration_fn=registration_fn,
            runtime_error=runtime_error,
        )
    except Exception as exc:
        return AdapterDetection(
            ADAPTER_FAILED, None, None, f"adapter detection errored: {exc}"
        )


# Session-claim sidecar: the runtime hook records the compiling session's
# identity here (it alone sees the harness session id, on the tool call that
# runs the compile); the compiler consumes it and stamps the envelope. A
# claim older than this window, or naming a different message file, is
# ignored — the envelope then compiles unbound (session_id null) and the
# hook enforces for every session, the pre-session-binding behavior.
SESSION_CLAIM_FILENAME = "pending_session_claim.json"
SESSION_CLAIM_MAX_AGE_SECONDS = 120

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


def _parse_message_snapshot(raw: bytes, path: Path) -> Dict[str, Any]:
    """Parse the admitted message from its verified snapshot bytes."""
    import yaml  # type: ignore

    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except Exception as exc:
        raise EnvelopeCompileError(f"cannot parse message {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EnvelopeCompileError(f"{path} must contain a YAML mapping")
    return data


def build_envelope(
    message: Dict[str, Any],
    config: Dict[str, Any],
    *,
    receiver: str,
    project: str,
    message_path: Optional[Path] = None,
    now_iso: Optional[str] = None,
    session_id: Optional[str] = None,
    message_raw: Optional[bytes] = None,
    adapter: Optional[AdapterDetection] = None,
) -> Dict[str, Any]:
    """Return an envelope dict for an admitted message, or raise
    :class:`EnvelopeCompileError`.

    The compiler does not re-run admission gates — it normalizes the declared
    profile with the gate's own functions and embeds the receiver-side
    allowlist so the hook can enforce repo boundaries without re-reading
    config.

    ``adapter`` is the receiver's :func:`detect_adapter` verdict and decides
    ``enforcement``. A caller that supplies none gets the ``failed`` state:
    ``hooks`` is stamped only when an adapter was detected, never by default.
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

    if adapter is None:
        adapter = AdapterDetection(
            ADAPTER_FAILED, None, None, "no adapter detection supplied to the compiler"
        )
    envelope: Dict[str, Any] = {
        "envelope_version": ENVELOPE_VERSION,
        "spec_version": ENVELOPE_SPEC_VERSION,
        "compiler": "envelope_compiler.py",
        "compiled_at_utc": now_iso or utc_now_iso(),
        "project": project,
        "receiver": receiver,
        "message_id": message_id,
        "message_sha256": message_sha256(message, message_path, message_raw),
        "constraints": constraints,
        "counters": {
            "files_touched": [],
        },
        "enforcement": adapter.enforcement,
        "adapter": adapter.as_dict(),
        "session_id": session_id or None,
    }
    if adapter.enforcement_reason is not None:
        envelope["enforcement_reason"] = adapter.enforcement_reason
    return envelope


# ── Envelope state I/O ────────────────────────────────────────────────────────


def envelope_path(oacp_root: Path, project: str, receiver: str) -> Path:
    return oacp_root / "projects" / project / "agents" / receiver / "state" / ENVELOPE_FILENAME


def session_claim_path(envelope_target: Path, session_id: str) -> Path:
    """Per-session claim file: concurrent sessions never overwrite each other.

    Distinct files are what makes a same-message claim race *detectable* —
    with one shared file, last-writer-wins would silently bind the compile
    to whichever session claimed last.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    stem, suffix = SESSION_CLAIM_FILENAME.rsplit(".", 1)
    return envelope_target.parent / f"{stem}.{digest}.{suffix}"


def _iter_session_claim_paths(envelope_target: Path) -> List[Path]:
    stem, suffix = SESSION_CLAIM_FILENAME.rsplit(".", 1)
    return sorted(envelope_target.parent.glob(f"{stem}*.{suffix}"))


def write_session_claim(
    envelope_target: Path, session_id: str, message_name: str
) -> None:
    """Record the compiling session's identity for the compiler to consume.

    Callers (the runtime hook) must hold ``envelope_lock(envelope_target)``.
    The claim is advisory: losing or skipping it degrades to an unbound
    envelope, never to a wrong binding.
    """
    claim = {
        "session_id": session_id,
        "message_name": message_name,
        "claimed_at_utc": utc_now_iso(),
    }
    path = session_claim_path(envelope_target, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(claim, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_session_claim(raw: str, message_name: str) -> Tuple[str, Optional[str]]:
    """Classify a claim against ``message_name``.

    Returns ``("match", session_id)`` for a fresh claim naming the same
    message, ``("other", None)`` for a fresh claim naming a different
    message (a concurrent compile in flight — it belongs to that compile),
    and ``("discard", None)`` for malformed or stale claims.
    """
    try:
        claim = json.loads(raw)
        if not isinstance(claim, dict):
            return ("discard", None)
        claimed_at = dt.datetime.strptime(
            str(claim.get("claimed_at_utc") or ""), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - claimed_at).total_seconds()
        if not (0 <= age <= SESSION_CLAIM_MAX_AGE_SECONDS):
            return ("discard", None)
        if str(claim.get("message_name") or "") != message_name:
            return ("other", None)
        session_id = str(claim.get("session_id") or "")
        return ("match", session_id) if session_id else ("discard", None)
    except (ValueError, TypeError):
        return ("discard", None)


def consume_session_claim(envelope_target: Path, message_name: str) -> Optional[str]:
    """Read, validate, and delete the pending claims for ``message_name``.

    Returns a session id only when exactly one session holds a fresh claim
    naming the same message file. Two *different* sessions with fresh claims
    for the same message are indistinguishable to the compiler — the compile
    could belong to either — so ambiguity degrades to an unbound envelope,
    never to a wrong binding. Malformed and stale claims are garbage-collected;
    a fresh claim naming a *different* message survives untouched so the
    concurrent compile it belongs to can still bind. Callers must hold
    ``envelope_lock``.
    """
    claimed_sessions = set()
    for path in _iter_session_claim_paths(envelope_target):
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
        verdict, session_id = _parse_session_claim(raw, message_name)
        if verdict == "other":
            continue
        try:
            path.unlink()
        except (FileNotFoundError, OSError):
            pass
        if verdict == "match" and session_id:
            claimed_sessions.add(session_id)
    if len(claimed_sessions) == 1:
        return claimed_sessions.pop()
    return None


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


def _resolve_admission_audit_path(
    raw: str, oacp_root: Path, project: str, receiver: str
) -> Path:
    """Contain ``--audit`` to the receiver's canonical admission audit dir.

    The record authorizes skipping envelope enforcement, so an arbitrary
    readable YAML path must never qualify — only a record the admission
    gate itself could have written.
    """
    canonical = (
        oacp_root / "projects" / project / "agents" / receiver
        / "audit" / "autonomy_decisions"
    ).resolve()
    resolved = Path(raw).resolve()
    try:
        resolved.relative_to(canonical)
    except ValueError:
        raise EnvelopeCompileError(
            "--audit must name a record inside the receiver's canonical "
            f"admission audit directory ({canonical}); got {resolved}"
        ) from None
    return resolved


def _stamp_none_by_rule_approved(
    audit_path: Path,
    *,
    message_id: str,
    receiver: str,
    message_sha256: str,
) -> bool:
    """Validate and stamp the approval record in ONE locked read.

    Eligibility and the marker write consume the same locked snapshot — a
    record swapped after a separate eligibility read can never be the one
    stamped. Eligible means: an admission-PAUSED record (the gate's
    ``decision: paused`` with ``completion_kind: admission_paused``),
    carrying a ``schema_version``, content-matched on ``message_id`` +
    ``receiver`` (never the filename), bound to the exact verified message
    snapshot via ``message_sha256``, with a recorded human outcome of
    ``approved`` or ``modified``. Returns True when stamped;
    ``envelope_enforcement`` stays ``none`` and the named reason is what
    makes the mode a recorded rule rather than a silent absence.
    """
    import yaml  # type: ignore

    audit_path = Path(audit_path)
    with locked_audit(audit_path):
        try:
            audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return False
        if not isinstance(audit, dict):
            return False
        result = audit.get("result")
        outcome = (
            result.get("human_outcome") if isinstance(result, dict) else None
        )
        eligible = (
            bool(audit.get("schema_version"))
            and audit.get("message_id") == message_id
            and audit.get("receiver") == receiver
            and audit.get("message_sha256") == message_sha256
            and audit.get("decision") == "paused"
            and isinstance(result, dict)
            and result.get("completion_kind") == "admission_paused"
            and isinstance(outcome, dict)
            and outcome.get("recorded") is True
            and outcome.get("decision") in ("approved", "modified")
        )
        if not eligible:
            return False
        result["envelope_enforcement"] = "none"
        result["envelope_enforcement_reason"] = (
            ENFORCEMENT_REASON_PUBLIC_APPROVED
        )
        _replace_audit(audit_path, audit)
    return True


def _replace_audit(audit_path: Path, audit: Dict[str, Any]) -> None:
    """Atomically rewrite an audit record; callers hold ``locked_audit``."""
    import yaml  # type: ignore

    content = yaml.safe_dump(audit, sort_keys=False, allow_unicode=True)
    mode = audit_path.stat().st_mode
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(audit_path.parent),
            prefix=f".{audit_path.name}.",
            suffix=".ee.tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, mode)
        os.replace(temp_path, audit_path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _stamp_adapter_enforcement(
    audit_path: Path,
    *,
    message_id: str,
    receiver: str,
    message_sha256: str,
    adapter: AdapterDetection,
) -> bool:
    """Stamp a compiled envelope's enforcement state into the admission record.

    ONE locked read, like the none-by-rule stamp: eligible means a record
    carrying a ``schema_version``, content-matched on ``message_id`` +
    ``receiver`` (never the filename), bound to the exact verified message
    snapshot via ``message_sha256``, with a ``result`` mapping. The record
    then reads ``envelope_enforcement`` exactly as the envelope does --
    ``hooks`` with no reason for a resolved adapter, ``none`` plus the
    reason naming the state otherwise. Returns False, touching nothing,
    when the record does not bind.
    """
    import yaml  # type: ignore

    audit_path = Path(audit_path)
    with locked_audit(audit_path):
        try:
            audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return False
        if not isinstance(audit, dict):
            return False
        result = audit.get("result")
        eligible = (
            bool(audit.get("schema_version"))
            and audit.get("message_id") == message_id
            and audit.get("receiver") == receiver
            and audit.get("message_sha256") == message_sha256
            and isinstance(result, dict)
        )
        if not eligible:
            return False
        result["envelope_enforcement"] = adapter.enforcement
        reason = adapter.enforcement_reason
        if reason is None:
            result.pop("envelope_enforcement_reason", None)
        else:
            result["envelope_enforcement_reason"] = reason
        _replace_audit(audit_path, audit)
    return True


def _cmd_compile(
    args: argparse.Namespace,
    oacp_root: Path,
    *,
    which_fn: WhichFn = shutil.which,
    registration_fn: Optional[RegistrationFn] = None,
) -> int:
    from message_verify import (
        STATUS_VERIFIED,
        read_message_bounded,
        verify_mode_from_config,
    )

    message_path = Path(args.message)
    # One bounded snapshot: the bytes verified below are the bytes parsed
    # into constraints and the bytes the envelope's message_sha256 names.
    try:
        message_raw = read_message_bounded(message_path)
    except OSError as exc:
        raise EnvelopeCompileError(f"cannot read message: {exc}") from exc
    project = _resolve_project(args, message_path)
    # An admission record is authorization (the none-by-rule branch) and
    # provenance (the enforcement stamp), so its path is contained before
    # anything else happens: an arbitrary readable YAML qualifies for neither.
    audit_path: Optional[Path] = None
    if args.audit:
        audit_path = _resolve_admission_audit_path(
            args.audit, oacp_root, project, args.receiver
        )

    if args.config:
        config_path = Path(args.config)
    else:
        config_path = oacp_root / "projects" / project / "agents" / args.receiver / "config.yaml"
    if not config_path.is_file():
        raise EnvelopeCompileError(f"receiver config not found: {config_path}")
    # Authorized policy read: the envelope's bounds come from the receiver
    # config, so an unauthorized (tampered/stripped-when-enrolled) config
    # must fail the compile, not silently shape the envelope.
    import policy_signing

    try:
        config, _policy_auth, _raw = policy_signing.load_authorized_policy(
            config_path,
            oacp_root,
            receiver=args.receiver,
            kind=policy_signing.POLICY_KIND_RECEIVER_CONFIG,
            project=project,
        )
    except policy_signing.PolicyAuthError as exc:
        raise EnvelopeCompileError(str(exc)) from exc

    # The runtime constraints come from the message, so the snapshot is
    # verified under the (authorized) receiver policy before it is parsed:
    # under enforce, an unverified message must not shape hook enforcement.
    verify_mode = verify_mode_from_config(config)
    if verify_mode in ("warn", "enforce"):
        from message_verify import (
            ALLOWED_SIGNERS_RELPATH,
            _load_pins_policy_checked,
            verify_message,
        )

        pins_path = config_path.parent / ALLOWED_SIGNERS_RELPATH
        pins, trust_error, _trust_policy_auth = _load_pins_policy_checked(
            pins_path, receiver=args.receiver, oacp_dir=str(oacp_root)
        )
        message_auth = verify_message(
            message_raw,
            pins,
            trust_source=str(pins_path),
            trust_error=trust_error,
        )
        if verify_mode == "enforce" and message_auth["status"] != STATUS_VERIFIED:
            raise EnvelopeCompileError(
                "message failed verification under enforce "
                f"({message_auth['status']}: {message_auth['reason'] or 'not verified'}) "
                "— refusing to compile an envelope from an unverified message"
            )

    message = _parse_message_snapshot(message_raw, message_path)

    # Enforcement is earned: resolve the RECEIVER's runtime from its agent
    # card and detect its adapter by console name. Detection never fails the
    # compile; its verdict is stamped into the envelope (and, with --audit,
    # the admission record) so a pickup-gate-only receiver is always named.
    adapter = detect_receiver_adapter(
        oacp_root,
        project,
        args.receiver,
        which_fn=which_fn,
        registration_fn=registration_fn,
    )

    envelope = build_envelope(
        message,
        config,
        receiver=args.receiver,
        project=project,
        message_path=message_path,
        message_raw=message_raw,
        adapter=adapter,
    )

    if envelope["constraints"]["public_visibility"] and audit_path is not None:
        # Admitted public-visibility tasks with recorded human admission
        # approval run under envelope_enforcement: none BY RULE — the
        # compiler deliberately does not compile (a compiled public
        # envelope denies the entire approved chain), and the audit record
        # names the mode instead of leaving an absent field. Human
        # admission plus live supervision is the control; the exception is
        # deliberate and recorded, and retires when a post-approval
        # envelope path ships. Without a matching approved record the
        # normal fail-closed compile below still runs.
        target = envelope_path(oacp_root, project, args.receiver)
        stamped = False
        with envelope_lock(target):
            # A none-by-rule result must MEAN no envelope governs the
            # receiver: any active envelope fails closed and keeps its
            # normal lifecycle (never silently deleted, never reported
            # around).
            if load_envelope(target) is not None:
                raise EnvelopeCompileError(
                    f"an active envelope already exists at {target}; a "
                    "none-by-rule result must not coexist with an active "
                    "envelope — clear it via `oacp envelope clear` first"
                )
            stamped = _stamp_none_by_rule_approved(
                audit_path,
                message_id=envelope["message_id"],
                receiver=args.receiver,
                message_sha256=envelope["message_sha256"],
            )
            if stamped:
                # A deliberate no-envelope success still consumes this
                # compile's session claim — a dangling claim would bind a
                # later, unrelated compile.
                consume_session_claim(target, message_path.name)
        if stamped:
            if args.json:
                print(
                    json.dumps(
                        {
                            "envelope_enforcement": "none",
                            "envelope_enforcement_reason": (
                                ENFORCEMENT_REASON_PUBLIC_APPROVED
                            ),
                            "message_id": envelope["message_id"],
                            "audit_record": str(audit_path),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(
                    "OK: admitted public-visibility task with recorded human "
                    "approval — envelope deliberately not compiled; "
                    "envelope_enforcement: none "
                    f"({ENFORCEMENT_REASON_PUBLIC_APPROVED}) recorded in "
                    "the audit record"
                )
            return 0

    target = envelope_path(oacp_root, project, args.receiver)
    with envelope_lock(target):
        claimed_session = consume_session_claim(target, message_path.name)
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
                # A recompile for the same task keeps its session binding:
                # the documented --extend runs outside the bound session
                # (post-re-auth, often a human terminal with no hook to
                # write a fresh claim), and dropping the binding there
                # would silently re-expose peer sessions to enforcement.
                if claimed_session is None:
                    claimed_session = str(existing.get("session_id") or "") or None
        envelope["session_id"] = claimed_session
        write_envelope(target, envelope)
        # The admission record tells the same story as the envelope:
        # stamped by the compiler for every one of the three states,
        # under the audit lock and STILL under the envelope lock (the
        # none-by-rule branch's lock order), so two successful compiles
        # of one message can never leave the envelope naming one state
        # and the record another.
        stamped = audit_path is None or _stamp_adapter_enforcement(
            audit_path,
            message_id=envelope["message_id"],
            receiver=args.receiver,
            message_sha256=envelope["message_sha256"],
            adapter=adapter,
        )

    if not stamped:
        print(
            f"WARNING: --audit record {audit_path} does not bind to message "
            f"{envelope['message_id']}; envelope_enforcement not stamped",
            file=sys.stderr,
        )
    if adapter.advisory:
        # Fail loud, not closed: the envelope is written and the compile
        # succeeds, but an adapter-capable receiver running pickup-gate-only
        # is named here and in both artifacts, never silently.
        print(
            f"ADVISORY ({adapter.enforcement_reason}): {adapter.detail}; "
            f"envelope compiled with enforcement: {ENFORCEMENT_NONE} "
            "(pickup-gate-only)",
            file=sys.stderr,
        )

    if args.json:
        print(json.dumps(envelope, indent=2, sort_keys=True))
    else:
        summary = f"enforcement: {adapter.enforcement}"
        if adapter.enforcement_reason is not None:
            summary += f" ({adapter.enforcement_reason})"
        print(
            f"OK: envelope compiled for {envelope['message_id']} -> {target} "
            f"[{summary}]"
        )
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
        "--audit",
        default=None,
        help=(
            "Admission audit record for this message: the compiler stamps "
            "result.envelope_enforcement (and its reason) from the detected "
            "adapter; on an admitted public-visibility task with recorded "
            "human approval, the envelope is deliberately not compiled and "
            "the record is stamped envelope_enforcement: none by rule"
        ),
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


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    which_fn: WhichFn = shutil.which,
    registration_fn: Optional[RegistrationFn] = None,
) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    from _oacp_env import resolve_oacp_home

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    handlers = {
        "show": _cmd_show,
        "clear": _cmd_clear,
    }
    try:
        if args.command == "compile":
            return _cmd_compile(
                args, oacp_root, which_fn=which_fn, registration_fn=registration_fn
            )
        return handlers[args.command](args, oacp_root)
    except EnvelopeCompileError as exc:
        print(f"ERROR ({ENVELOPE_COMPILE_ERROR}): {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
