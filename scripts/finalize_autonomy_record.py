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
re-authorization flow applies before successful completion can be written.
On ``--final-state done --replace``, scalar-pause derivation reads the stored
checkpoint first; pass explicit actual_minutes when also changing that pause.
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
    CONTINUATION_SIDE_EFFECT_FIELDS,
    DuplicateKeyError,
    FINAL_STATES,
    LEGACY_PROFILE_BOOL_FIELDS,
    PINNED_COMPLETION_KINDS,
    REAUTH_GOVERNING_CHANNELS,
    _actual_side_effects,
    evaluate_threshold_checkpoint,
    evaluation_identity,
    load_yaml_strict,
    normalize_continuation_scope,
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
# evaluator's birth state for an auto-accepted admission; only terminal
# finalization (this module) moves a record to ``done``.
TERMINAL_FINAL_STATES = frozenset({"done", "superseded", "error", "cancelled"})
LIVE_FINAL_STATES = frozenset({"pending", "paused", "blocked"})
VALID_FINAL_STATES = TERMINAL_FINAL_STATES | LIVE_FINAL_STATES
assert VALID_FINAL_STATES == FINAL_STATES

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
CANONICAL_CHECKPOINT_AXES = (
    frozenset(CANONICAL_NUMERIC_AXES)
    | {f"side_effects_actual.{field}" for field in CONTINUATION_SIDE_EFFECT_FIELDS}
    | {
        f"task_profile.{field}"
        for field in (*LEGACY_PROFILE_BOOL_FIELDS, *CONTINUATION_SIDE_EFFECT_FIELDS)
    }
)

# Validator finding codes, pinned like reason codes. ``error`` findings are
# integrity violations the fleet drives to zero; ``advisory`` findings are
# purely historical: legacy receipts flagged without rewriting history. Work
# in flight is ``final_state: pending``, so a ``done`` receipt missing its
# actuals is a completed record that never recorded them — advisory when an
# unmarked legacy writer produced it, an error when the canonical finalizer
# claims it (the finalizer refuses that shape at write time).
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
    "canonical_writer_missing_actuals": "error",
    "legacy_born_done_unfinalized": "advisory",
    "terminal_missing_work_started_at": "advisory",
    "canonical_writer_missing_work_started_at": "error",
    "invalid_finalizer_provenance": "error",
    "actual_minutes_inconsistent": "error",
    "work_started_before_admission": "error",
    "invalid_cancellation": "error",
    "invalid_clock_adjustments": "error",
    "invalid_pause_intervals": "error",
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


CLOCK_ADJUSTMENT_FIELDS = frozenset({
    "from_utc", "to_utc", "actor", "decided_at_utc", "reason",
})

# A pause interval is a checkpoint pause that a later checkpoint evaluation
# replaced on ``threshold_checkpoint``: the finalizer retires the answered
# scalar pair into ``result.pause_intervals`` so the work clock keeps
# excluding it. Distinct from clock adjustments, which are human-directed
# deductions that were never pauses.
PAUSE_INTERVAL_FIELDS = frozenset({
    "paused_at_utc", "cleared_paused_at_utc", "breached_fields", "channel",
    "decided_at_utc", "recorded_at_utc",
})


def _clock_utc(value: Any, field: str) -> dt.datetime:
    parsed = _parse_utc(value)
    if not isinstance(value, str) or parsed is None or parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError(f"{field} must use YYYY-MM-DDTHH:MM:SSZ")
    return parsed


