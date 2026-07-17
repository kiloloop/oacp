#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Record a structured human outcome in an autonomy audit file."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import yaml

from _oacp_constants import locked_audit, utc_now_iso
from autonomy_gate import (
    AUTONOMY_AUDIT_SCHEMA_VERSION,
    PINNED_COMPLETION_KINDS,
    normalize_continuation_scope,
)


HUMAN_DECISIONS = {"approved", "modified", "declined"}
GRANT_DECISIONS = {"not_requested", "approved", "modified", "denied"}
# Reason codes the evaluator emits for checkpoint-time pauses. Fallback
# discriminator ONLY for records that predate the pinned completion_kind
# enum — for enum-stamped records `result.completion_kind` is authoritative
# (declaration_error names both admission contradictions and checkpoint
# drift, so reason codes alone cannot separate the two phases).
CHECKPOINT_REASON_CODES = {"threshold_checkpoint_breached", "declaration_error"}


def _checkpoint_block(audit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    result = audit.get("result")
    if not isinstance(result, dict):
        return None
    checkpoint = result.get("threshold_checkpoint")
    return checkpoint if isinstance(checkpoint, dict) else None


def is_checkpoint_paused(audit: Dict[str, Any]) -> bool:
    """True when the record's pause is a §E checkpoint, not an admission pause.

    Two shapes qualify: an auto-accepted admission whose in-place checkpoint
    later breached (pause time != record creation time — the reason this
    distinction exists), and a checkpoint evaluation written as its own
    paused decision. A breached checkpoint block alone is not enough: an
    admission pause evaluated with over-limit actuals attached still pauses
    for its admission reason, at admission time.

    For current-schema records the pinned ``result.completion_kind`` is
    the authoritative phase discriminator across BOTH supported shapes:
    ``checkpoint_paused`` qualifies, ``admission_paused`` never does
    (regardless of what the attached actuals happen to breach), and the
    breached in-place auto-accepted shape must itself carry
    ``checkpoint_paused`` — the checkpoint re-evaluation is what updated
    the result block, so any other kind there is malformed. A
    current-schema record whose kind is missing or out of vocabulary is
    refused loudly rather than silently reclassified. The decision-based
    inference and the reason-code intersection survive only for records
    that genuinely predate the enum (``schema_version`` < 2 with no
    pinned kind).
    """
    checkpoint = _checkpoint_block(audit) or {}
    if checkpoint.get("breached") is not True:
        return False
    result = audit.get("result")
    kind = result.get("completion_kind") if isinstance(result, dict) else None
    schema = audit.get("schema_version")
    if isinstance(schema, int) and schema >= AUTONOMY_AUDIT_SCHEMA_VERSION:
        if kind not in PINNED_COMPLETION_KINDS:
            raise ValueError(
                f"schema-v{schema} record carries a breached checkpoint "
                f"but no pinned completion_kind (found {kind!r}); cannot "
                "classify the pause phase"
            )
        if audit.get("decision") == "auto_accepted" and kind != "checkpoint_paused":
            raise ValueError(
                f"schema-v{schema} auto-accepted record carries a breached "
                f"checkpoint but completion_kind {kind!r}; the in-place "
                "checkpoint shape requires checkpoint_paused"
            )
        return kind == "checkpoint_paused"
    if audit.get("decision") == "auto_accepted":
        return True
    if kind in PINNED_COMPLETION_KINDS:
        return kind == "checkpoint_paused"
    reasons = set(audit.get("reason_codes") or [])
    return bool(reasons & CHECKPOINT_REASON_CODES)


def _load_mapping(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _parse_utc(value: str, field: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DDTHH:MM:SSZ") from exc


def _requested_scope(
    audit: Dict[str, Any],
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    profile = audit.get("task_profile")
    if not isinstance(profile, dict):
        profile = audit.get("scope_envelope")
    if not isinstance(profile, dict):
        return False, None, None
    grants = profile.get("continuation_grants")
    if not isinstance(grants, dict):
        return False, None, None
    grant = grants.get("approved_thread_continuation")
    if not isinstance(grant, dict):
        return False, None, None
    scope, error = normalize_continuation_scope(grant.get("scope"))
    return True, scope, error


def build_human_outcome(
    audit: Dict[str, Any],
    *,
    decision: str,
    grant_decision: str = "not_requested",
    decided_at_utc: Optional[str] = None,
    granted_scope: Optional[Dict[str, Any]] = None,
    actor: str = "human",
) -> Dict[str, Any]:
    if decision not in HUMAN_DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(sorted(HUMAN_DECISIONS))}")
    if grant_decision not in GRANT_DECISIONS:
        choices = ", ".join(sorted(GRANT_DECISIONS))
        raise ValueError(f"grant_decision must be one of: {choices}")
    actor_value = actor.strip()
    if not actor_value:
        raise ValueError("actor must be non-empty")
    if any(char.isspace() for char in actor_value):
        raise ValueError("actor must be a single stable handle (no whitespace)")

    # Latency measures pause -> decision. For admission pauses the record's
    # creation time IS the pause; for checkpoint pauses it is not (the record
    # was created at admission, possibly long before the checkpoint fired),
    # so the checkpoint's own pause stamp is required — falling back to
    # created_at_utc would count the whole execution window as human latency.
    if is_checkpoint_paused(audit):
        checkpoint = _checkpoint_block(audit) or {}
        pause_recorded_at = str(checkpoint.get("paused_at_utc") or "")
        if not pause_recorded_at:
            raise ValueError(
                "checkpoint-paused audit lacks threshold_checkpoint."
                "paused_at_utc; refusing to measure latency from admission "
                "time — re-evaluate the checkpoint with a paused_at_utc stamp"
            )
        pause_time = _parse_utc(
            pause_recorded_at, "threshold_checkpoint.paused_at_utc"
        )
        pause_reasons: Any = (
            ["declaration_error"]
            if checkpoint.get("declaration_errors")
            else ["threshold_checkpoint_breached"]
        )
    else:
        pause_recorded_at = str(audit.get("created_at_utc") or "")
        if not pause_recorded_at:
            raise ValueError("audit.created_at_utc is required")
        pause_time = _parse_utc(pause_recorded_at, "audit.created_at_utc")
        pause_reasons = audit.get("reason_codes") or []

    decision_time_text = decided_at_utc or utc_now_iso()
    decision_time = _parse_utc(decision_time_text, "decided_at_utc")
    if decision_time < pause_time:
        raise ValueError("decided_at_utc cannot precede the recorded pause time")

    request_present, requested_scope, request_error = _requested_scope(audit)
    if request_present and request_error is None and grant_decision == "not_requested":
        raise ValueError(
            "audit contains a grant request; record an explicit --grant-decision"
        )
    normalized_granted_scope: Optional[Dict[str, Any]] = None
    if grant_decision == "approved":
        candidate_scope = granted_scope if granted_scope is not None else requested_scope
        normalized_granted_scope, error = normalize_continuation_scope(
            candidate_scope
        )
        if error:
            raise ValueError(f"approved grant requires a valid scope: {error}")
    elif grant_decision == "modified":
        if granted_scope is None:
            raise ValueError("modified grant requires --grant-scope-file")
        normalized_granted_scope, error = normalize_continuation_scope(
            granted_scope
        )
        if error:
            raise ValueError(f"modified grant requires a valid scope: {error}")
    elif granted_scope is not None:
        raise ValueError(
            "granted scope is valid only for approved or modified grants"
        )

    if decision == "declined" and grant_decision in {"approved", "modified"}:
        raise ValueError("a declined task cannot approve or modify a grant")

    if not isinstance(pause_reasons, list) or not all(
        isinstance(item, str) for item in pause_reasons
    ):
        raise ValueError("audit.reason_codes must be a list of strings")

    return {
        "recorded": True,
        "actor": actor_value,
        "decision": decision,
        "decided_at_utc": decision_time_text,
        "decision_latency_seconds": int((decision_time - pause_time).total_seconds()),
        "pause_reason_codes": list(pause_reasons),
        "grant": {
            "decision": grant_decision,
            "request_present": request_present,
            "request_error": request_error,
            "requested_scope": requested_scope,
            "granted_scope": normalized_granted_scope,
        },
    }


def record_human_outcome(
    audit: Dict[str, Any],
    *,
    replace: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    updated = copy.deepcopy(audit)
    schema_version = updated.get("schema_version")
    if schema_version not in {1, AUTONOMY_AUDIT_SCHEMA_VERSION}:
        raise ValueError("audit.schema_version must be 1 or 2")
    if updated.get("decision") != "paused" and not is_checkpoint_paused(updated):
        raise ValueError(
            "human outcomes may be recorded only for paused or "
            "checkpoint-paused audits"
        )
    result = updated.get("result")
    if not isinstance(result, dict):
        raise ValueError("audit.result must be a mapping")
    existing = result.get("human_outcome")
    if (
        not replace
        and isinstance(existing, dict)
        and existing.get("recorded") is True
    ):
        raise ValueError("audit already has a recorded human outcome; use --replace")
    result["human_outcome"] = build_human_outcome(updated, **kwargs)
    updated["schema_version"] = AUTONOMY_AUDIT_SCHEMA_VERSION
    updated.setdefault("conversation_id", None)
    updated.setdefault("parent_message_id", None)
    return updated


def _atomic_write_yaml(path: Path, data: Dict[str, Any]) -> None:
    content = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    mode = path.stat().st_mode
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
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _grant_scope_from_file(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    data = _load_mapping(path)
    scope = data.get("scope", data)
    if not isinstance(scope, dict):
        raise ValueError(f"{path} must contain a grant scope mapping")
    return scope


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_file", type=Path)
    parser.add_argument("--decision", required=True, choices=sorted(HUMAN_DECISIONS))
    parser.add_argument(
        "--grant-decision",
        default="not_requested",
        choices=sorted(GRANT_DECISIONS),
    )
    parser.add_argument("--grant-scope-file", type=Path)
    parser.add_argument("--decided-at")
    parser.add_argument(
        "--actor",
        default="human",
        help=(
            "Stable per-person handle of the deciding human (one canonical "
            "handle fleet-wide). The default 'human' is anonymous and warns."
        ),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing recorded human outcome",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.actor == "human":
        print(
            "WARNING: actor 'human' is the anonymous default — pass "
            "--actor <handle> (one stable handle per person, fleet-wide) so "
            "outcomes can be attributed across receivers",
            file=sys.stderr,
        )

    try:
        # Shared stable audit lock (see _oacp_constants.locked_audit): every
        # audit read-modify-write path serializes here so concurrent writers
        # (e.g. message_auth attachment) cannot drop each other's blocks.
        # Locking the audit file's own inode is unsound across atomic
        # replaces — the inode a waiter locked may no longer be the file.
        with locked_audit(args.audit_file):
            audit = _load_mapping(args.audit_file)
            updated = record_human_outcome(
                audit,
                replace=args.replace,
                decision=args.decision,
                grant_decision=args.grant_decision,
                decided_at_utc=args.decided_at,
                granted_scope=_grant_scope_from_file(args.grant_scope_file),
                actor=args.actor,
            )
            if not args.dry_run:
                _atomic_write_yaml(args.audit_file, updated)

        outcome = updated["result"]["human_outcome"]
        if args.json:
            print(json.dumps({
                "audit_file": str(args.audit_file),
                "dry_run": args.dry_run,
                "schema_version": updated["schema_version"],
                "human_outcome": outcome,
            }, indent=2))
        elif args.dry_run:
            print(yaml.safe_dump(updated, sort_keys=False, allow_unicode=True).rstrip())
        else:
            print(
                f"OK: {args.audit_file} — {outcome['decision']} "
                f"({outcome['decision_latency_seconds']}s)"
            )
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
