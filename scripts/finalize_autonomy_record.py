#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Finalize and validate autonomy audit records.

One lock-aware code path owns every receiver-side write to an audit
record after admission: mid-task threshold checkpoints (``--checkpoint``)
and terminal updates (``--final-state``). Hand-edited terminal blocks are
what produced off-enum vocabulary, duplicate live evaluations, and
terminal records still carrying a paused checkpoint action — this module
enforces the pinned enums and cross-field invariants at write time, and
exposes the same checks as a validator (`validate_audit_record`,
`sweep_audit_dir`) for `oacp doctor` and the conformance fixtures.

Exit codes: 0 written (or validated) · 2 usage/validation error ·
4 checkpoint breached — the record is now checkpoint-paused and the §E
re-authorization flow applies before any terminal state can be written.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

import policy_signing
from _oacp_constants import atomic_replace_yaml, locked_audit, utc_now_iso
from _oacp_env import resolve_oacp_home
from autonomy_gate import (
    AUTONOMY_AUDIT_SCHEMA_VERSION,
    BREACH_BASES,
    BREACH_SUB_BASES,
    COVERABLE_CONTINUATION_FIELDS,
    DuplicateKeyError,
    FINAL_STATES,
    LEGACY_PROFILE_BOOL_FIELDS,
    PINNED_COMPLETION_KINDS,
    _actual_side_effects,
    evaluate_threshold_checkpoint,
    evaluation_identity,
    load_yaml_strict,
    receiver_policy,
)
from record_autonomy_outcome import GRANT_DECISIONS, HUMAN_DECISIONS

__all__ = [
    "DuplicateKeyError",
    "apply_checkpoint",
    "finalize_audit_record",
    "load_audit_strict",
    "sweep_audit_dir",
    "validate_audit_record",
]


# The run-state vocabulary, split by lifecycle phase. ``pending`` is the
# receiver-written in-flight value (an admission record picked up for
# execution); the evaluator itself never writes it, so it extends the
# evaluator's FINAL_STATES rather than appearing there.
TERMINAL_FINAL_STATES = frozenset({"done", "superseded", "error"})
LIVE_FINAL_STATES = frozenset({"pending", "paused", "blocked"})
VALID_FINAL_STATES = TERMINAL_FINAL_STATES | LIVE_FINAL_STATES
assert TERMINAL_FINAL_STATES | (LIVE_FINAL_STATES - {"pending"}) == FINAL_STATES

# Canonical mapping for off-enum vocabulary observed in pre-rail records.
# The mapping is documentation for auditors and collectors — history is
# never rewritten. A record carrying one of these is closed by superseding
# it (a state update through this module), not by editing the old value.
LEGACY_COMPLETION_KIND_MAP = {
    "executed": "auto_accepted",
    "review_round_delivered": "auto_accepted",
    "human_approved_completed": "admission_paused",
    "completed_after_human_approval": "admission_paused",
}
LEGACY_FINAL_STATE_MAP = {"completed": "done"}

# Canonical realized-axis names a checkpoint may breach on. Everything the
# evaluator emits is drawn from these; receiver-composed variants (three
# observed spellings for the file axis alone) fragment calibration.
CANONICAL_NUMERIC_AXES = ("actual_minutes", "actual_files_touched")
CANONICAL_CHECKPOINT_AXES = frozenset(CANONICAL_NUMERIC_AXES) | {
    f"side_effects_actual.{field}" for field in COVERABLE_CONTINUATION_FIELDS
} | {
    f"task_profile.{field}"
    for field in (*LEGACY_PROFILE_BOOL_FIELDS, *COVERABLE_CONTINUATION_FIELDS)
}

# Validator finding codes, pinned like reason codes. ``error`` findings are
# integrity violations the fleet drives to zero; ``advisory`` findings are
# ambiguous-by-construction (an auto-accepted record is born
# ``final_state: done`` with null actuals, so missing terminal data cannot
# be distinguished from work still in flight) or purely historical.
FINDING_SEVERITIES = {
    "record_unparsable": "error",
    "duplicate_yaml_key": "error",
    "off_enum_completion_kind": "error",
    "missing_completion_kind": "error",
    "off_enum_final_state": "error",
    "decision_kind_incoherent": "error",
    "paused_terminal_checkpoint_action": "error",
    "paused_terminal_completed": "error",
    "terminal_paused_without_outcome": "error",
    "superseded_missing_successor": "error",
    "breached_empty_fields": "error",
    "off_enum_breach_basis": "error",
    "breach_basis_incoherent": "error",
    "invalid_human_outcome": "error",
    "admission_outcome_replaced_by_checkpoint_clear": "advisory",
    "duplicate_logical_id": "error",
    "noncanonical_checkpoint_axis": "advisory",
    "terminal_missing_actuals": "advisory",
    "terminal_missing_work_started_at": "advisory",
    "canonical_writer_missing_work_started_at": "error",
    "invalid_finalizer_provenance": "error",
    "actual_minutes_inconsistent": "error",
    "work_started_before_admission": "error",
}

# Writer-era marker for checkpoint and non-supersession terminal receipts.
# Historical records have no marker and remain byte-preserved; the validator
# can still flag their missing work clock advisory-only, while any receipt
# claiming this canonical writer must satisfy the current required contract.
FINALIZER_PROVENANCE = {
    "name": "oacp autonomy-finalize",
    "schema_version": 1,
}


# The strict duplicate-key-refusing loader lives in autonomy_gate (shared
# with automatic supersession, which must also fail closed on ambiguous
# YAML); this module re-exports it under its historical name.
load_audit_strict = load_yaml_strict


def _finding(code: str, detail: str) -> Dict[str, str]:
    if code not in FINDING_SEVERITIES:  # pragma: no cover - programming guard
        raise ValueError(f"unpinned finding code: {code}")
    return {"code": code, "severity": FINDING_SEVERITIES[code], "detail": detail}


def _parse_utc(value: Any) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _result_block(record: Dict[str, Any]) -> Dict[str, Any]:
    result = record.get("result")
    return result if isinstance(result, dict) else {}


def _checkpoint_block(record: Dict[str, Any]) -> Dict[str, Any]:
    checkpoint = _result_block(record).get("threshold_checkpoint")
    return checkpoint if isinstance(checkpoint, dict) else {}


def _stamp_finalizer_provenance(result: Dict[str, Any]) -> None:
    existing = result.get("finalizer")
    if existing is not None and existing != FINALIZER_PROVENANCE:
        raise ValueError(
            "result.finalizer conflicts with the canonical finalizer provenance"
        )
    result["finalizer"] = dict(FINALIZER_PROVENANCE)


