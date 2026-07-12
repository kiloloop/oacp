#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Record a structured human outcome in an autonomy audit file."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import yaml

from _oacp_constants import utc_now_iso
from autonomy_gate import (
    AUTONOMY_AUDIT_SCHEMA_VERSION,
    normalize_continuation_scope,
)


HUMAN_DECISIONS = {"approved", "modified", "declined"}
GRANT_DECISIONS = {"not_requested", "approved", "modified", "denied"}


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
    if not actor.strip():
        raise ValueError("actor must be non-empty")

    pause_recorded_at = str(audit.get("created_at_utc") or "")
    if not pause_recorded_at:
        raise ValueError("audit.created_at_utc is required")
    pause_time = _parse_utc(pause_recorded_at, "audit.created_at_utc")
    decision_time_text = decided_at_utc or utc_now_iso()
    decision_time = _parse_utc(decision_time_text, "decided_at_utc")
    if decision_time < pause_time:
        raise ValueError("decided_at_utc cannot precede audit.created_at_utc")

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

    pause_reasons = audit.get("reason_codes") or []
    if not isinstance(pause_reasons, list) or not all(
        isinstance(item, str) for item in pause_reasons
    ):
        raise ValueError("audit.reason_codes must be a list of strings")

    return {
        "recorded": True,
        "actor": actor.strip(),
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
    if updated.get("decision") != "paused":
        raise ValueError("human outcomes may be recorded only for paused audits")
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
    parser.add_argument("--actor", default="human")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing recorded human outcome",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        with args.audit_file.open("r", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
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
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

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