def _adjustment_intervals(
    record: Dict[str, Any],
    started_at_utc: Any,
    completed_at_utc: Any = None,
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Read attributed, closed intervals even when terminal actuals are absent."""
    result = _result_block(record)
    entries = result.get("clock_adjustments", [])
    if not isinstance(entries, list):
        raise ValueError("clock_adjustments must be a list")
    if not entries:
        return []
    started = _clock_utc(started_at_utc, "work_started_at_utc")
    completed = (
        _clock_utc(completed_at_utc, "completed_at_utc")
        if completed_at_utc is not None else None
    )
    intervals = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != CLOCK_ADJUSTMENT_FIELDS | {"recorded_at_utc"}:
            raise ValueError("each clock adjustment requires interval, actor, decision, reason and writer timestamp only")
        actor = entry["actor"]
        if not isinstance(actor, str) or not actor or any(c.isspace() for c in actor):
            raise ValueError("clock adjustment actor must be a stable nonempty handle")
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise ValueError("clock adjustment reason must be nonempty")
        begin = _clock_utc(entry["from_utc"], "from_utc")
        end = _clock_utc(entry["to_utc"], "to_utc")
        decided = _clock_utc(entry["decided_at_utc"], "decided_at_utc")
        recorded = _clock_utc(entry["recorded_at_utc"], "recorded_at_utc")
        if not (started <= begin < end <= recorded) or decided > recorded:
            raise ValueError("clock adjustment must follow work start and finish before recording; its decision must precede recording")
        if completed is not None and end > completed:
            raise ValueError("clock adjustment extends beyond completion")
        intervals.append((begin, end))
    return intervals


def _with_clock_adjustments(
    record: Dict[str, Any], actuals: Dict[str, Any],
) -> Dict[str, Any]:
    """Append attributed deductions; omission and repeated inputs preserve history.

    The writer stamps new entries. Input cannot rewrite a stored entry's
    provenance; exact repeats are idempotent. No adjustment grants authority
    or changes the checkpoint's re-authorization state.
    """
    updated = copy.deepcopy(record)
    result = updated.setdefault("result", {})
    started = actuals.get("work_started_at_utc", result.get("work_started_at_utc"))
    _adjustment_intervals(record, started)
    if "clock_adjustments" in actuals:
        additions = actuals["clock_adjustments"]
        if not isinstance(additions, list):
            raise ValueError("actuals.clock_adjustments must be a list")
        entries = result.setdefault("clock_adjustments", [])
        for entry in additions:
            if (
                not isinstance(entry, dict)
                or not CLOCK_ADJUSTMENT_FIELDS <= set(entry)
                or set(entry) - (CLOCK_ADJUSTMENT_FIELDS | {"recorded_at_utc"})
            ):
                raise ValueError("clock adjustment input requires from_utc, to_utc, actor, decided_at_utc and reason")
            core = {key: entry[key] for key in CLOCK_ADJUSTMENT_FIELDS}
            prior = next((e for e in entries if all(e[k] == core[k] for k in core)), None)
            if prior is not None:
                if "recorded_at_utc" in entry and entry["recorded_at_utc"] != prior["recorded_at_utc"]:
                    raise ValueError("cannot rewrite clock adjustment provenance")
                continue
            if "recorded_at_utc" in entry:
                raise ValueError("recorded_at_utc is writer-owned for new clock adjustments")
            entries.append(dict(core, recorded_at_utc=utc_now_iso()))
    endpoint = actuals.get("completed_at_utc", result.get("completed_at_utc"))
    _adjustment_intervals(updated, started, endpoint)
    return updated


def _pause_intervals(
    record: Dict[str, Any],
    started_at_utc: Any,
    completed_at_utc: Any = None,
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Read retired checkpoint pauses even when terminal actuals are absent."""
    result = _result_block(record)
    entries = result.get("pause_intervals", [])
    if not isinstance(entries, list):
        raise ValueError("pause_intervals must be a list")
    if not entries:
        return []
    started = _clock_utc(started_at_utc, "work_started_at_utc")
    completed = (
        _clock_utc(completed_at_utc, "completed_at_utc")
        if completed_at_utc is not None else None
    )
    intervals = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != PAUSE_INTERVAL_FIELDS:
            raise ValueError(
                "each pause interval requires paused_at_utc, "
                "cleared_paused_at_utc, breached_fields, channel, "
                "decided_at_utc and recorded_at_utc only"
            )
        fields = entry["breached_fields"]
        if not isinstance(fields, list) or not fields or any(
            not isinstance(field, str) or not field for field in fields
        ):
            raise ValueError(
                "pause interval breached_fields must be a nonempty list of axis names"
            )
        if entry["channel"] not in REAUTH_GOVERNING_CHANNELS:
            raise ValueError(
                "pause interval channel must be a governing re-authorization channel"
            )
        paused = _clock_utc(entry["paused_at_utc"], "paused_at_utc")
        cleared = _clock_utc(entry["cleared_paused_at_utc"], "cleared_paused_at_utc")
        decided = _clock_utc(entry["decided_at_utc"], "decided_at_utc")
        recorded = _clock_utc(entry["recorded_at_utc"], "recorded_at_utc")
        if not (started <= paused <= cleared <= recorded) or decided > recorded:
            raise ValueError(
                "pause interval must follow work start, clear no earlier than "
                "it pauses, and be recorded after its clear and decision"
            )
        if completed is not None and cleared > completed:
            raise ValueError("pause interval extends beyond completion")
        intervals.append((paused, cleared))
    return intervals


def _effective_clear(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The governing answer that cleared the current pause, or ``None``.

    Two representations of an answered checkpoint pause are supported and
    every clock consumer reads both. A checkpoint clear arbitrated into
    ``threshold_checkpoint.reauthorization`` names its own clear time and
    channel. A receiver-side human outcome recorded on the record AFTER the
    pause — the outcome recorder's first-answer route on an auto-accepted
    admission that carried no prior outcome — clears the pause at its
    decision time through the ``receiver_human`` channel. An unanswered
    pause, a declined answer, and an admission outcome decided before the
    pause clear nothing. Only the governing answer confers a clear: a
    clear timestamp the recorder retained beside a later ``declined``
    arbitration is history, and a declined answer recorded at or after a
    human outcome withdraws that outcome's clear.
    """
    checkpoint = result.get("threshold_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    reauth = checkpoint.get("reauthorization")
    reauth = reauth if isinstance(reauth, dict) else {}
    declined = (
        reauth.get("disposition") == "declined" or reauth.get("decision") == "declined"
    )
    if reauth.get("disposition") == "resumed" and reauth.get("cleared_paused_at_utc"):
        return {
            "cleared_paused_at_utc": reauth["cleared_paused_at_utc"],
            "channel": reauth.get("channel"),
            "decided_at_utc": reauth.get("decided_at_utc"),
        }
    if checkpoint.get("breached") is not True:
        return None
    outcome = result.get("human_outcome")
    if not isinstance(outcome, dict) or outcome.get("recorded") is not True:
        return None
    if outcome.get("decision") not in {"approved", "modified"}:
        return None
    decided = _parse_utc(outcome.get("decided_at_utc"))
    paused = _parse_utc(checkpoint.get("paused_at_utc"))
    if decided is None or paused is None or decided < paused:
        return None
    if declined:
        declined_at = _parse_utc(reauth.get("decided_at_utc"))
        if declined_at is None or declined_at >= decided:
            return None
    return {
        "cleared_paused_at_utc": outcome["decided_at_utc"],
        "channel": "receiver_human",
        "decided_at_utc": outcome["decided_at_utc"],
    }


def _current_pause(record: Dict[str, Any]) -> Dict[str, Any]:
    """The record's current breached pause, or an empty mapping.

    Keys: ``paused_at_utc``, ``cleared_paused_at_utc`` (the governing clear
    from either answered representation, ``_effective_clear``; None while
    the pause is unanswered), ``terminal_time`` (True when the breach fired at
    terminal finalization) and ``completed_at_utc`` (the completion a
    terminal-time pause pinned).
    """
    checkpoint = _checkpoint_block(record)
    if checkpoint.get("breached") is not True or not checkpoint.get("paused_at_utc"):
        return {}
    clear = _effective_clear(_result_block(record)) or {}
    return {
        "paused_at_utc": checkpoint.get("paused_at_utc"),
        "cleared_paused_at_utc": clear.get("cleared_paused_at_utc"),
        "terminal_time": checkpoint.get("terminal_time") is True,
        "completed_at_utc": checkpoint.get("completed_at_utc"),
    }


def _pin_terminal_completion(
    record: Dict[str, Any], actuals: Dict[str, Any], *, operation: str,
) -> Dict[str, Any]:
    """Bind actuals to the completion a terminal-time checkpoint pinned.

    A breach at terminal finalization fired when the work was complete:
    the pause begins at that completion and the answer clears exactly the
    pinned extent. Every later write on that pause derives the clock to
    the pinned completion — the wait for the answer lies outside the work
    clock — so a supplied ``completed_at_utc`` that differs is refused.
    """
    pause = _current_pause(record)
    pinned = pause.get("completed_at_utc") if pause.get("terminal_time") else None
    if pinned is None:
        return actuals
    supplied = actuals.get("completed_at_utc")
    if supplied is not None and supplied != pinned:
        raise ValueError(
            f"completed_at_utc {supplied} conflicts with the completion the "
            f"terminal-time checkpoint pinned at {pinned} — omit it for "
            f"{operation}: the wait for the clear lies outside the work clock, "
            "and work after completion needs a fresh admission"
        )
    return dict(actuals, completed_at_utc=pinned)


def _retire_answered_pause(result: Dict[str, Any], now_utc: Optional[str]) -> None:
    """Move an answered scalar pause into ``pause_intervals`` before a new
    checkpoint evaluation replaces the scalar pair.

    Only a breached pause with a governing clear (``_effective_clear``, in
    either representation) is retired; an unanswered or declined pause
    stays the current pause. Retirement is idempotent on ``paused_at_utc``
    and never edits a stored entry.
    """
    checkpoint = result.get("threshold_checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("breached") is not True:
        return
    paused = checkpoint.get("paused_at_utc")
    clear = _effective_clear(result)
    if not paused or clear is None:
        return
    entries = result.setdefault("pause_intervals", [])
    if not isinstance(entries, list):
        raise ValueError("pause_intervals must be a list")
    if any(isinstance(e, dict) and e.get("paused_at_utc") == paused for e in entries):
        return
    entries.append({
        "paused_at_utc": paused,
        "cleared_paused_at_utc": clear["cleared_paused_at_utc"],
        "breached_fields": list(checkpoint.get("breached_fields") or []),
        "channel": clear["channel"],
        "decided_at_utc": clear["decided_at_utc"],
        "recorded_at_utc": now_utc or utc_now_iso(),
    })


def _active_minutes(
    record: Dict[str, Any],
    started_at_utc: Any,
    completed_at_utc: Any,
) -> int:
    """Return active wall-clock minutes, rounded up to a whole minute.

    A checkpoint carries one current re-authorization pause interval; the
    answered pauses that later checkpoints replaced live in
    ``result.pause_intervals``.  Peer-review waits remain inside the wall
    clock.  A resolved pause excludes the explicit ``paused_at_utc`` ->
    ``cleared_paused_at_utc`` interval, the clear taken from either answered
    representation (``_effective_clear``); an uncleared pause ends the
    active clock at ``paused_at_utc``. Attributed clock adjustments are additional
    exclusions. The union of the scalar pause, the retired intervals and
    the adjustments is subtracted in seconds, then rounded once; overlap
    never deducts the same second twice.
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

    intervals = _adjustment_intervals(record, started_at_utc, completed_at_utc)
    intervals.extend(_pause_intervals(record, started_at_utc, completed_at_utc))
    checkpoint = _checkpoint_block(record)
    paused_text = checkpoint.get("paused_at_utc")
    clear = _effective_clear(_result_block(record))
    cleared_text = clear["cleared_paused_at_utc"] if clear else None
    if paused_text is not None:
        paused = _parse_utc(paused_text)
        cleared = completed if cleared_text is None else _parse_utc(cleared_text)
        if paused is None or cleared is None:
            raise ValueError(
                "a re-authorization pause needs parseable paused_at_utc and, "
                "when present, cleared_paused_at_utc timestamps"
            )
        if checkpoint.get("terminal_time") is True:
            # The breach fired at terminal finalization: the pause begins
            # at the recorded completion, and its clear may postdate it
            # without adding work — clamp the exclusion to the endpoint.
            if not (started <= paused <= completed):
                raise ValueError(
                    "a terminal-time pause must begin between work start and "
                    "the recorded completion"
                )
            cleared = min(cleared, completed)
        elif not (started <= paused <= cleared <= completed):
            raise ValueError(
                "the re-authorization pause interval must fall within the "
                "work-start to completion interval"
            )
        intervals.append((paused, cleared))
    elif cleared_text is not None:
        raise ValueError(
            "cleared_paused_at_utc requires a parseable paused_at_utc timestamp"
        )

    excluded_seconds = 0.0
    merged: List[Tuple[dt.datetime, dt.datetime]] = []
    for begin, end in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    for begin, end in merged:
        excluded_seconds += (end - begin).total_seconds()
    active_seconds = (completed - started).total_seconds() - excluded_seconds
    if active_seconds < 0:  # pragma: no cover - guarded by interval ordering
        raise ValueError("re-authorization pauses exceed the task wall clock")
    return int(math.ceil(active_seconds / 60.0))


def _is_closed(record: Dict[str, Any]) -> bool:
    """A record is closed once it carries completion evidence.

    The evidence is ``completed_at_utc``, which only terminal finalization
    writes. An auto-accepted admission is born ``pending`` and stays open
    until then; a ``done`` record with no completion stamp is a legacy
    receipt born ``done`` under the pre-0.5.2 admission shape and is still
    open to exactly one finalization. ``error`` and ``cancelled`` close
    unconditionally, as does ``superseded`` (authority transferred).
    """
    if _result_block(record).get("final_state") in {"superseded", "error", "cancelled"}:
        return True
    return bool(_result_block(record).get("completed_at_utc"))


def _human_outcome_recorded(record: Dict[str, Any]) -> bool:
    outcome = _result_block(record).get("human_outcome")
    return isinstance(outcome, dict) and outcome.get("recorded") is True


def _cancellation_errors(record: Dict[str, Any]) -> List[str]:
    """Validate cancellation evidence identically at write and read-back."""
    result = _result_block(record)
    errors = []
    outcome = result.get("human_outcome")
    admitted = record.get("decision") == "auto_accepted" or (
        record.get("decision") == "paused"
        and isinstance(outcome, dict)
        and outcome.get("recorded") is True
        and outcome.get("decision") in {"approved", "modified"}
    )
    if not admitted:
        errors.append("cancelled requires an admitted task")
    if result.get("cancelled_by") not in ("human", "sender"):
        errors.append("cancelled_by must be human or sender")
    cancelled = _parse_utc(result.get("cancelled_at_utc"))
    completed = _parse_utc(result.get("completed_at_utc"))
    if cancelled is None or completed is None or cancelled != completed:
        errors.append("cancelled_at_utc must be a UTC timestamp equal to completed_at_utc")
    lower = _parse_utc(result.get("work_started_at_utc") or record.get("created_at_utc"))
    if cancelled is not None and lower is not None and cancelled < lower:
        errors.append("cancelled_at_utc precedes admission or work start")
    if isinstance(outcome, dict) and outcome.get("recorded") is True:
        approved_at = _parse_utc(outcome.get("decided_at_utc"))
        if cancelled is not None and approved_at is not None and cancelled < approved_at:
            errors.append("cancelled_at_utc precedes the admission outcome")
    if "reason" in result and not isinstance(result["reason"], str):
        errors.append("reason must be a string when supplied")
    landed = result.get("deliverables_landed")
    if not isinstance(landed, list) or any(
        not isinstance(ref, str) or not ref.strip() for ref in landed
    ):
        errors.append("deliverables_landed must be a list of nonempty reference strings")
    for key in CANONICAL_NUMERIC_AXES:
        value = result.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"cancelled requires a non-negative integer {key}")
    if result.get("work_started_at_utc") is None and (
        result.get("actual_minutes") != 0
        or result.get("actual_files_touched") != 0
        or landed
    ):
        errors.append("cancellation before work start requires zero actuals and no deliverables")
    return errors


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
    # The same resolver feeds the work clock and pause retirement.
    return _effective_clear(_result_block(record)) is not None


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
    if decision == "auto_accepted" and state == "done" and not completed_at:
        # The pre-0.5.2 evaluator wrote auto-accepted admissions born `done`
        # with no completion stamp; `done` is now only ever the finalizer's
        # stamped write, so this shape is a legacy receipt that was never
        # finalized — flagged, never rewritten, still open to one finalize.
        findings.append(_finding(
            "legacy_born_done_unfinalized",
            f"{source}: auto-accepted record born done with no "
            "completed_at_utc — legacy admission shape, never finalized",
        ))
    clock_endpoint = completed_at or (
        checkpoint.get("completed_at_utc") if "clock_adjustments" in result else None
    )
    try:
        _adjustment_intervals(record, result.get("work_started_at_utc"), clock_endpoint)
    except ValueError as exc:
        findings.append(_finding("invalid_clock_adjustments", f"{source}: {exc}"))
    try:
        _pause_intervals(record, result.get("work_started_at_utc"), clock_endpoint)
    except ValueError as exc:
        findings.append(_finding("invalid_pause_intervals", f"{source}: {exc}"))
    if state == "cancelled":
        findings.extend(
            _finding("invalid_cancellation", f"{source}: {detail}")
            for detail in _cancellation_errors(record)
        )
    elif any(key in result for key in ("cancelled_by", "cancelled_at_utc", "deliverables_landed", "reason")):
        findings.append(_finding(
            "invalid_cancellation", f"{source}: cancellation evidence requires final_state cancelled",
        ))
    finalizer = result.get("finalizer")
    canonical_writer = finalizer == FINALIZER_PROVENANCE
    if finalizer is not None and not canonical_writer:
        findings.append(_finding(
            "invalid_finalizer_provenance",
            f"{source}: result.finalizer {finalizer!r} is not the pinned "
            "canonical writer marker",
        ))
    if state in TERMINAL_FINAL_STATES:
        if state != "cancelled" and checkpoint.get("action") == "paused_for_reauthorization":
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
            # In-flight work is `pending`, so a completed record without
            # numeric actuals never recorded them. The canonical finalizer
            # refuses that shape at write time; its marker on such a record
            # is an integrity error, an unmarked one is a legacy receipt.
            for key in CANONICAL_NUMERIC_AXES:
                value = result.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    findings.append(_finding(
                        "canonical_writer_missing_actuals"
                        if canonical_writer
                        else "terminal_missing_actuals",
                        f"{source}: completed record has no usable result.{key}"
                        + (
                            " despite claiming the canonical finalizer"
                            if canonical_writer
                            else " (legacy receipt; flag without rewriting history)"
                        ),
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
                clock_endpoint is not None
                and isinstance(actual_minutes, int)
                and not isinstance(actual_minutes, bool)
            ):
                try:
                    derived_minutes = _active_minutes(
                        record, started_at, clock_endpoint
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

    continuation = record.get("continuation_grant")
    if record.get("scope_envelope_source") == "continuation_grant" or (
        decision == "auto_accepted" and isinstance(continuation, dict)
        and continuation.get("decision") == "accepted"
    ):
        scope = continuation.get("scope") if isinstance(continuation, dict) else None
        normalized, error = normalize_continuation_scope(scope)
        envelope = record.get("scope_envelope")
        invalid = (
            error or not isinstance(envelope, dict)
            or not isinstance(continuation, dict)
            or continuation.get("decision") != "accepted"
            or record.get("message_type") not in (normalized or {}).get("allowed_types", [])
        )
        if not invalid:
            for field, cap in (("estimated_minutes", "max_actual_minutes"),
                               ("expected_files_touched", "max_actual_files_touched")):
                value = envelope.get(field)
                invalid = invalid or not isinstance(value, int) or isinstance(value, bool)
                if isinstance(value, int):
                    invalid = invalid or value < 0 or value > normalized[cap]
            for field in CONTINUATION_SIDE_EFFECT_FIELDS:
                value = envelope.get(field, False)
                invalid = invalid or not isinstance(value, bool) or (value and not normalized[field])
            for field in LEGACY_PROFILE_BOOL_FIELDS:
                value = envelope.get(field)
                invalid = invalid or not isinstance(value, bool)
                if field != "external_side_effects":
                    invalid = invalid or value is True
        if invalid:
            findings.append(_finding(
                "decision_kind_incoherent",
                f"{source}: generic continuation lacks a valid accepted scope/envelope "
                "or its envelope exceeds the grant",
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
        granted_scope = grant.get("granted_scope") if isinstance(grant, dict) else None
        # Old receipts remain readable historical evidence; only scopes
        # claiming the new generic grammar are validated under that grammar.
        if isinstance(granted_scope, dict) and any(
            key in granted_scope
            for key in ("allowed_types", "max_round", "expires_at_utc")
        ):
            _normalized, scope_error = normalize_continuation_scope(granted_scope)
            if scope_error:
                problems.append(f"grant.granted_scope invalid: {scope_error}")
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
        data = load_yaml_strict(args.actuals)
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

    if args.final_state == "superseded":
        if "clock_adjustments" in actuals:
            raise ValueError("supersession cannot add clock adjustments")
        return actuals

    if args.final_state == "cancelled":
        if args.cancelled_at is None:
            raise ValueError("--final-state cancelled requires --cancelled-at")
        if (
            args.completed_at not in (None, args.cancelled_at)
            or actuals.get("completed_at_utc", args.cancelled_at) != args.cancelled_at
        ):
            raise ValueError("--completed-at must equal --cancelled-at")
        actuals["completed_at_utc"] = args.cancelled_at
        previous_files = _result_block(record).get("actual_files_touched")
        if previous_files is not None:
            actuals.setdefault("actual_files_touched", previous_files)
        elif not (_result_block(record).get("work_started_at_utc") or args.started_at or actuals.get("work_started_at_utc")):
            actuals.setdefault("actual_files_touched", 0)

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
        actuals = _pin_terminal_completion(
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
                _with_clock_adjustments(record, actuals), started_at, completed_at
            )
    if args.final_state == "cancelled" and started_at is None:
        previous_minutes = record_result.get("actual_minutes")
        actuals.setdefault("actual_minutes", 0 if previous_minutes is None else previous_minutes)
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
    terminal: bool = False,
) -> Tuple[Dict[str, Any], bool]:
    """Evaluate and record a §E checkpoint in place; return (record, paused).

    ``terminal`` marks the evaluation terminal finalization runs against
    the final actuals: a breach there is a terminal-time checkpoint, which
    pins ``completed_at_utc`` at the breach (the pause begins at completion)
    and stamps ``terminal_time`` on the block.

    The pause stamp is written once, at the breach. Re-evaluating an
    unanswered pause — a resumed ``--checkpoint`` presenting the answer —
    reads the recorded ``paused_at_utc`` (and a terminal-time pause's pinned
    completion); a supplied value that differs is refused. Only a new
    breach after an answered pause stamps afresh.

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
    decision shape. A prior pause that was already answered is retired
    into ``result.pause_intervals`` before the new evaluation replaces the
    scalar pair, so every cleared pause keeps excluding its interval. Admission-time fields (decision, reason_codes,
    co_occurring_reason_codes, admission_axes, breached) stay untouched as
    history — checkpoint reasons live in ``result.threshold_checkpoint``
    and never replace admission reasons.

    A closed record (completion evidence present, or superseded) is
    refused: a mid-task checkpoint must never reopen terminal state.
    ``allow_closed`` exists solely for the finalizer's explicit
    ``--replace`` correction path.
    """
    if _result_block(record).get("final_state") == "cancelled" or (
        _is_closed(record) and not allow_closed
    ):
        raise ValueError(
            "record is already closed (completed_at_utc set or superseded); "
            "a checkpoint cannot reopen terminal state"
        )
    actuals = dict(actuals)
    current = _current_pause(record)
    if current and current["cleared_paused_at_utc"] is None:
        # Stamp-once: the unanswered pause keeps its breach moment across
        # re-evaluation; the answer clears that pause, not a re-stamped one.
        supplied = actuals.get("paused_at_utc")
        if supplied is not None and supplied != current["paused_at_utc"]:
            raise ValueError(
                f"paused_at_utc {supplied} conflicts with the pause already "
                f"stamped at {current['paused_at_utc']} — the stamp is written "
                "once at the breach; omit it to re-evaluate that pause"
            )
        actuals["paused_at_utc"] = current["paused_at_utc"]
        actuals = _pin_terminal_completion(record, actuals, operation="checkpoint")
    elif current.get("terminal_time") and not terminal:
        raise ValueError(
            "the terminal-time checkpoint pinned completion at "
            f"{current['completed_at_utc']} and its pause is answered; a "
            "checkpoint cannot extend work past its recorded completion — "
            "finalize the record (or cancel it)"
        )
    elif terminal:
        # A breach here fires when the work is complete: the pause begins
        # at the measured completion, which the block pins (a closed record
        # under --replace keeps its recorded completion).
        recorded = _result_block(record).get("completed_at_utc") if allow_closed else None
        actuals.setdefault("completed_at_utc", recorded or now_utc or utc_now_iso())
        actuals.setdefault("paused_at_utc", actuals["completed_at_utc"])
    has_adjustments = (
        "clock_adjustments" in actuals or "clock_adjustments" in _result_block(record)
    )
    if has_adjustments:
        endpoint = _result_block(record).get("completed_at_utc") if allow_closed else None
        actuals.setdefault("completed_at_utc", endpoint or now_utc or utc_now_iso())
    updated = _with_clock_adjustments(record, actuals)
    envelope = updated.get("scope_envelope")
    if not isinstance(envelope, dict):
        raise ValueError(
            "record carries no scope_envelope; nothing to checkpoint against"
        )
    # The evaluation below replaces the scalar pause pair; an answered pause
    # is retired first so the clock keeps excluding it (an unanswered one
    # stays current).
    _retire_answered_pause(updated.setdefault("result", {}), now_utc)
    actuals = _require_work_start(record, actuals, operation="checkpoint")
    if has_adjustments:
        derived_minutes = _active_minutes(
            updated, actuals["work_started_at_utc"],
            actuals["completed_at_utc"],
        )
        actuals.setdefault("actual_minutes", derived_minutes)
        supplied_minutes = actuals["actual_minutes"]
        if isinstance(supplied_minutes, int) and abs(supplied_minutes - derived_minutes) > 1:
            raise ValueError("actual_minutes_inconsistent: checkpoint differs from its work clock")
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
    if breached and (terminal or current.get("terminal_time")):
        # The marker follows the pause it describes: stamped at a
        # terminal-time breach, carried across the re-evaluation that
        # answers it.
        checkpoint["terminal_time"] = True
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
    cancellation: Optional[Dict[str, Any]] = None,
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
    if final_state == "cancelled":
        if _is_closed(record):
            raise ValueError("record is already closed; cancellation cannot replace terminal history")
        supported_actuals = {
            "actual_minutes", "actual_files_touched", "work_started_at_utc",
            "completed_at_utc", "clock_adjustments",
        }
        if set(actuals) - supported_actuals:
            raise ValueError(
                "cancellation accepts clock/count actuals only; checkpoint "
                "effects, risks and re-authorization while the record is live"
            )
        if not isinstance(cancellation, dict):
            raise ValueError("cancelled requires cancellation metadata")
        allowed = {"cancelled_by", "cancelled_at_utc", "reason", "deliverables_landed"}
        if set(cancellation) - allowed:
            raise ValueError("unsupported cancellation metadata")
        if actuals.get("completed_at_utc", cancellation.get("cancelled_at_utc")) != cancellation.get("cancelled_at_utc"):
            raise ValueError("completed_at_utc must equal cancelled_at_utc")
        actuals = dict(actuals, completed_at_utc=cancellation.get("cancelled_at_utc"))
        if result.get("work_started_at_utc") is not None or actuals.get("work_started_at_utc") is not None:
            actuals = _require_work_start(record, actuals, operation="cancellation")
    elif cancellation is not None:
        raise ValueError("cancellation metadata is valid only with final_state cancelled")
    if current_state == "cancelled" and final_state != "superseded":
        raise ValueError("cancelled record is closed; cannot reopen terminal history")
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

    if final_state == "superseded" and "clock_adjustments" in actuals:
        raise ValueError("supersession preserves history; it cannot add clock adjustments")
    if final_state in {"done", "error"}:
        # Bind to the completion a terminal-time checkpoint pinned before
        # anything reads the endpoint: the adjustment-interval validation and
        # the derived clock below both consume it, so a caller that omits the
        # completion (as documented) lands on the pin rather than on now.
        actuals = _pin_terminal_completion(
            record, actuals, operation="terminal finalization"
        )
    updated = copy.deepcopy(record) if final_state == "superseded" else _with_clock_adjustments(record, actuals)
    if final_state != "superseded" and "clock_adjustments" in _result_block(updated):
        started = actuals.get("work_started_at_utc") or result.get("work_started_at_utc")
        if "actual_minutes" not in actuals and started is not None:
            actuals = dict(actuals, actual_minutes=_active_minutes(
                updated, started,
                actuals.get("completed_at_utc") or result.get("completed_at_utc") or now_utc or utc_now_iso(),
            ))
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
                policy=policy, terminal=True,
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
    if cancellation is not None:
        result_block.update(copy.deepcopy(cancellation))
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
        choices=sorted(CONTINUATION_SIDE_EFFECT_FIELDS),
        help="side effect actually performed (repeatable)",
    )
    parser.add_argument("--reply-message-id")
    parser.add_argument("--artifact", action="append", dest="artifacts")
    parser.add_argument("--completed-at")
    parser.add_argument("--cancelled-by", choices=("human", "sender"))
    parser.add_argument("--cancelled-at", help="cancellation time (YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--reason", help="optional cancellation explanation")
    parser.add_argument("--deliverable-landed", action="append", default=[])
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
        cancellation = None
        if args.final_state == "cancelled":
            cancellation = {
                "cancelled_by": args.cancelled_by,
                "cancelled_at_utc": args.cancelled_at,
                "deliverables_landed": args.deliverable_landed,
            }
            if args.reason is not None:
                cancellation["reason"] = args.reason
        elif args.cancelled_by is not None or args.cancelled_at is not None or args.reason is not None or args.deliverable_landed:
            raise ValueError("cancellation options require --final-state cancelled")
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
                    cancellation=cancellation,
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