def _require_work_start(
    record: Dict[str, Any],
    actuals: Dict[str, Any],
    *,
    operation: str,
) -> Dict[str, Any]:
    """Return actuals bound to the record's immutable task-start stamp."""
    normalized = dict(actuals)
    record_started = _result_block(record).get("work_started_at_utc")
    provided_started = normalized.get("work_started_at_utc")
    if (
        record_started is not None
        and provided_started is not None
        and provided_started != record_started
    ):
        raise ValueError(
            "work_started_at_utc conflicts with the record's existing stamp"
        )
    started_at = provided_started or record_started
    if started_at is None:
        raise ValueError(
            f"{operation} requires work_started_at_utc; pass --started-at "
            "or checkpoint the record's task start first"
        )
    started = _parse_utc(started_at)
    if started is None:
        raise ValueError("work_started_at_utc must use YYYY-MM-DDTHH:MM:SSZ")
    admission = _parse_utc(record.get("created_at_utc"))
    if admission is not None and started < admission:
        raise ValueError(
            "work_started_at_utc precedes the admission decision "
            f"({record.get('created_at_utc')})"
        )
    normalized["work_started_at_utc"] = started_at
    return normalized


def _active_minutes(
    record: Dict[str, Any],
    started_at_utc: Any,
    completed_at_utc: Any,
) -> int:
    """Return active wall-clock minutes, rounded up to a whole minute.

    A checkpoint may carry one re-authorization pause interval.  Peer-review
    waits remain inside the wall clock.  A resolved pause excludes the
    explicit ``paused_at_utc`` -> ``cleared_paused_at_utc`` interval; an
    uncleared pause ends the active clock at ``paused_at_utc``.
    """
    started = _parse_utc(started_at_utc)
    completed = _parse_utc(completed_at_utc)
    if started is None or completed is None:
        raise ValueError(
            "work_started_at_utc and completed_at_utc must use "
            "YYYY-MM-DDTHH:MM:SSZ"
        )
    if completed < started:
        raise ValueError("completed_at_utc precedes work_started_at_utc")

    paused_seconds = 0.0
    checkpoint = _checkpoint_block(record)
    paused_text = checkpoint.get("paused_at_utc")
    reauthorization = checkpoint.get("reauthorization")
    cleared_text = (
        reauthorization.get("cleared_paused_at_utc")
        if isinstance(reauthorization, dict)
        else None
    )
    if paused_text is not None:
        paused = _parse_utc(paused_text)
        cleared = completed if cleared_text is None else _parse_utc(cleared_text)
        if paused is None or cleared is None:
            raise ValueError(
                "a re-authorization pause needs parseable paused_at_utc and, "
                "when present, cleared_paused_at_utc timestamps"
            )
        if not (started <= paused <= cleared <= completed):
            raise ValueError(
                "the re-authorization pause interval must fall within the "
                "work-start to completion interval"
            )
        paused_seconds = (cleared - paused).total_seconds()
    elif cleared_text is not None:
        raise ValueError(
            "cleared_paused_at_utc requires a parseable paused_at_utc timestamp"
        )

    active_seconds = (completed - started).total_seconds() - paused_seconds
    if active_seconds < 0:  # pragma: no cover - guarded by interval ordering
        raise ValueError("re-authorization pauses exceed the task wall clock")
    return int(math.ceil(active_seconds / 60.0))


def _is_closed(record: Dict[str, Any]) -> bool:
    """A record is closed once it carries completion evidence.

    ``final_state`` alone cannot answer this: auto-accepted records are
    born ``done`` (conformance-pinned admission shape) with null actuals
    and no completion stamp. ``superseded`` closes unconditionally — its
    authority transferred to the superseding evaluation.
    """
    if _result_block(record).get("final_state") == "superseded":
        return True
    return bool(_result_block(record).get("completed_at_utc"))


def _human_outcome_recorded(record: Dict[str, Any]) -> bool:
    outcome = _result_block(record).get("human_outcome")
    return isinstance(outcome, dict) and outcome.get("recorded") is True


def _checkpoint_resolved(record: Dict[str, Any]) -> bool:
    """True when a breached checkpoint has a governing resumed answer."""
    checkpoint = _checkpoint_block(record)
    if checkpoint.get("breached") is not True:
        return True
    reauth = checkpoint.get("reauthorization")
    if isinstance(reauth, dict) and reauth.get("disposition") == "resumed":
        return True
    # A receiver-side human ruling recorded AFTER the checkpoint pause
    # (via `oacp autonomy-outcome` on the checkpoint-paused record) also
    # clears it: the recorder refuses checkpoint records without a
    # paused_at_utc stamp, so a recorded outcome whose decision time is
    # not before the pause is a checkpoint answer, not the admission one.
    outcome = _result_block(record).get("human_outcome")
    if not isinstance(outcome, dict) or outcome.get("recorded") is not True:
        return False
    if outcome.get("decision") not in {"approved", "modified"}:
        return False
    decided = _parse_utc(outcome.get("decided_at_utc"))
    paused = _parse_utc(checkpoint.get("paused_at_utc"))
    if decided is None or paused is None:
        return False
    return decided >= paused


_EVALUATION_ID_RE = re.compile(r"^eval-[0-9a-f]{16}$")


def validate_audit_record(
    record: Dict[str, Any],
    *,
    source: str = "<record>",
) -> List[Dict[str, str]]:
    """Return pinned integrity findings for one parsed audit record."""
    findings: List[Dict[str, str]] = []
    result = _result_block(record)
    checkpoint = _checkpoint_block(record)
    schema = record.get("schema_version")
    decision = record.get("decision")
    kind = result.get("completion_kind")
    state = result.get("final_state")

    if state == "superseded":
        # Closed history: authority transferred to the successor, and the
        # record left the live corpus — collectors and duplicate detection
        # exclude it, and legacy off-enum vocabulary preserved inside it is
        # exactly what the supersession repair is for. The one invariant
        # that must hold is the successor chain itself.
        successor = record.get("superseded_by_evaluation_id")
        if not isinstance(successor, str) or not _EVALUATION_ID_RE.fullmatch(successor):
            findings.append(_finding(
                "superseded_missing_successor",
                f"{source}: superseded record names no well-formed "
                f"superseded_by_evaluation_id (found {successor!r})",
            ))
        return findings

    if kind is None:
        if isinstance(schema, int) and schema >= AUTONOMY_AUDIT_SCHEMA_VERSION:
            findings.append(_finding(
                "missing_completion_kind",
                f"{source}: schema-v{schema} record has no result.completion_kind",
            ))
    elif kind not in PINNED_COMPLETION_KINDS:
        mapped = LEGACY_COMPLETION_KIND_MAP.get(str(kind))
        hint = (
            f"; canonical mapping: {mapped}" if mapped else "; no canonical mapping"
        )
        findings.append(_finding(
            "off_enum_completion_kind",
            f"{source}: completion_kind {kind!r} is off-enum{hint} — close via "
            "supersession, never by rewriting history",
        ))

    if state not in VALID_FINAL_STATES:
        mapped_state = LEGACY_FINAL_STATE_MAP.get(str(state))
        hint = (
            f"; canonical mapping: {mapped_state}"
            if mapped_state
            else "; no canonical mapping"
        )
        findings.append(_finding(
            "off_enum_final_state",
            f"{source}: final_state {state!r} is off-enum{hint}",
        ))

    if kind in PINNED_COMPLETION_KINDS and decision in {"auto_accepted", "paused"}:
        coherent = {
            "auto_accepted": {"auto_accepted", "checkpoint_paused"},
            "paused": {"admission_paused", "checkpoint_paused", "config_malformed"},
        }[str(decision)]
        if kind not in coherent:
            findings.append(_finding(
                "decision_kind_incoherent",
                f"{source}: decision {decision!r} cannot carry "
                f"completion_kind {kind!r}",
            ))

    completed_at = result.get("completed_at_utc")
    finalizer = result.get("finalizer")
    canonical_writer = finalizer == FINALIZER_PROVENANCE
    if finalizer is not None and not canonical_writer:
        findings.append(_finding(
            "invalid_finalizer_provenance",
            f"{source}: result.finalizer {finalizer!r} is not the pinned "
            "canonical writer marker",
        ))
    if state in TERMINAL_FINAL_STATES:
        if checkpoint.get("action") == "paused_for_reauthorization":
            findings.append(_finding(
                "paused_terminal_checkpoint_action",
                f"{source}: final_state {state!r} with threshold_checkpoint."
                "action still paused_for_reauthorization — reconcile at "
                "finalization",
            ))
        if state == "done" and completed_at:
            if decision == "paused" and not _human_outcome_recorded(record):
                findings.append(_finding(
                    "terminal_paused_without_outcome",
                    f"{source}: paused admission finalized done without a "
                    "recorded human outcome",
                ))
            if kind == "checkpoint_paused" and not _checkpoint_resolved(record):
                findings.append(_finding(
                    "terminal_paused_without_outcome",
                    f"{source}: checkpoint-paused record finalized done "
                    "without a resumed re-authorization or post-pause "
                    "human outcome",
                ))
        if state == "done" and completed_at:
            for key in CANONICAL_NUMERIC_AXES:
                value = result.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    findings.append(_finding(
                        "terminal_missing_actuals",
                        f"{source}: completed record has no usable result.{key}",
                    ))
    elif state in LIVE_FINAL_STATES and completed_at:
        findings.append(_finding(
            "paused_terminal_completed",
            f"{source}: final_state {state!r} but result.completed_at_utc is "
            "set — the run ended without a terminal state",
        ))

    started_at = result.get("work_started_at_utc")
    if started_at is None and completed_at is not None and state in {"done", "error"}:
        code = (
            "canonical_writer_missing_work_started_at"
            if canonical_writer
            else "terminal_missing_work_started_at"
        )
        findings.append(_finding(
            code,
            f"{source}: completed record has no result.work_started_at_utc"
            + (
                " despite claiming the canonical finalizer"
                if canonical_writer
                else " (legacy receipt; flag without rewriting history)"
            ),
        ))
    elif (
        started_at is None
        and canonical_writer
        and checkpoint.get("evaluated") is True
    ):
        findings.append(_finding(
            "canonical_writer_missing_work_started_at",
            f"{source}: checkpoint written by the canonical finalizer has no "
            "result.work_started_at_utc",
        ))
    if started_at is not None:
        started = _parse_utc(started_at)
        admission = _parse_utc(record.get("created_at_utc"))
        if started is None:
            findings.append(_finding(
                "actual_minutes_inconsistent",
                f"{source}: result.work_started_at_utc {started_at!r} is "
                "not a YYYY-MM-DDTHH:MM:SSZ timestamp",
            ))
        else:
            if admission is not None and started < admission:
                findings.append(_finding(
                    "work_started_before_admission",
                    f"{source}: result.work_started_at_utc {started_at!r} "
                    f"precedes admission at {record.get('created_at_utc')!r}",
                ))
            actual_minutes = result.get("actual_minutes")
            if (
                completed_at is not None
                and isinstance(actual_minutes, int)
                and not isinstance(actual_minutes, bool)
            ):
                try:
                    derived_minutes = _active_minutes(
                        record, started_at, completed_at
                    )
                except ValueError as exc:
                    findings.append(_finding(
                        "actual_minutes_inconsistent",
                        f"{source}: cannot validate result.actual_minutes "
                        f"against its work clock — {exc}",
                    ))
                else:
                    if abs(actual_minutes - derived_minutes) > 1:
                        findings.append(_finding(
                            "actual_minutes_inconsistent",
                            f"{source}: result.actual_minutes {actual_minutes} "
                            f"differs from the work clock ({derived_minutes}) "
                            "by more than one minute",
                        ))

    if checkpoint.get("breached") is True and not checkpoint.get("breached_fields"):
        findings.append(_finding(
            "breached_empty_fields",
            f"{source}: threshold_checkpoint.breached is true with an empty "
            "breached_fields list",
        ))
    for list_key in ("breached_fields", "declaration_errors"):
        entries = checkpoint.get(list_key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if str(entry) not in CANONICAL_CHECKPOINT_AXES:
                findings.append(_finding(
                    "noncanonical_checkpoint_axis",
                    f"{source}: threshold_checkpoint.{list_key} entry "
                    f"{entry!r} is not a canonical realized-axis name",
                ))

    # The breach-basis grammar is enforced on read-back, not only when the
    # evaluator writes it: a persisted record is durable ledger evidence,
    # and validate/finalize/doctor must not certify an off-vocabulary or
    # misplaced basis. A null basis on a breached checkpoint is tolerated —
    # records written before the basis existed carry it.
    breached = checkpoint.get("breached") is True
    breached_fields = checkpoint.get("breached_fields")
    declaration_errors = checkpoint.get("declaration_errors")
    breached_axes = {
        str(entry)
        for entries in (breached_fields, declaration_errors)
        if isinstance(entries, list)
        for entry in entries
    }
    basis = checkpoint.get("breach_basis")
    if basis is not None and basis not in BREACH_BASES:
        findings.append(_finding(
            "off_enum_breach_basis",
            f"{source}: threshold_checkpoint.breach_basis {basis!r} is "
            f"off-enum (pinned: {', '.join(BREACH_BASES)})",
        ))
    elif basis is not None and not breached:
        findings.append(_finding(
            "breach_basis_incoherent",
            f"{source}: threshold_checkpoint.breach_basis {basis!r} on an "
            "unbreached checkpoint",
        ))
    elif basis is not None:
        # Mirror the evaluator's breach-source grammar: `declared_intent`
        # labels only a prospective task_profile.* correction with no
        # realized effect and no materialized risk; `realized` labels only
        # realized numeric / side-effect axes. An enum-valid label on the
        # opposite source shape is exactly what the evaluator refuses to
        # write, so read-back refuses to certify it too.
        prospective = sorted(
            axis for axis in breached_axes if axis.startswith("task_profile.")
        )
        realized_axes = sorted(breached_axes.difference(prospective))
        effects = checkpoint.get("side_effects_actual")
        realized_effects = sorted(
            str(key)
            for key, value in (effects.items() if isinstance(effects, dict) else ())
            if value is True
        )
        mismatch: List[str] = []
        if basis == "declared_intent":
            if realized_axes:
                mismatch.append(f"breached fields carry realized axes {realized_axes}")
            if realized_effects:
                mismatch.append(f"side_effects_actual realized {realized_effects}")
            if checkpoint.get("predicted_risk_materialized") is True:
                mismatch.append("predicted_risk_materialized is true")
        elif prospective:
            mismatch.append(
                f"breached fields carry prospective task_profile axes {prospective}"
            )
        if mismatch:
            findings.append(_finding(
                "breach_basis_incoherent",
                f"{source}: threshold_checkpoint.breach_basis {basis!r} does not "
                "match its breach source — " + "; ".join(mismatch),
            ))
    sub_basis = checkpoint.get("breach_sub_basis")
    if sub_basis is not None and sub_basis not in BREACH_SUB_BASES:
        findings.append(_finding(
            "off_enum_breach_basis",
            f"{source}: threshold_checkpoint.breach_sub_basis {sub_basis!r} "
            f"is off-enum (pinned: {', '.join(BREACH_SUB_BASES)})",
        ))
    elif sub_basis is not None:
        misplaced: List[str] = []
        if not breached:
            misplaced.append("checkpoint is not breached")
        if basis != "realized":
            misplaced.append(f"breach_basis is {basis!r}, not realized")
        if "actual_minutes" not in breached_axes:
            misplaced.append("actual_minutes is not among breached_fields")
        if misplaced:
            findings.append(_finding(
                "breach_basis_incoherent",
                f"{source}: threshold_checkpoint.breach_sub_basis "
                f"{sub_basis!r} cannot apply — " + "; ".join(misplaced),
            ))

    outcome = result.get("human_outcome")
    if isinstance(outcome, dict) and outcome.get("recorded") is True:
        problems: List[str] = []
        actor = str(outcome.get("actor") or "")
        if not actor or any(ch.isspace() for ch in actor):
            problems.append("actor must be a non-empty whitespace-free handle")
        if outcome.get("decision") not in HUMAN_DECISIONS:
            problems.append(f"decision {outcome.get('decision')!r} off-enum")
        if _parse_utc(outcome.get("decided_at_utc")) is None:
            problems.append("decided_at_utc unparsable")
        latency = outcome.get("decision_latency_seconds")
        if not isinstance(latency, int) or isinstance(latency, bool) or latency < 0:
            problems.append("decision_latency_seconds must be a non-negative int")
        grant = outcome.get("grant")
        grant_decision = grant.get("decision") if isinstance(grant, dict) else None
        if grant_decision not in GRANT_DECISIONS | {"not_recorded"}:
            problems.append(f"grant.decision {grant_decision!r} off-enum")
        if problems:
            findings.append(_finding(
                "invalid_human_outcome",
                f"{source}: recorded human_outcome invalid — "
                + "; ".join(problems),
            ))
        # Damage signature of a checkpoint clear recorded over the admission
        # outcome: the only surviving human decision on an admission-paused
        # record answers a checkpoint pause. Advisory, not error — records
        # written before the outcome recorder routed checkpoint clears into
        # threshold_checkpoint.reauthorization had no non-destructive way to
        # record the clear, so the shape is sanctioned history there.
        if decision == "paused" and "threshold_checkpoint_breached" in (
            outcome.get("pause_reason_codes") or []
        ):
            findings.append(_finding(
                "admission_outcome_replaced_by_checkpoint_clear",
                f"{source}: the recorded human outcome answers a checkpoint "
                "pause while the admission pause has no surviving outcome — "
                "the admission decision was overwritten by a checkpoint "
                "clear",
            ))

    return findings


def sweep_audit_dir(audit_dir: Path) -> Dict[str, Any]:
    """Validate every record in an audit directory plus cross-record checks.

    Returns ``{"records": {filename: [findings]}, "duplicate_groups":
    [...]}``. Duplicate logical IDs count only records not yet closed as
    ``superseded`` — a superseded stale sibling is the resolved shape.
    """
    per_file: Dict[str, List[Dict[str, str]]] = {}
    live_by_identity: Dict[Tuple[str, str], List[str]] = {}
    parsed: Dict[str, Dict[str, Any]] = {}
    for path in sorted(audit_dir.glob("*.yaml")):
        try:
            record = load_audit_strict(path)
        except DuplicateKeyError as exc:
            per_file[path.name] = [_finding(
                "duplicate_yaml_key", f"{path.name}: {exc}"
            )]
            continue
        except Exception as exc:
            per_file[path.name] = [_finding(
                "record_unparsable", f"{path.name}: {exc}"
            )]
            continue
        parsed[path.name] = record
        per_file[path.name] = validate_audit_record(record, source=path.name)
        identity = (
            str(record.get("receiver") or ""),
            str(record.get("message_id") or ""),
        )
        if all(identity):
            state = _result_block(record).get("final_state")
            if state != "superseded":
                live_by_identity.setdefault(identity, []).append(path.name)

    # Successor chains must resolve strictly: authority transferred to the
    # unique same-identity successor that references this predecessor back.
    # A well-formed but dangling, self-referential, ambiguous, or
    # unrelated-identity successor id is the same orphan shape as a
    # missing one.
    ids_by_value: Dict[str, List[str]] = {}
    for name, record in parsed.items():
        evaluation_id = record.get("evaluation_id")
        if isinstance(evaluation_id, str) and evaluation_id:
            ids_by_value.setdefault(evaluation_id, []).append(name)
    for name, record in parsed.items():
        if _result_block(record).get("final_state") != "superseded":
            continue
        successor = record.get("superseded_by_evaluation_id")
        if not isinstance(successor, str) or not _EVALUATION_ID_RE.fullmatch(successor):
            continue  # flagged by validate_audit_record already
        holders = [
            parsed[holder]
            for holder in ids_by_value.get(successor, [])
            if holder != name
        ]
        link_error = _successor_link_error(record, holders, successor)
        if link_error:
            per_file.setdefault(name, []).append(_finding(
                "superseded_missing_successor", f"{name}: {link_error}"
            ))

    duplicate_groups: List[Dict[str, Any]] = []
    for (receiver, message_id), names in sorted(live_by_identity.items()):
        if len(names) < 2:
            continue
        duplicate_groups.append({
            "receiver": receiver,
            "message_id": message_id,
            "files": names,
        })
        for name in names:
            per_file.setdefault(name, []).append(_finding(
                "duplicate_logical_id",
                f"{name}: {len(names)} live evaluations for "
                f"({receiver}, {message_id}) — supersede the stale ones",
            ))
    return {"records": per_file, "duplicate_groups": duplicate_groups}


def _build_actuals(
    args: argparse.Namespace,
    record: Dict[str, Any],
    *,
    measured_at_utc: str,
) -> Dict[str, Any]:
    if args.actuals is not None:
        data = yaml.safe_load(args.actuals.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{args.actuals} must contain a YAML mapping")
        actuals = dict(data)
    else:
        actuals = {}
        if args.actual_minutes is not None:
            actuals["actual_minutes"] = args.actual_minutes
        if args.actual_files_touched is not None:
            actuals["actual_files_touched"] = args.actual_files_touched
        if args.realized:
            actuals["side_effects_actual"] = {key: True for key in args.realized}
        if args.completed_at:
            actuals["completed_at_utc"] = args.completed_at
        if args.predicted_risk_materialized is not None:
            actuals["predicted_risk_materialized"] = (
                args.predicted_risk_materialized == "true"
            )

    if args.started_at is not None:
        existing = actuals.get("work_started_at_utc")
        if existing is not None and existing != args.started_at:
            raise ValueError(
                "--started-at conflicts with actuals.work_started_at_utc"
            )
        actuals["work_started_at_utc"] = args.started_at

    record_result = _result_block(record)
    record_started = record_result.get("work_started_at_utc")
    supplied_started = actuals.get("work_started_at_utc")
    if (
        record_started is not None
        and supplied_started is not None
        and supplied_started != record_started
    ):
        raise ValueError(
            "--started-at conflicts with result.work_started_at_utc already "
            "stamped in the record"
        )
    if supplied_started is None and record_started is not None:
        actuals["work_started_at_utc"] = record_started

    requires_start = args.checkpoint or args.final_state in {"done", "error"}
    if requires_start:
        actuals = _require_work_start(
            record,
            actuals,
            operation=("checkpoint" if args.checkpoint else "terminal finalization"),
        )

    started_at = actuals.get("work_started_at_utc")
    if started_at is not None:
        started = _parse_utc(started_at)
        admission = _parse_utc(record.get("created_at_utc"))
        if started is None:
            raise ValueError("--started-at must use YYYY-MM-DDTHH:MM:SSZ")
        if admission is not None and started < admission:
            raise ValueError(
                "work_started_at_utc precedes the admission decision "
                f"({record.get('created_at_utc')})"
            )
        if "actual_minutes" not in actuals:
            completed_at = (
                actuals.get("completed_at_utc")
                or record_result.get("completed_at_utc")
                or measured_at_utc
            )
            actuals["actual_minutes"] = _active_minutes(
                record, started_at, completed_at
            )
    return actuals


def _live_siblings(
    audit_path: Path, record: Dict[str, Any]
) -> List[str]:
    """Names of other non-superseded records sharing this logical identity."""
    receiver = str(record.get("receiver") or "")
    message_id = str(record.get("message_id") or "")
    if not receiver or not message_id:
        return []
    names: List[str] = []
    for path in sorted(audit_path.parent.glob("*.yaml")):
        if path.name == audit_path.name:
            continue
        try:
            sibling = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(sibling, dict):
            continue
        if (
            str(sibling.get("receiver") or "") == receiver
            and str(sibling.get("message_id") or "") == message_id
            and _result_block(sibling).get("final_state") != "superseded"
        ):
            names.append(path.name)
    return names


def _predecessor_evaluation_id(record: Dict[str, Any]) -> str:
    """The id a successor must reference — on-disk value or the same
    deterministic identity ``_ensure_evaluation_id`` would stamp."""
    return str(
        record.get("evaluation_id")
        or evaluation_identity(
            str(record.get("receiver") or ""),
            str(record.get("message_id") or ""),
            str(record.get("message_sha256") or ""),
            str(record.get("created_at_utc") or ""),
        )
    )


def _successor_link_error(
    predecessor: Dict[str, Any],
    holders: List[Dict[str, Any]],
    successor_id: str,
) -> Optional[str]:
    """One strict cross-record resolver for finalization and the sweep.

    Authority must transfer to the unique successor of the same logical
    identity, and that successor must reference this predecessor back
    through ``supersedes_evaluation_id`` or ``superseded_evaluation_ids``.
    A record that merely carries the id — unrelated identity, ambiguous
    holders, no back-reference — is the same orphan shape as a missing
    successor.
    """
    if not holders:
        return (
            f"superseded_by_evaluation_id {successor_id} does not resolve "
            "to any other evaluation in this directory"
        )
    if len(holders) > 1:
        return (
            f"superseded_by_evaluation_id {successor_id} is ambiguous — "
            f"{len(holders)} records carry it"
        )
    successor = holders[0]
    pred_identity = (
        str(predecessor.get("receiver") or ""),
        str(predecessor.get("message_id") or ""),
    )
    succ_identity = (
        str(successor.get("receiver") or ""),
        str(successor.get("message_id") or ""),
    )
    if pred_identity != succ_identity:
        return (
            f"successor {successor_id} belongs to a different logical "
            "identity — authority transfers only within the same "
            "(receiver, message_id)"
        )
    pred_id = _predecessor_evaluation_id(predecessor)
    listed = successor.get("superseded_evaluation_ids")
    if successor.get("supersedes_evaluation_id") != pred_id and not (
        isinstance(listed, list) and pred_id in listed
    ):
        return (
            f"successor {successor_id} does not reference this evaluation "
            f"({pred_id}) through supersedes_evaluation_id or "
            "superseded_evaluation_ids"
        )
    return None


def _resolve_successor(
    audit_path: Path, record: Dict[str, Any], successor_id: str
) -> Optional[str]:
    """Error detail when *successor_id* fails strict resolution, else None.

    Discovery is permissive (locating which files claim the id), but only
    strict bytes serve as successor evidence: a candidate that fails the
    duplicate-key-refusing loader is refused outright — a permissively
    loaded duplicate-key record would let whichever duplicate value the
    parser kept decide the back-reference.
    """
    holders: List[Dict[str, Any]] = []
    for path in sorted(audit_path.parent.glob("*.yaml")):
        if path.name == audit_path.name:
            continue
        try:
            candidate = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not (
            isinstance(candidate, dict)
            and candidate.get("evaluation_id") == successor_id
        ):
            continue
        try:
            holders.append(load_audit_strict(path))
        except Exception as exc:
            return (
                f"candidate successor {path.name} carrying {successor_id} "
                f"has ambiguous or unreadable bytes ({exc}) — repair it "
                "before superseding onto it"
            )
    return _successor_link_error(record, holders, successor_id)


def _ensure_evaluation_id(record: Dict[str, Any]) -> None:
    if record.get("evaluation_id"):
        return
    record["evaluation_id"] = evaluation_identity(
        str(record.get("receiver") or ""),
        str(record.get("message_id") or ""),
        str(record.get("message_sha256") or ""),
        str(record.get("created_at_utc") or ""),
    )


def _grant_result(record: Dict[str, Any]) -> Dict[str, Any]:
    grant = record.get("continuation_grant")
    return grant if isinstance(grant, dict) else {}


def apply_checkpoint(
    record: Dict[str, Any],
    actuals: Dict[str, Any],
    *,
    now_utc: Optional[str] = None,
    allow_closed: bool = False,
    policy: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Evaluate and record a §E checkpoint in place; return (record, paused).

    ``policy`` is the receiver's parsed admission policy, threaded into the
    checkpoint evaluation so sender_reply re-authorizations are arbitrated
    under the receiver's caps instead of extending nothing. Without it the
    sender channel stays fail-closed (the receiver_human channel is
    unaffected either way).

    On an unresolved breach the record becomes the in-place
    checkpoint-paused shape the outcome recorder expects:
    ``completion_kind: checkpoint_paused`` (the checkpoint evaluation is
    what updated the result block — this is the evaluator's kind for it,
    not a receiver-composed value), ``final_state: paused``, and a
    ``paused_at_utc`` stamp. A breach arbitrated to ``resumed`` keeps the
    admission kind and run state, mirroring the evaluator's own resumed
    decision shape. Admission-time fields (decision, reason_codes,
    co_occurring_reason_codes, admission_axes, breached) stay untouched as
    history — checkpoint reasons live in ``result.threshold_checkpoint``
    and never replace admission reasons.

    A closed record (completion evidence present, or superseded) is
    refused: a mid-task checkpoint must never reopen terminal state.
    ``allow_closed`` exists solely for the finalizer's explicit
    ``--replace`` correction path.
    """
    if _is_closed(record) and not allow_closed:
        raise ValueError(
            "record is already closed (completed_at_utc set or superseded); "
            "a checkpoint cannot reopen terminal state"
        )
    updated = copy.deepcopy(record)
    envelope = updated.get("scope_envelope")
    if not isinstance(envelope, dict):
        raise ValueError(
            "record carries no scope_envelope; nothing to checkpoint against"
        )
    actuals = _require_work_start(record, actuals, operation="checkpoint")
    checkpoint = evaluate_threshold_checkpoint(
        envelope, _grant_result(updated), actuals, policy=policy
    )
    result = updated.setdefault("result", {})
    breached = checkpoint.get("breached") is True
    reauth = checkpoint.get("reauthorization")
    resumed = isinstance(reauth, dict) and reauth.get("disposition") == "resumed"
    if breached and not resumed:
        if not checkpoint.get("paused_at_utc"):
            checkpoint["paused_at_utc"] = now_utc or utc_now_iso()
        result["completion_kind"] = "checkpoint_paused"
        result["final_state"] = "paused"
    result["threshold_checkpoint"] = checkpoint
    result["actual_minutes"] = checkpoint.get("actual_minutes")
    result["actual_files_touched"] = checkpoint.get("actual_files_touched")
    result["predicted_risk_materialized"] = bool(
        checkpoint.get("predicted_risk_materialized", False)
    )
    if "work_started_at_utc" in actuals:
        result["work_started_at_utc"] = actuals["work_started_at_utc"]
    _stamp_finalizer_provenance(result)
    return updated, breached and not resumed


def _reconcile_resolved_checkpoint(
    updated: Dict[str, Any], actuals: Dict[str, Any]
) -> None:
    """Carry a resolved checkpoint to terminal shape without re-arbitrating.

    A recorded re-authorization is an answered pause; re-evaluating it from
    terminal actuals that lack the original ``reauthorization`` input would
    clobber the recorded answer and re-pause an already-resolved breach.
    But an answered pause covers only what was paused and granted — every
    final axis is still compared against the resolved scope, and any new
    expansion refuses terminal reconciliation: a realized effect not
    covered by the envelope, an accepted grant, the recorded
    re-authorization scope, or the answered pause itself, or a numeric
    beyond a scoped re-authorization budget, must go through a fresh
    ``--checkpoint`` (with re-authorization input) before the record can
    finalize. Within scope, numerics update to the final measurements, the
    complete terminal ``side_effects_actual`` map is persisted, and a
    still-paused action reconciles to ``resumed_after_reauthorization``
    (the six paused-terminal records in the audit corpora are exactly this
    missed reconciliation).
    """
    result = updated.setdefault("result", {})
    checkpoint = result.setdefault("threshold_checkpoint", {})
    envelope = updated.get("scope_envelope")
    envelope = envelope if isinstance(envelope, dict) else {}
    grant = _grant_result(updated)
    grant_scope = grant.get("scope") if grant.get("decision") == "accepted" else None
    grant_scope = grant_scope if isinstance(grant_scope, dict) else {}
    reauth = checkpoint.get("reauthorization")
    reauth = reauth if isinstance(reauth, dict) else {}
    reauth_scope = reauth.get("scope")
    reauth_scope = reauth_scope if isinstance(reauth_scope, dict) else {}
    paused_effects = checkpoint.get("side_effects_actual")
    paused_effects = paused_effects if isinstance(paused_effects, dict) else {}

    final_effects = _actual_side_effects(actuals)
    raw_effects = actuals.get("side_effects_actual") or {}
    uncovered = sorted(
        key
        for key, realized in final_effects.items()
        if realized
        and envelope.get(key) is not True
        and grant_scope.get(key) is not True
        and reauth_scope.get(key) is not True
        and paused_effects.get(key) is not True
    )
    if uncovered:
        fields = ", ".join(f"side_effects_actual.{key}" for key in uncovered)
        raise ValueError(
            f"terminal actuals realize {fields} beyond the resolved "
            "re-authorization — record a fresh checkpoint (--checkpoint "
            "with the new actuals and re-authorization input) before "
            "finalizing"
        )
    # Realized effects are monotonic evidence: a true checkpoint value can
    # never become false at finalization, and terminal actuals that
    # explicitly claim so are contradictory under-reporting.
    contradicted = sorted(
        key
        for key, prior_true in paused_effects.items()
        if prior_true is True and raw_effects.get(key) is False
    )
    if contradicted:
        fields = ", ".join(f"side_effects_actual.{key}" for key in contradicted)
        raise ValueError(
            f"terminal actuals declare {fields} false but the checkpoint "
            "recorded it realized — realized effects are monotonic evidence"
        )
    # Numeric bounds follow the answer's consumption shape: a scoped
    # re-authorization budget stands for the rest of the task up to the
    # budget; a scope-less approval authorizes exactly the extent recorded
    # at the pause it answered and cannot waive later growth.
    numeric_budgets = {
        "actual_minutes": reauth_scope.get("max_actual_minutes"),
        "actual_files_touched": reauth_scope.get("max_actual_files_touched"),
    }
    for key, budget in numeric_budgets.items():
        bound = budget if isinstance(budget, int) else checkpoint.get(key)
        basis = (
            "re-authorized budget"
            if isinstance(budget, int)
            else "extent the scope-less approval cleared"
        )
        final_value = actuals.get(key)
        if (
            isinstance(bound, int)
            and isinstance(final_value, int)
            and not isinstance(final_value, bool)
            and final_value > bound
        ):
            raise ValueError(
                f"terminal {key} {final_value} exceeds the {basis} "
                f"({bound}) — record a fresh checkpoint before finalizing"
            )

    for key in CANONICAL_NUMERIC_AXES:
        if key in actuals:
            checkpoint[key] = actuals[key]
            result[key] = actuals[key]
    merged_effects = {
        key: bool(paused_effects.get(key)) or bool(final_effects.get(key))
        for key in {*paused_effects, *final_effects}
    }
    checkpoint["side_effects_actual"] = merged_effects
    if checkpoint.get("action") == "paused_for_reauthorization":
        checkpoint["action"] = "resumed_after_reauthorization"


def finalize_audit_record(
    audit_path: Path,
    record: Dict[str, Any],
    *,
    final_state: str,
    actuals: Dict[str, Any],
    reply_message_id: Optional[str] = None,
    artifacts: Optional[Sequence[str]] = None,
    superseded_by: Optional[str] = None,
    replace: bool = False,
    now_utc: Optional[str] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Return the finalized record; raises on any integrity violation.

    The second return value is True when a terminal checkpoint breached
    and the record was left checkpoint-paused instead of terminal.
    """
    if final_state not in TERMINAL_FINAL_STATES:
        choices = ", ".join(sorted(TERMINAL_FINAL_STATES))
        raise ValueError(f"final_state must be one of: {choices}")
    schema = record.get("schema_version")
    if schema not in {1, AUTONOMY_AUDIT_SCHEMA_VERSION}:
        raise ValueError("audit.schema_version must be 1 or 2")
    result = _result_block(record)
    kind = result.get("completion_kind")
    current_state = result.get("final_state")
    if superseded_by is not None and final_state != "superseded":
        raise ValueError("--superseded-by is valid only with final_state superseded")
    if final_state == "superseded":
        # The supersession repair is the documented exit for records this
        # module otherwise refuses to touch — off-enum legacy vocabulary,
        # closed history — so it bypasses the enum and closed guards. The
        # two invariants it keeps: never re-supersede (the chain would
        # fork), and always name the successor that inherited authority
        # (an orphan supersession is invisible to doctor and
        # envelope-clear for no benefit).
        if current_state == "superseded":
            raise ValueError("record is already superseded")
        if not isinstance(superseded_by, str) or not _EVALUATION_ID_RE.fullmatch(
            superseded_by
        ):
            raise ValueError(
                "final_state superseded requires --superseded-by naming the "
                "successor evaluation_id (eval-<16 hex>)"
            )
        link_error = _resolve_successor(audit_path, record, superseded_by)
        if link_error:
            # Authority must transfer TO the real replacement: a record
            # that merely carries the id — or none at all — is an orphan
            # chain, invisible to doctor and envelope-clear for no benefit.
            raise ValueError(
                f"--superseded-by refused: {link_error} — record the "
                "successor (same receiver and message_id, referencing this "
                "evaluation through supersedes_evaluation_id or "
                "superseded_evaluation_ids) first"
            )
    else:
        if kind not in PINNED_COMPLETION_KINDS:
            raise ValueError(
                f"refusing to finalize: completion_kind {kind!r} is off-enum "
                "— supersede this record instead of finalizing onto it"
            )
        if current_state not in VALID_FINAL_STATES:
            raise ValueError(
                f"refusing to finalize: final_state {current_state!r} is "
                "off-enum — supersede this record instead"
            )
        if _is_closed(record) and not replace:
            raise ValueError(
                "record is already closed (completed_at_utc set or "
                "superseded); use --replace only for a deliberate correction"
            )

    updated = copy.deepcopy(record)
    if final_state == "superseded" and current_state not in VALID_FINAL_STATES:
        # Preserve the legacy off-enum run state as history — supersession
        # overwrites final_state, and the original value is part of what
        # the repair is documenting.
        updated.setdefault("result", {})["legacy_final_state"] = current_state
    if final_state == "done":
        if record.get("decision") == "paused" and not _human_outcome_recorded(record):
            raise ValueError(
                "terminal ⇒ not paused: a paused admission needs a recorded "
                "human outcome (oacp autonomy-outcome) before it can be done"
            )
        siblings = _live_siblings(audit_path, record)
        if siblings:
            raise ValueError(
                "duplicate logical id: live sibling evaluation(s) "
                f"{', '.join(siblings)} — finalize the stale ones as "
                "superseded first"
            )
        actuals = _require_work_start(
            record, actuals, operation="terminal finalization"
        )
        prior_breach = _checkpoint_block(record).get("breached") is True
        if prior_breach:
            if not _checkpoint_resolved(record):
                raise ValueError(
                    "terminal ⇒ not paused: the breached checkpoint has no "
                    "resumed re-authorization or post-pause human outcome"
                )
            _reconcile_resolved_checkpoint(updated, actuals)
        elif isinstance(updated.get("scope_envelope"), dict):
            # Terminal checkpoint parity: every envelope-bearing record gets
            # a checkpoint evaluation at finalization, so coverage no longer
            # depends on which receiver wrote the record.
            updated, checkpoint_paused = apply_checkpoint(
                updated, actuals, now_utc=now_utc, allow_closed=replace,
                policy=policy,
            )
            if checkpoint_paused:
                return updated, True
        else:
            result_block = updated.setdefault("result", {})
            for key in CANONICAL_NUMERIC_AXES:
                if key in actuals:
                    result_block[key] = actuals[key]
            predicted = actuals.get("predicted_risk_materialized")
            if isinstance(predicted, bool):
                result_block["predicted_risk_materialized"] = predicted
        for key in CANONICAL_NUMERIC_AXES:
            value = updated["result"].get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"finalizing done requires a non-negative integer {key}"
                )
    elif final_state == "error":
        actuals = _require_work_start(
            record, actuals, operation="terminal finalization"
        )

    result_block = updated.setdefault("result", {})
    if "work_started_at_utc" in actuals:
        result_block["work_started_at_utc"] = actuals["work_started_at_utc"]
    completed = (
        actuals.get("completed_at_utc")
        or result_block.get("completed_at_utc")
        or now_utc
        or utc_now_iso()
    )
    if _parse_utc(completed) is None:
        raise ValueError("completed_at_utc must use YYYY-MM-DDTHH:MM:SSZ")
    result_block["final_state"] = final_state
    result_block["completed_at_utc"] = completed
    checkpoint = result_block.get("threshold_checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("completed_at_utc") is None:
        checkpoint["completed_at_utc"] = completed
    if final_state != "done":
        for key in CANONICAL_NUMERIC_AXES:
            if key in actuals:
                result_block[key] = actuals[key]
    if final_state != "superseded":
        _stamp_finalizer_provenance(result_block)
    if reply_message_id is not None:
        result_block["reply_message_id"] = reply_message_id
    else:
        result_block.setdefault("reply_message_id", None)
    if artifacts:
        result_block["artifacts"] = [str(item) for item in artifacts]
    else:
        result_block.setdefault("artifacts", [])
    _ensure_evaluation_id(updated)
    if superseded_by is not None:
        updated["superseded_by_evaluation_id"] = superseded_by

    residual = [
        finding
        for finding in validate_audit_record(updated, source=audit_path.name)
        if finding["severity"] == "error"
    ]
    if residual:
        details = "; ".join(finding["detail"] for finding in residual)
        raise ValueError(f"finalized record fails validation: {details}")
    return updated, False


def _resolve_policy(
    record: Dict[str, Any],
    config_override: Optional[Path],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the receiver policy for re-authorization arbitration.

    Returns ``(policy, error)``. The record's ``policy_path`` is the
    default source; ``--config`` overrides it. The read goes through the
    authorized policy path (`policy_signing.load_authorized_policy`: one
    bounded snapshot, signature/receiver/context binding and enrollment
    downgrade resistance verified, then parsed from those same bytes) —
    policy bytes that fail authorization grant nothing. An unresolvable
    or unauthorized record-carried path degrades to no policy (the sender
    channel then extends nothing — fail closed) with the reason returned
    for surfacing; an explicit override that cannot be resolved or
    authorized raises instead, because the caller asked for exactly that
    config.
    """
    path = config_override or record.get("policy_path")
    if not path:
        return None, "record carries no policy_path"
    receiver = record.get("receiver")
    if not isinstance(receiver, str) or not receiver.strip():
        reason = "record names no receiver to authorize the policy against"
        if config_override is not None:
            raise ValueError(f"--config {path}: {reason}")
        return None, reason
    try:
        loaded, _policy_auth, _raw = policy_signing.load_authorized_policy(
            Path(path),
            resolve_oacp_home(),
            receiver=receiver.strip(),
            kind=policy_signing.POLICY_KIND_RECEIVER_CONFIG,
        )
        _mode, policy = receiver_policy(loaded)
    except Exception as exc:
        if config_override is not None:
            raise ValueError(f"--config {path}: {exc}") from exc
        return None, f"policy_path {path}: {exc}"
    return policy, None


def _presents_sender_reauthorization(actuals: Dict[str, Any]) -> bool:
    reauth = actuals.get("reauthorization")
    return isinstance(reauth, dict) and "sender_reply" in reauth


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_file", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--final-state", choices=sorted(TERMINAL_FINAL_STATES)
    )
    mode.add_argument(
        "--checkpoint",
        action="store_true",
        help="record a mid-task §E checkpoint evaluation instead of a terminal state",
    )
    mode.add_argument(
        "--validate",
        action="store_true",
        help="report integrity findings for the record (or its directory) and write nothing",
    )
    parser.add_argument("--actuals", type=Path, help="§E actuals mapping (YAML)")
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "receiver config for re-authorization arbitration "
            "(default: the record's policy_path)"
        ),
    )
    parser.add_argument("--actual-minutes", type=int)
    parser.add_argument("--actual-files-touched", type=int)
    parser.add_argument(
        "--started-at",
        help=(
            "receiver work start (YYYY-MM-DDTHH:MM:SSZ); derives "
            "actual_minutes when --actual-minutes is omitted"
        ),
    )
    parser.add_argument(
        "--realized",
        action="append",
        choices=sorted(COVERABLE_CONTINUATION_FIELDS),
        help="side effect actually performed (repeatable)",
    )
    parser.add_argument("--reply-message-id")
    parser.add_argument("--artifact", action="append", dest="artifacts")
    parser.add_argument("--completed-at")
    parser.add_argument(
        "--predicted-risk-materialized", choices=("true", "false"), default=None
    )
    parser.add_argument(
        "--superseded-by",
        help="evaluation_id of the superseding evaluation (final_state superseded)",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="with --validate: sweep the record's whole directory",
    )
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.validate:
            if args.sweep:
                report = sweep_audit_dir(args.audit_file.parent if args.audit_file.is_file() else args.audit_file)
            else:
                findings = validate_audit_record(
                    load_audit_strict(args.audit_file),
                    source=args.audit_file.name,
                )
                report = {"records": {args.audit_file.name: findings}}
            errors = sum(
                1
                for findings in report["records"].values()
                for finding in findings
                if finding["severity"] == "error"
            )
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                for name, findings in sorted(report["records"].items()):
                    for finding in findings:
                        print(f"{finding['severity'].upper()}: {finding['detail']}")
                print(f"{errors} error finding(s)")
            return 2 if errors else 0

        with locked_audit(args.audit_file):
            record = load_audit_strict(args.audit_file)
            measured_at_utc = args.completed_at or utc_now_iso()
            actuals = _build_actuals(
                args, record, measured_at_utc=measured_at_utc
            )
            policy, policy_error = _resolve_policy(record, args.config)
            if policy is None and _presents_sender_reauthorization(actuals):
                print(
                    f"WARNING: receiver policy unresolvable ({policy_error}) "
                    "— the sender_reply re-authorization channel extends "
                    "nothing (fail closed); pass --config to resolve it",
                    file=sys.stderr,
                )
            if args.checkpoint:
                updated, paused = apply_checkpoint(
                    record, actuals, now_utc=measured_at_utc, policy=policy
                )
            else:
                updated, paused = finalize_audit_record(
                    args.audit_file,
                    record,
                    final_state=args.final_state,
                    actuals=actuals,
                    reply_message_id=args.reply_message_id,
                    artifacts=args.artifacts,
                    superseded_by=args.superseded_by,
                    replace=args.replace,
                    now_utc=measured_at_utc,
                    policy=policy,
                )
            if not args.dry_run:
                atomic_replace_yaml(args.audit_file, updated)

        result = updated.get("result", {})
        checkpoint = result.get("threshold_checkpoint", {})
        summary = {
            "audit_file": str(args.audit_file),
            "dry_run": args.dry_run,
            "final_state": result.get("final_state"),
            "completion_kind": result.get("completion_kind"),
            "evaluation_id": updated.get("evaluation_id"),
            "checkpoint_action": (
                checkpoint.get("action") if isinstance(checkpoint, dict) else None
            ),
        }
        if args.json:
            print(json.dumps(summary, indent=2))
        elif paused:
            print(
                f"CHECKPOINT PAUSED: {args.audit_file} — "
                "Blocked: autonomy threshold exceeded; notify the sender and "
                "re-authorize per the §E flow before finalizing"
            )
        else:
            print(
                f"OK: {args.audit_file} — {summary['final_state']} "
                f"({summary['completion_kind']})"
            )
        return 4 if paused else 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
