# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for the autonomy audit terminal finalizer and validator."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from finalize_autonomy_record import (  # noqa: E402
    CANONICAL_CHECKPOINT_AXES,
    DuplicateKeyError,
    FINALIZER_PROVENANCE,
    _predecessor_evaluation_id,
    apply_checkpoint,
    finalize_audit_record,
    load_audit_strict,
    main,
    sweep_audit_dir,
    validate_audit_record,
)
from autonomy_gate import evaluate_threshold_checkpoint  # noqa: E402
from oacp_doctor import check_autonomy  # noqa: E402


ENVELOPE = {
    "estimated_minutes": 45,
    "expected_files_touched": 5,
    "risk_tier": "P1",
    "target_repo": "acme/widgets",
    "destructive_ops": False,
    "external_side_effects": True,
    "touches_auth_config_or_secrets": False,
    "touches_dependencies": False,
    "public_visibility": False,
    "creates_or_updates_pr": True,
    "comments_on_github": False,
    "commits_changes": True,
    "merges_pr": False,
    "files_issues": False,
    "sends_oacp_reply_only": False,
    "continuation_grants": {},
}


def _human_outcome(decided_at: str = "2026-08-01T01:10:00Z") -> Dict[str, Any]:
    return {
        "recorded": True,
        "actor": "alice",
        "decision": "approved",
        "decided_at_utc": decided_at,
        "decision_latency_seconds": 600,
        "pause_reason_codes": ["expected_files_touched_exceeds_threshold"],
        "grant": {"decision": "not_requested"},
    }


def _record(
    *,
    decision: str = "paused",
    completion_kind: str = "admission_paused",
    final_state: str = "paused",
    message_id: str = "msg-20260801010000-alice-0001",
    human_outcome: Optional[Dict[str, Any]] = None,
    envelope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "schema_version": 2,
        "spec_version": "0.4.3",
        "created_at_utc": "2026-08-01T01:00:00Z",
        "receiver": "claude",
        "sender": "alice",
        "message_id": message_id,
        "message_sha256": "c0ffee",
        "decision": decision,
        "mode": "auto_review",
        "reason_codes": ["expected_files_touched_exceeds_threshold"],
        "scope_envelope": dict(envelope) if envelope is not None else dict(ENVELOPE),
        "continuation_grant": {"decision": "not_present", "scope": None},
        "result": {
            "final_state": final_state,
            "completion_kind": completion_kind,
            "actual_minutes": None,
            "actual_files_touched": None,
            "predicted_risk_materialized": False,
            "completed_at_utc": None,
            "envelope_enforcement": "none",
        },
    }
    if human_outcome is not None:
        record["result"]["human_outcome"] = human_outcome
    return record


def _write(tmp_path: Path, record: Dict[str, Any], name: str = "20260801T010000Z_a.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return path


# ── strict loading ────────────────────────────────────────────────────────


def test_strict_loader_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text(
        "schema_version: 2\nlogged_notes:\n- code: x\nlogged_notes: []\n",
        encoding="utf-8",
    )
    with pytest.raises(DuplicateKeyError):
        load_audit_strict(path)


# ── validator ─────────────────────────────────────────────────────────────


def test_clean_finalized_record_validates_clean() -> None:
    record = _record(human_outcome=_human_outcome())
    record["result"].update({
        "final_state": "done",
        "actual_minutes": 20,
        "actual_files_touched": 2,
        "work_started_at_utc": "2026-08-01T01:40:00Z",
        "completed_at_utc": "2026-08-01T02:00:00Z",
    })
    assert validate_audit_record(record) == []


def test_optional_pause_evidence_preserves_v2_validation_and_doctor(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "sample"
    audit_dir = project / "agents" / "claude" / "audit" / "autonomy_decisions"
    audit_dir.mkdir(parents=True)
    record = _record(human_outcome=_human_outcome())
    record["result"]["human_outcome"]["decision"] = "modified"
    audit_path = _write(audit_dir, record)
    assert validate_audit_record(record) == []
    before = check_autonomy(project)
    record.update({
        "pause_classification": "designed",
        "expected_pause_codes": ["expected_files_touched_exceeds_threshold"],
        "unplanned_pause_codes": [],
    })
    record["result"]["human_outcome"]["modification"] = {
        "task_profile": {"expected_files_touched": 10}, "note": "Permit extra files",
    }
    audit_path.write_text(yaml.safe_dump(record))
    assert validate_audit_record(record) == []
    assert check_autonomy(project) == before


def test_validator_flags_legacy_terminal_without_work_start() -> None:
    record = _record(human_outcome=_human_outcome())
    record["result"].update({
        "final_state": "done",
        "actual_minutes": 20,
        "actual_files_touched": 2,
        "completed_at_utc": "2026-08-01T02:00:00Z",
    })

    findings = validate_audit_record(record)

    assert findings == [{
        "code": "terminal_missing_work_started_at",
        "severity": "advisory",
        "detail": (
            "<record>: completed record has no result.work_started_at_utc "
            "(legacy receipt; flag without rewriting history)"
        ),
    }]


def test_validator_rejects_canonical_receipt_without_work_start() -> None:
    record = _record(human_outcome=_human_outcome())
    record["result"].update({
        "final_state": "done",
        "actual_minutes": 20,
        "actual_files_touched": 2,
        "completed_at_utc": "2026-08-01T02:00:00Z",
        "finalizer": dict(FINALIZER_PROVENANCE),
    })

    findings = validate_audit_record(record)

    assert [finding["code"] for finding in findings] == [
        "canonical_writer_missing_work_started_at"
    ]


def test_validator_rejects_invalid_finalizer_provenance() -> None:
    record = _record()
    record["result"]["finalizer"] = {
        "name": "local yaml edit",
        "schema_version": 1,
    }

    findings = validate_audit_record(record)

    assert [finding["code"] for finding in findings] == [
        "invalid_finalizer_provenance"
    ]


def test_off_enum_completion_kind_maps_legacy_vocabulary() -> None:
    record = _record(completion_kind="executed")
    findings = validate_audit_record(record)
    codes = [finding["code"] for finding in findings]
    assert "off_enum_completion_kind" in codes
    detail = next(
        finding["detail"]
        for finding in findings
        if finding["code"] == "off_enum_completion_kind"
    )
    assert "auto_accepted" in detail


def test_off_enum_final_state_flagged_with_mapping() -> None:
    record = _record(final_state="completed")
    findings = validate_audit_record(record)
    codes = [finding["code"] for finding in findings]
    assert "off_enum_final_state" in codes


def test_superseded_final_state_is_pinned_valid() -> None:
    record = _record(final_state="superseded")
    record["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    record["superseded_by_evaluation_id"] = "eval-0123456789abcdef"
    assert validate_audit_record(record) == []


def test_superseded_without_successor_flagged() -> None:
    record = _record(final_state="superseded")
    record["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    codes = [finding["code"] for finding in validate_audit_record(record)]
    assert codes == ["superseded_missing_successor"]


def test_pending_final_state_is_valid_live_state() -> None:
    record = _record(decision="auto_accepted", completion_kind="auto_accepted", final_state="pending")
    assert validate_audit_record(record) == []


def test_terminal_with_paused_checkpoint_action_flagged() -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="checkpoint_paused",
        final_state="done",
        human_outcome=_human_outcome(),
    )
    record["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    record["result"]["actual_minutes"] = 50
    record["result"]["actual_files_touched"] = 6
    record["result"]["threshold_checkpoint"] = {
        "evaluated": True,
        "breached": True,
        "breached_fields": ["actual_files_touched"],
        "declaration_errors": [],
        "paused_at_utc": "2026-08-01T01:30:00Z",
        "action": "paused_for_reauthorization",
    }
    codes = [finding["code"] for finding in validate_audit_record(record)]
    assert "paused_terminal_checkpoint_action" in codes


def test_live_state_with_completed_stamp_flagged() -> None:
    record = _record(final_state="paused")
    record["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    codes = [finding["code"] for finding in validate_audit_record(record)]
    assert "paused_terminal_completed" in codes


def test_done_paused_admission_without_outcome_flagged() -> None:
    record = _record(final_state="done")
    record["result"].update({
        "completed_at_utc": "2026-08-01T02:00:00Z",
        "actual_minutes": 10,
        "actual_files_touched": 1,
    })
    codes = [finding["code"] for finding in validate_audit_record(record)]
    assert "terminal_paused_without_outcome" in codes


def test_breached_with_empty_fields_flagged() -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="checkpoint_paused",
        final_state="paused",
    )
    record["result"]["threshold_checkpoint"] = {
        "evaluated": True,
        "breached": True,
        "breached_fields": [],
        "declaration_errors": [],
        "paused_at_utc": "2026-08-01T01:30:00Z",
        "action": "paused_for_reauthorization",
    }
    codes = [finding["code"] for finding in validate_audit_record(record)]
    assert "breached_empty_fields" in codes


def test_noncanonical_axis_names_are_advisory() -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="checkpoint_paused",
        final_state="paused",
    )
    record["result"]["threshold_checkpoint"] = {
        "evaluated": True,
        "breached": True,
        "breached_fields": ["files_touched_actual"],
        "declaration_errors": [],
        "paused_at_utc": "2026-08-01T01:30:00Z",
        "action": "paused_for_reauthorization",
    }
    findings = validate_audit_record(record)
    axis = [f for f in findings if f["code"] == "noncanonical_checkpoint_axis"]
    assert axis and axis[0]["severity"] == "advisory"


def test_invalid_human_outcome_flagged() -> None:
    outcome = _human_outcome()
    outcome["decision"] = "acknowledged"
    outcome["actor"] = "a b"
    record = _record(human_outcome=outcome)
    codes = [finding["code"] for finding in validate_audit_record(record)]
    assert "invalid_human_outcome" in codes


def test_decision_kind_incoherence_flagged() -> None:
    record = _record(decision="auto_accepted", completion_kind="admission_paused")
    codes = [finding["code"] for finding in validate_audit_record(record)]
    assert "decision_kind_incoherent" in codes


def test_canonical_axes_cover_evaluator_vocabulary() -> None:
    assert "actual_minutes" in CANONICAL_CHECKPOINT_AXES
    assert "side_effects_actual.merges_pr" in CANONICAL_CHECKPOINT_AXES
    assert "task_profile.external_side_effects" in CANONICAL_CHECKPOINT_AXES


# ── sweep ─────────────────────────────────────────────────────────────────


def test_sweep_flags_duplicate_live_evaluations(tmp_path: Path) -> None:
    first = _record(human_outcome=_human_outcome())
    second = _record(human_outcome=_human_outcome())
    _write(tmp_path, first, "20260801T010000Z_a.yaml")
    _write(tmp_path, second, "20260801T020000Z_b.yaml")
    report = sweep_audit_dir(tmp_path)
    assert len(report["duplicate_groups"]) == 1
    assert report["duplicate_groups"][0]["files"] == [
        "20260801T010000Z_a.yaml",
        "20260801T020000Z_b.yaml",
    ]


def test_sweep_superseded_sibling_resolves_duplicate(tmp_path: Path) -> None:
    first = _record(human_outcome=_human_outcome())
    second = _record(final_state="superseded")
    second["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    _write(tmp_path, first, "20260801T010000Z_a.yaml")
    _write(tmp_path, second, "20260801T020000Z_b.yaml")
    report = sweep_audit_dir(tmp_path)
    assert report["duplicate_groups"] == []


def test_sweep_reports_duplicate_yaml_key(tmp_path: Path) -> None:
    (tmp_path / "dup.yaml").write_text(
        "schema_version: 2\nlogged_notes:\n- code: x\nlogged_notes: []\n",
        encoding="utf-8",
    )
    report = sweep_audit_dir(tmp_path)
    codes = [f["code"] for f in report["records"]["dup.yaml"]]
    assert codes == ["duplicate_yaml_key"]


# ── checkpoint recording ──────────────────────────────────────────────────


def test_checkpoint_within_envelope_keeps_run_state() -> None:
    record = _record(decision="auto_accepted", completion_kind="auto_accepted", final_state="done")
    updated, paused = apply_checkpoint(
        record,
        {
            "actual_minutes": 10,
            "actual_files_touched": 2,
            "work_started_at_utc": "2026-08-01T01:20:00Z",
        },
    )
    assert paused is False
    assert updated["result"]["completion_kind"] == "auto_accepted"
    assert updated["result"]["threshold_checkpoint"]["action"] == "within_declared_envelope"


def test_checkpoint_breach_writes_paused_shape() -> None:
    record = _record(decision="auto_accepted", completion_kind="auto_accepted", final_state="done")
    updated, paused = apply_checkpoint(
        record,
        {
            "actual_minutes": 90,
            "actual_files_touched": 2,
            "work_started_at_utc": "2026-08-01T01:20:00Z",
        },
    )
    assert paused is True
    result = updated["result"]
    assert result["final_state"] == "paused"
    assert result["completion_kind"] == "checkpoint_paused"
    checkpoint = result["threshold_checkpoint"]
    assert checkpoint["breached_fields"] == ["actual_minutes"]
    assert checkpoint["paused_at_utc"]
    assert checkpoint["action"] == "paused_for_reauthorization"


def test_checkpoint_requires_envelope() -> None:
    record = _record()
    record["scope_envelope"] = None
    with pytest.raises(ValueError, match="scope_envelope"):
        apply_checkpoint(record, {"actual_minutes": 1, "actual_files_touched": 0})


# ── finalization ──────────────────────────────────────────────────────────


def test_finalize_done_records_terminal_checkpoint(tmp_path: Path) -> None:
    record = _record(human_outcome=_human_outcome())
    path = _write(tmp_path, record)
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="done",
        actuals={
            "actual_minutes": 20,
            "actual_files_touched": 3,
            "work_started_at_utc": "2026-08-01T01:40:00Z",
            "completed_at_utc": "2026-08-01T02:00:00Z",
            "side_effects_actual": {"creates_or_updates_pr": True, "commits_changes": True},
        },
        reply_message_id="msg-20260801020000-claude-9999",
    )
    assert paused is False
    result = updated["result"]
    assert result["final_state"] == "done"
    assert result["completion_kind"] == "admission_paused"
    assert result["actual_minutes"] == 20
    assert result["actual_files_touched"] == 3
    assert result["completed_at_utc"]
    assert result["reply_message_id"] == "msg-20260801020000-claude-9999"
    assert result["threshold_checkpoint"]["action"] == "within_declared_envelope"
    assert updated["evaluation_id"].startswith("eval-")
    assert validate_audit_record(updated) == []


def test_finalize_done_refuses_unapproved_paused_admission(tmp_path: Path) -> None:
    record = _record()
    path = _write(tmp_path, record)
    with pytest.raises(ValueError, match="human outcome"):
        finalize_audit_record(
            path,
            record,
            final_state="done",
            actuals={"actual_minutes": 5, "actual_files_touched": 1},
        )


def test_finalize_done_pauses_on_terminal_breach(tmp_path: Path) -> None:
    record = _record(human_outcome=_human_outcome())
    path = _write(tmp_path, record)
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="done",
        actuals={
            "actual_minutes": 20,
            "actual_files_touched": 9,
            "work_started_at_utc": "2026-08-01T01:40:00Z",
        },
    )
    assert paused is True
    assert updated["result"]["final_state"] == "paused"
    assert updated["result"]["completion_kind"] == "checkpoint_paused"


def test_finalize_done_refuses_undeclared_realized_side_effect(tmp_path: Path) -> None:
    record = _record(human_outcome=_human_outcome())
    path = _write(tmp_path, record)
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="done",
        actuals={
            "actual_minutes": 5,
            "actual_files_touched": 1,
            "work_started_at_utc": "2026-08-01T01:55:00Z",
            "side_effects_actual": {"merges_pr": True},
        },
    )
    assert paused is True
    checkpoint = updated["result"]["threshold_checkpoint"]
    assert "side_effects_actual.merges_pr" in checkpoint["breached_fields"]


def test_finalize_reconciles_resolved_checkpoint(tmp_path: Path) -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="checkpoint_paused",
        final_state="paused",
    )
    record["result"]["threshold_checkpoint"] = {
        "evaluated": True,
        "actual_minutes": 50,
        "actual_files_touched": 3,
        "side_effects_actual": {},
        "breached": True,
        "breached_fields": ["actual_minutes"],
        "declaration_errors": [],
        "breach_basis": "realized",
        "paused_at_utc": "2026-08-01T02:00:00Z",
        "action": "paused_for_reauthorization",
        "reauthorization": {
            "presented": True,
            "channel": "receiver_human",
            "decision": "approved",
            "disposition": "resumed",
        },
    }
    record["result"]["work_started_at_utc"] = "2026-08-01T01:10:00Z"
    path = _write(tmp_path, record)
    # A scope-less approval covers exactly the extent recorded at the
    # pause, so the terminal actuals may not exceed it.
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="done",
        actuals={"actual_minutes": 50, "actual_files_touched": 3},
    )
    assert paused is False
    result = updated["result"]
    assert result["final_state"] == "done"
    assert result["actual_minutes"] == 50
    checkpoint = result["threshold_checkpoint"]
    assert checkpoint["action"] == "resumed_after_reauthorization"
    assert validate_audit_record(updated) == []


def test_finalize_done_refuses_unresolved_checkpoint(tmp_path: Path) -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="checkpoint_paused",
        final_state="paused",
    )
    record["result"]["threshold_checkpoint"] = {
        "evaluated": True,
        "breached": True,
        "breached_fields": ["actual_minutes"],
        "declaration_errors": [],
        "paused_at_utc": "2026-08-01T01:30:00Z",
        "action": "paused_for_reauthorization",
        "reauthorization": {"presented": False, "disposition": "unanswered"},
    }
    path = _write(tmp_path, record)
    with pytest.raises(ValueError, match="terminal"):
        finalize_audit_record(
            path,
            record,
            final_state="done",
            actuals={"actual_minutes": 50, "actual_files_touched": 3},
        )


def test_finalize_done_refuses_live_duplicate_sibling(tmp_path: Path) -> None:
    record = _record(human_outcome=_human_outcome())
    sibling = _record()
    path = _write(tmp_path, record, "20260801T010000Z_a.yaml")
    _write(tmp_path, sibling, "20260801T020000Z_b.yaml")
    with pytest.raises(ValueError, match="duplicate logical id"):
        finalize_audit_record(
            path,
            record,
            final_state="done",
            actuals={"actual_minutes": 5, "actual_files_touched": 1},
        )


def _write_successor(
    tmp_path: Path,
    evaluation_id: str = "eval-0123456789abcdef",
    supersedes: Optional[str] = None,
) -> Path:
    # Strict resolution: the successor shares the predecessor's logical
    # identity (same _record() fields) and references it back.
    successor = _record()
    successor["evaluation_id"] = evaluation_id
    successor["supersedes_evaluation_id"] = (
        supersedes if supersedes is not None
        else _predecessor_evaluation_id(_record())
    )
    return _write(tmp_path, successor, "20260801T030000Z_successor.yaml")


def test_finalize_superseded_closes_stale_sibling(tmp_path: Path) -> None:
    record = _record()
    path = _write(tmp_path, record)
    _write_successor(tmp_path)
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="superseded",
        actuals={},
        superseded_by="eval-0123456789abcdef",
    )
    assert paused is False
    assert updated["result"]["final_state"] == "superseded"
    assert updated["superseded_by_evaluation_id"] == "eval-0123456789abcdef"
    assert updated["result"]["completed_at_utc"]
    assert validate_audit_record(updated) == []


def test_finalize_refuses_off_enum_completion_kind(tmp_path: Path) -> None:
    record = _record(completion_kind="executed")
    path = _write(tmp_path, record)
    with pytest.raises(ValueError, match="off-enum"):
        finalize_audit_record(
            path,
            record,
            final_state="done",
            actuals={"actual_minutes": 5, "actual_files_touched": 1},
        )


def test_finalize_refuses_closed_record_without_replace(tmp_path: Path) -> None:
    record = _record(human_outcome=_human_outcome())
    record["result"].update({
        "final_state": "done",
        "actual_minutes": 5,
        "actual_files_touched": 1,
        "completed_at_utc": "2026-08-01T02:00:00Z",
    })
    path = _write(tmp_path, record)
    with pytest.raises(ValueError, match="already closed"):
        finalize_audit_record(
            path,
            record,
            final_state="done",
            actuals={"actual_minutes": 5, "actual_files_touched": 1},
        )


def test_finalize_error_state_allowed_without_outcome(tmp_path: Path) -> None:
    record = _record()
    path = _write(tmp_path, record)
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="error",
        actuals={
            "actual_minutes": 3,
            "actual_files_touched": 0,
            "work_started_at_utc": "2026-08-01T01:57:00Z",
            "completed_at_utc": "2026-08-01T02:00:00Z",
        },
    )
    assert paused is False
    assert updated["result"]["final_state"] == "error"
    assert updated["result"]["actual_minutes"] == 3


# ── CLI ───────────────────────────────────────────────────────────────────


def test_cli_finalizes_done(tmp_path: Path) -> None:
    record = _record(human_outcome=_human_outcome())
    path = _write(tmp_path, record)
    rc = main([
        str(path),
        "--final-state", "done",
        "--actual-minutes", "20",
        "--actual-files-touched", "3",
        "--started-at", "2026-08-01T01:40:00Z",
        "--completed-at", "2026-08-01T02:00:00Z",
        "--realized", "creates_or_updates_pr",
        "--realized", "commits_changes",
        "--reply-message-id", "msg-20260801020000-claude-9999",
    ])
    assert rc == 0
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert stored["result"]["final_state"] == "done"
    assert stored["result"]["reply_message_id"] == "msg-20260801020000-claude-9999"
    assert stored["result"]["finalizer"] == FINALIZER_PROVENANCE


def test_cli_done_without_work_start_fails_without_mutation(tmp_path: Path) -> None:
    record = _record(human_outcome=_human_outcome())
    path = _write(tmp_path, record)
    original = path.read_bytes()

    rc = main([
        str(path),
        "--final-state", "done",
        "--actual-minutes", "1",
        "--actual-files-touched", "0",
        "--completed-at", "2026-08-01T02:00:00Z",
    ])

    assert rc == 2
    assert path.read_bytes() == original


def test_cli_checkpoint_without_work_start_fails_without_mutation(
    tmp_path: Path,
) -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="auto_accepted",
        final_state="pending",
    )
    path = _write(tmp_path, record)
    original = path.read_bytes()

    rc = main([
        str(path),
        "--checkpoint",
        "--actual-minutes", "1",
        "--actual-files-touched", "0",
    ])

    assert rc == 2
    assert path.read_bytes() == original


def test_cli_recorded_start_fallback_derives_serialized_item_minutes(
    tmp_path: Path,
) -> None:
    envelope = dict(ENVELOPE)
    envelope["estimated_minutes"] = 30
    record = _record(
        decision="auto_accepted",
        completion_kind="auto_accepted",
        final_state="done",
        envelope=envelope,
    )
    record["result"]["work_started_at_utc"] = "2026-08-01T01:20:00Z"
    path = _write(tmp_path, record)

    rc = main([
        str(path),
        "--final-state", "done",
        "--completed-at", "2026-08-01T01:45:00Z",
        "--actual-files-touched", "1",
    ])

    assert rc == 0
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    result = stored["result"]
    assert result["work_started_at_utc"] == "2026-08-01T01:20:00Z"
    assert result["actual_minutes"] == 25
    assert result["threshold_checkpoint"]["breached"] is False


def test_cli_replace_without_actuals_preserves_recorded_work_clock(
    tmp_path: Path,
) -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="auto_accepted",
        final_state="done",
    )
    record.pop("scope_envelope")
    record["result"].update({
        "actual_minutes": 18,
        "actual_files_touched": 3,
        "work_started_at_utc": "2026-08-01T01:42:00Z",
        "completed_at_utc": "2026-08-01T02:00:00Z",
        "reply_message_id": "msg-20260801020000-claude-old",
    })
    path = _write(tmp_path, record)

    rc = main([
        str(path),
        "--final-state", "done",
        "--replace",
        "--reply-message-id", "msg-20260801020500-claude-new",
    ])

    assert rc == 0
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    result = stored["result"]
    assert result["actual_minutes"] == 18
    assert result["actual_files_touched"] == 3
    assert result["work_started_at_utc"] == "2026-08-01T01:42:00Z"
    assert result["completed_at_utc"] == "2026-08-01T02:00:00Z"
    assert result["reply_message_id"] == "msg-20260801020500-claude-new"


def test_cli_started_at_conflict_with_record_stamp_is_refused(
    tmp_path: Path,
) -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="auto_accepted",
        final_state="done",
    )
    record["result"]["work_started_at_utc"] = "2026-08-01T01:20:00Z"
    path = _write(tmp_path, record)
    original = path.read_bytes()

    rc = main([
        str(path),
        "--final-state", "done",
        "--started-at", "2026-08-01T01:21:00Z",
        "--completed-at", "2026-08-01T01:45:00Z",
        "--actual-files-touched", "1",
    ])

    assert rc == 2
    assert path.read_bytes() == original


def test_cli_started_at_excludes_reauthorization_pause(tmp_path: Path) -> None:
    record = _resolved_checkpoint_record()
    record["result"].pop("work_started_at_utc")
    checkpoint = _gate_checkpoint("approved")
    record["result"]["threshold_checkpoint"] = checkpoint
    assert checkpoint["reauthorization"]["cleared_paused_at_utc"] == (
        "2026-08-01T01:40:00Z"
    )
    path = _write(tmp_path, record)

    rc = main([
        str(path),
        "--final-state", "done",
        "--started-at", "2026-08-01T01:20:00Z",
        "--completed-at", "2026-08-01T02:00:00Z",
        "--actual-files-touched", "3",
    ])

    assert rc == 0
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert stored["result"]["actual_minutes"] == 30


def test_uncleared_pause_conformance_record_finalizes_error(tmp_path: Path) -> None:
    record = _resolved_checkpoint_record()
    record["result"].pop("work_started_at_utc")
    checkpoint = _gate_checkpoint("declined")
    record["result"]["threshold_checkpoint"] = checkpoint
    assert checkpoint["action"] == "reauthorization_declined"
    assert checkpoint["reauthorization"]["cleared_paused_at_utc"] is None
    path = _write(tmp_path, record)

    rc = main([
        str(path),
        "--final-state", "error",
        "--started-at", "2026-08-01T01:20:00Z",
        "--completed-at", "2026-08-01T02:00:00Z",
        "--actual-files-touched", "3",
    ])

    assert rc == 0
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert stored["result"]["actual_minutes"] == 10
    assert validate_audit_record(stored) == []


def test_validator_reports_inconsistent_work_clock() -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="auto_accepted",
        final_state="done",
    )
    record["result"].update({
        "work_started_at_utc": "2026-08-01T00:55:00Z",
        "actual_minutes": 25,
        "actual_files_touched": 1,
        "completed_at_utc": "2026-08-01T01:45:00Z",
    })

    codes = [finding["code"] for finding in validate_audit_record(record)]

    assert codes == [
        "work_started_before_admission",
        "actual_minutes_inconsistent",
    ]


def test_cli_checkpoint_breach_exits_4(tmp_path: Path) -> None:
    record = _record(decision="auto_accepted", completion_kind="auto_accepted", final_state="done")
    path = _write(tmp_path, record)
    rc = main([
        str(path),
        "--checkpoint",
        "--actual-minutes", "90",
        "--actual-files-touched", "2",
        "--started-at", "2026-08-01T01:20:00Z",
    ])
    assert rc == 4
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert stored["result"]["final_state"] == "paused"
    assert stored["result"]["completion_kind"] == "checkpoint_paused"


def test_cli_validate_reports_findings(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    record = _record(completion_kind="executed")
    path = _write(tmp_path, record)
    rc = main([str(path), "--validate"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "off_enum" in captured.out or "off-enum" in captured.out


def test_cli_validate_flags_legacy_missing_work_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    record = _record(human_outcome=_human_outcome())
    record["result"].update({
        "final_state": "done",
        "actual_minutes": 1,
        "actual_files_touched": 0,
        "completed_at_utc": "2026-08-01T02:00:00Z",
    })
    path = _write(tmp_path, record)

    rc = main([str(path), "--validate", "--json"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "terminal_missing_work_started_at" in captured.out


def test_cli_validate_sweep_clean_dir(tmp_path: Path) -> None:
    record = _record(human_outcome=_human_outcome())
    path = _write(tmp_path, record)
    rc = main([str(path), "--validate", "--sweep"])
    assert rc == 0


# ── round-1 review regressions (F-002..F-005) ─────────────────────────────


@pytest.mark.parametrize("closed_shape", ["done", "error", "superseded"])
def test_checkpoint_refuses_closed_records(tmp_path: Path, closed_shape: str) -> None:
    record = _record(
        decision="auto_accepted",
        completion_kind="auto_accepted",
        final_state=closed_shape,
    )
    if closed_shape == "superseded":
        record["superseded_by_evaluation_id"] = "eval-0123456789abcdef"
    else:
        record["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    path = _write(tmp_path, record)
    original = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="cannot reopen"):
        apply_checkpoint(record, {"actual_minutes": 90, "actual_files_touched": 2})
    rc = main([
        str(path),
        "--checkpoint",
        "--actual-minutes", "90",
        "--actual-files-touched", "2",
    ])
    assert rc == 2
    assert path.read_text(encoding="utf-8") == original


def _resolved_checkpoint_record(reauth_scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    record = _record(
        decision="auto_accepted",
        completion_kind="checkpoint_paused",
        final_state="paused",
    )
    reauth: Dict[str, Any] = {
        "presented": True,
        "channel": "receiver_human",
        "decision": "approved",
        "decided_at_utc": "2026-08-01T02:10:00Z",
        "disposition": "resumed",
    }
    if reauth_scope is not None:
        reauth["scope"] = reauth_scope
    record["result"]["threshold_checkpoint"] = {
        "evaluated": True,
        "actual_minutes": 50,
        "actual_files_touched": 3,
        "side_effects_actual": {},
        "breached": True,
        "breached_fields": ["actual_minutes"],
        "declaration_errors": [],
        "breach_basis": "realized",
        "paused_at_utc": "2026-08-01T02:00:00Z",
        "action": "paused_for_reauthorization",
        "reauthorization": reauth,
    }
    record["result"]["work_started_at_utc"] = "2026-08-01T01:10:00Z"
    return record


def _gate_checkpoint(decision: str) -> Dict[str, Any]:
    return evaluate_threshold_checkpoint(
        ENVELOPE,
        {"present": False},
        {
            "actual_minutes": 50,
            "actual_files_touched": 3,
            "paused_at_utc": "2026-08-01T01:30:00Z",
            "reauthorization": {
                "receiver_human": {
                    "decision": decision,
                    "decided_at_utc": "2026-08-01T01:40:00Z",
                },
            },
        },
        policy={"thresholds": {}},
    )


# ── read-back enforcement of the breach-basis grammar ─────────────────────


def _finalized_time_breach_record(tmp_path: Path) -> Dict[str, Any]:
    record = _resolved_checkpoint_record()
    path = _write(tmp_path, record)
    done, paused = finalize_audit_record(
        path,
        record,
        final_state="done",
        actuals={"actual_minutes": 50, "actual_files_touched": 3},
    )
    assert paused is False
    assert validate_audit_record(done) == []
    return done


def _clean_done_record() -> Dict[str, Any]:
    record = _record(human_outcome=_human_outcome())
    record["result"].update({
        "final_state": "done",
        "actual_minutes": 20,
        "actual_files_touched": 2,
        "work_started_at_utc": "2026-08-01T01:40:00Z",
        "completed_at_utc": "2026-08-01T02:00:00Z",
        "threshold_checkpoint": {
            "evaluated": True,
            "actual_minutes": 20,
            "actual_files_touched": 2,
            "side_effects_actual": {},
            "breached": False,
            "breached_fields": [],
            "declaration_errors": [],
            "breach_basis": None,
            "breach_sub_basis": None,
            "paused_at_utc": None,
            "action": "not_evaluated",
        },
    })
    assert validate_audit_record(record) == []
    return record


def test_validator_accepts_waiting_on_peer_on_realized_time_breach(tmp_path: Path) -> None:
    done = _finalized_time_breach_record(tmp_path)
    done["result"]["threshold_checkpoint"]["breach_sub_basis"] = "waiting_on_peer"
    assert validate_audit_record(done) == []


def test_validator_tolerates_legacy_null_basis_on_breach(tmp_path: Path) -> None:
    # Records written before breach_basis existed carry null on a breach;
    # they stay finding-free rather than turning the fleet sweep red.
    done = _finalized_time_breach_record(tmp_path)
    done["result"]["threshold_checkpoint"]["breach_basis"] = None
    assert validate_audit_record(done) == []


@pytest.mark.parametrize(
    ("base", "patch", "code", "fragment"),
    [
        ("breached", {"breach_sub_basis": "reviewing"}, "off_enum_breach_basis", "breach_sub_basis 'reviewing'"),
        ("breached", {"breach_basis": "guessed"}, "off_enum_breach_basis", "breach_basis 'guessed'"),
        (
            "breached",
            {"breach_sub_basis": "waiting_on_peer", "breached_fields": ["actual_files_touched"]},
            "breach_basis_incoherent",
            "actual_minutes",
        ),
        (
            "breached",
            {"breach_sub_basis": "waiting_on_peer", "breach_basis": "declared_intent"},
            "breach_basis_incoherent",
            "not realized",
        ),
        ("clean", {"breach_sub_basis": "waiting_on_peer"}, "breach_basis_incoherent", "not breached"),
        ("clean", {"breach_basis": "realized"}, "breach_basis_incoherent", "unbreached"),
        # enum-valid label on the opposite breach-source shape
        (
            "breached",
            {"breach_basis": "declared_intent"},
            "breach_basis_incoherent",
            "realized axes ['actual_minutes']",
        ),
        (
            "breached",
            {
                "breach_basis": "realized",
                "breached_fields": ["task_profile.merges_pr"],
                "declaration_errors": ["task_profile.merges_pr"],
            },
            "breach_basis_incoherent",
            "prospective task_profile axes ['task_profile.merges_pr']",
        ),
        (
            "breached",
            {
                "breach_basis": "realized",
                "breached_fields": ["actual_minutes"],
                "declaration_errors": ["task_profile.merges_pr"],
            },
            "breach_basis_incoherent",
            "prospective task_profile axes",
        ),
        (
            "breached",
            {
                "breach_basis": "declared_intent",
                "breached_fields": ["task_profile.merges_pr"],
                "declaration_errors": ["task_profile.merges_pr"],
                "side_effects_actual": {"merges_pr": True},
            },
            "breach_basis_incoherent",
            "side_effects_actual realized ['merges_pr']",
        ),
        (
            "breached",
            {
                "breach_basis": "declared_intent",
                "breached_fields": ["task_profile.merges_pr"],
                "declaration_errors": ["task_profile.merges_pr"],
                "predicted_risk_materialized": True,
            },
            "breach_basis_incoherent",
            "predicted_risk_materialized is true",
        ),
    ],
)
def test_validator_rejects_persisted_off_grammar_basis(
    tmp_path: Path, base: str, patch: Dict[str, Any], code: str, fragment: str
) -> None:
    record = _finalized_time_breach_record(tmp_path) if base == "breached" else _clean_done_record()
    record["result"]["threshold_checkpoint"].update(patch)
    findings = validate_audit_record(record)
    # Every finding carries the expected code (a doubly-incoherent shape may
    # legitimately report it twice), never a different one.
    assert findings and {finding["code"] for finding in findings} == {code}, findings
    assert all(finding["severity"] == "error" for finding in findings)
    assert fragment in " | ".join(finding["detail"] for finding in findings)


def test_validator_accepts_coherent_declared_intent_shape(tmp_path: Path) -> None:
    # The prospective shape the evaluator writes: task_profile.* fields,
    # every realized effect false, materialization pinned false.
    done = _finalized_time_breach_record(tmp_path)
    done["result"]["threshold_checkpoint"].update({
        "breach_basis": "declared_intent",
        "breached_fields": ["task_profile.merges_pr"],
        "declaration_errors": ["task_profile.merges_pr"],
        "side_effects_actual": {"merges_pr": False},
        "predicted_risk_materialized": False,
    })
    assert validate_audit_record(done) == []


def test_declared_intent_checkpoint_round_trips_through_read_back() -> None:
    record = _record(
        decision="auto_accepted", completion_kind="auto_accepted", final_state="pending"
    )
    updated, paused = apply_checkpoint(
        record,
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "work_started_at_utc": "2026-08-01T01:20:00Z",
            "declared_intent_fields": ["task_profile.merges_pr"],
            "paused_at_utc": "2026-08-01T01:30:00Z",
        },
    )
    assert paused is True
    checkpoint = updated["result"]["threshold_checkpoint"]
    assert checkpoint["breach_basis"] == "declared_intent"
    assert checkpoint["breached_fields"] == ["task_profile.merges_pr"]
    assert validate_audit_record(updated) == []


def test_finalize_refuses_stored_off_enum_sub_basis(tmp_path: Path) -> None:
    # The reconciliation path must not certify malformed stored evidence.
    record = _resolved_checkpoint_record()
    record["result"]["threshold_checkpoint"]["breach_sub_basis"] = "reviewing"
    path = _write(tmp_path, record)
    with pytest.raises(ValueError, match="breach_sub_basis 'reviewing'"):
        finalize_audit_record(
            path,
            record,
            final_state="done",
            actuals={"actual_minutes": 50, "actual_files_touched": 3},
        )


def test_checkpoint_round_trips_waiting_on_peer_through_read_back() -> None:
    # What the evaluator writes for a peer-wait time breach validates clean
    # on read-back; the two sides of the grammar agree.
    record = _record(
        decision="auto_accepted", completion_kind="auto_accepted", final_state="pending"
    )
    updated, paused = apply_checkpoint(
        record,
        {
            "actual_minutes": 60,
            "actual_files_touched": 1,
            "work_started_at_utc": "2026-08-01T01:20:00Z",
            "breach_sub_basis": "waiting_on_peer",
            "paused_at_utc": "2026-08-01T01:30:00Z",
        },
    )
    assert paused is True
    checkpoint = updated["result"]["threshold_checkpoint"]
    assert checkpoint["breached_fields"] == ["actual_minutes"]
    assert checkpoint["breach_basis"] == "realized"
    assert checkpoint["breach_sub_basis"] == "waiting_on_peer"
    assert validate_audit_record(updated) == []
    with pytest.raises(ValueError, match="breach_sub_basis"):
        apply_checkpoint(
            record,
            {
                "actual_minutes": 60,
                "actual_files_touched": 1,
                "work_started_at_utc": "2026-08-01T01:20:00Z",
                "breach_sub_basis": "reviewing",
            },
        )


def test_reconcile_refuses_newly_realized_uncovered_effect(tmp_path: Path) -> None:
    record = _resolved_checkpoint_record()
    path = _write(tmp_path, record)
    with pytest.raises(ValueError, match="side_effects_actual.merges_pr"):
        finalize_audit_record(
            path,
            record,
            final_state="done",
            actuals={
                "actual_minutes": 55,
                "actual_files_touched": 4,
                "side_effects_actual": {"merges_pr": True},
            },
        )


def test_reconcile_refuses_numeric_beyond_reauthorized_budget(tmp_path: Path) -> None:
    record = _resolved_checkpoint_record(
        reauth_scope={"max_actual_minutes": 60, "max_actual_files_touched": 5}
    )
    path = _write(tmp_path, record)
    with pytest.raises(ValueError, match="exceeds the re-authorized budget"):
        finalize_audit_record(
            path,
            record,
            final_state="done",
            actuals={"actual_minutes": 65, "actual_files_touched": 4},
        )


def test_reconcile_persists_complete_terminal_side_effects(tmp_path: Path) -> None:
    record = _resolved_checkpoint_record()
    path = _write(tmp_path, record)
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="done",
        actuals={
            "actual_minutes": 50,
            "actual_files_touched": 3,
            "side_effects_actual": {
                "creates_or_updates_pr": True,
                "commits_changes": True,
            },
        },
    )
    assert paused is False
    checkpoint = updated["result"]["threshold_checkpoint"]
    assert checkpoint["side_effects_actual"]["creates_or_updates_pr"] is True
    assert checkpoint["side_effects_actual"]["commits_changes"] is True
    assert checkpoint["side_effects_actual"]["merges_pr"] is False
    assert checkpoint["action"] == "resumed_after_reauthorization"
    assert validate_audit_record(updated) == []


def test_reconcile_scope_less_numeric_growth_refused(tmp_path: Path) -> None:
    record = _resolved_checkpoint_record()
    path = _write(tmp_path, record)
    for grown in (55, 500):
        with pytest.raises(ValueError, match="scope-less approval cleared"):
            finalize_audit_record(
                path,
                record,
                final_state="done",
                actuals={"actual_minutes": grown, "actual_files_touched": 3},
            )


def test_reconcile_scoped_budget_allows_growth_within_budget(tmp_path: Path) -> None:
    record = _resolved_checkpoint_record(
        reauth_scope={"max_actual_minutes": 60, "max_actual_files_touched": 5}
    )
    record["result"]["work_started_at_utc"] = "2026-08-01T01:05:00Z"
    path = _write(tmp_path, record)
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="done",
        actuals={"actual_minutes": 55, "actual_files_touched": 4},
    )
    assert paused is False
    assert updated["result"]["actual_minutes"] == 55


def test_reconcile_prior_realized_effect_is_monotonic(tmp_path: Path) -> None:
    record = _resolved_checkpoint_record()
    record["result"]["threshold_checkpoint"]["side_effects_actual"] = {
        "merges_pr": True
    }
    record["result"]["threshold_checkpoint"]["breached_fields"] = [
        "side_effects_actual.merges_pr"
    ]
    path = _write(tmp_path, record)
    # Terminal actuals that omit the effect must carry the recorded true
    # forward, never rewrite it to false.
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="done",
        actuals={"actual_minutes": 50, "actual_files_touched": 3},
    )
    assert paused is False
    checkpoint = updated["result"]["threshold_checkpoint"]
    assert checkpoint["side_effects_actual"]["merges_pr"] is True
    # An explicit terminal false against a recorded true is contradictory
    # under-reporting and refuses.
    fresh = _resolved_checkpoint_record()
    fresh["result"]["threshold_checkpoint"]["side_effects_actual"] = {
        "merges_pr": True
    }
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_path = _write(fresh_dir, fresh)
    with pytest.raises(ValueError, match="monotonic evidence"):
        finalize_audit_record(
            fresh_path,
            fresh,
            final_state="done",
            actuals={
                "actual_minutes": 50,
                "actual_files_touched": 3,
                "side_effects_actual": {"merges_pr": False},
            },
        )


def test_supersede_repairs_off_enum_legacy_record(tmp_path: Path) -> None:
    record = _record(completion_kind="executed", final_state="completed")
    record["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    path = _write(tmp_path, record)
    _write_successor(tmp_path)
    rc = main([
        str(path),
        "--final-state", "superseded",
        "--superseded-by", "eval-0123456789abcdef",
    ])
    assert rc == 0
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert stored["result"]["final_state"] == "superseded"
    assert stored["result"]["completion_kind"] == "executed"
    assert stored["result"]["legacy_final_state"] == "completed"
    assert stored["superseded_by_evaluation_id"] == "eval-0123456789abcdef"
    assert validate_audit_record(stored) == []


def test_supersede_requires_wellformed_resolvable_successor(tmp_path: Path) -> None:
    record = _record()
    path = _write(tmp_path, record)
    original = path.read_text(encoding="utf-8")
    assert main([str(path), "--final-state", "superseded"]) == 2
    assert main([
        str(path), "--final-state", "superseded", "--superseded-by", "not-an-id"
    ]) == 2
    # Well-formed but resolving to no record in the directory: an orphan
    # chain, refused the same way.
    assert main([
        str(path),
        "--final-state", "superseded",
        "--superseded-by", "eval-deadbeefdeadbeef",
    ]) == 2
    assert path.read_text(encoding="utf-8") == original


def test_supersede_refuses_unrelated_identity_successor(tmp_path: Path) -> None:
    """A record merely carrying the id is not a successor.

    An otherwise valid evaluation for a different (receiver, message_id)
    must not satisfy successor resolution — authority never transfers
    across logical identities.
    """
    record = _record()
    path = _write(tmp_path, record)
    unrelated = _record()
    unrelated["receiver"] = "other-receiver"
    unrelated["message_id"] = "msg-20260801010000-alice-unrelated"
    unrelated["evaluation_id"] = "eval-bbbbbbbbbbbbbbbb"
    unrelated["supersedes_evaluation_id"] = _predecessor_evaluation_id(record)
    _write(tmp_path, unrelated, "20260801T030000Z_unrelated.yaml")
    original = path.read_text(encoding="utf-8")
    rc = main([
        str(path),
        "--final-state", "superseded",
        "--superseded-by", "eval-bbbbbbbbbbbbbbbb",
    ])
    assert rc == 2
    assert path.read_text(encoding="utf-8") == original


def test_supersede_refuses_ambiguous_successor(tmp_path: Path) -> None:
    record = _record()
    path = _write(tmp_path, record)
    _write_successor(tmp_path)
    duplicate = _record()
    duplicate["evaluation_id"] = "eval-0123456789abcdef"
    duplicate["supersedes_evaluation_id"] = _predecessor_evaluation_id(record)
    _write(tmp_path, duplicate, "20260801T040000Z_duplicate_holder.yaml")
    original = path.read_text(encoding="utf-8")
    rc = main([
        str(path),
        "--final-state", "superseded",
        "--superseded-by", "eval-0123456789abcdef",
    ])
    assert rc == 2
    assert path.read_text(encoding="utf-8") == original


def test_supersede_refuses_successor_without_back_reference(tmp_path: Path) -> None:
    record = _record()
    path = _write(tmp_path, record)
    _write_successor(tmp_path, supersedes="eval-1111111111111111")
    original = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="does not reference this evaluation"):
        finalize_audit_record(
            path,
            record,
            final_state="superseded",
            actuals={},
            superseded_by="eval-0123456789abcdef",
        )
    assert path.read_text(encoding="utf-8") == original


def test_supersede_refuses_duplicate_key_successor(tmp_path: Path) -> None:
    """Ambiguous successor evidence is refused before the predecessor closes.

    A permissive loader would let the later of two duplicate
    supersedes_evaluation_id keys decide the back-reference; only strict
    bytes may serve as successor evidence.
    """
    record = _record()
    path = _write(tmp_path, record)
    pred_id = _predecessor_evaluation_id(record)
    successor = _record()
    successor["evaluation_id"] = "eval-0123456789abcdef"
    successor["supersedes_evaluation_id"] = "eval-1111111111111111"
    text = yaml.safe_dump(successor, sort_keys=False)
    text += f"supersedes_evaluation_id: {pred_id}\n"
    (tmp_path / "20260801T030000Z_successor.yaml").write_text(
        text, encoding="utf-8"
    )
    original = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous or unreadable bytes"):
        finalize_audit_record(
            path,
            record,
            final_state="superseded",
            actuals={},
            superseded_by="eval-0123456789abcdef",
        )
    assert path.read_text(encoding="utf-8") == original


def test_supersede_accepts_back_reference_via_superseded_ids_list(
    tmp_path: Path,
) -> None:
    """A multi-prior heal successor references older priors through the
    superseded_evaluation_ids list rather than the single back-pointer."""
    record = _record()
    path = _write(tmp_path, record)
    successor = _record()
    successor["evaluation_id"] = "eval-0123456789abcdef"
    successor["supersedes_evaluation_id"] = "eval-2222222222222222"
    successor["superseded_evaluation_ids"] = [
        "eval-2222222222222222",
        _predecessor_evaluation_id(record),
    ]
    _write(tmp_path, successor, "20260801T030000Z_successor.yaml")
    updated, paused = finalize_audit_record(
        path,
        record,
        final_state="superseded",
        actuals={},
        superseded_by="eval-0123456789abcdef",
    )
    assert paused is False
    assert updated["superseded_by_evaluation_id"] == "eval-0123456789abcdef"


def test_sweep_flags_unrelated_identity_successor(tmp_path: Path) -> None:
    record = _record(final_state="superseded")
    record["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    record["superseded_by_evaluation_id"] = "eval-bbbbbbbbbbbbbbbb"
    _write(tmp_path, record)
    unrelated = _record()
    unrelated["receiver"] = "other-receiver"
    unrelated["message_id"] = "msg-20260801010000-alice-unrelated"
    unrelated["evaluation_id"] = "eval-bbbbbbbbbbbbbbbb"
    _write(tmp_path, unrelated, "20260801T030000Z_unrelated.yaml")
    report = sweep_audit_dir(tmp_path)
    codes = [
        finding["code"]
        for findings in report["records"].values()
        for finding in findings
    ]
    assert codes == ["superseded_missing_successor"]


def test_sweep_accepts_multi_prior_heal_chain(tmp_path: Path) -> None:
    """Both predecessors of a multi-prior heal resolve through the
    successor's superseded_evaluation_ids list — no findings."""
    older = _record(final_state="superseded")
    older["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    older["evaluation_id"] = "eval-2222222222222222"
    older["superseded_by_evaluation_id"] = "eval-0123456789abcdef"
    _write(tmp_path, older, "20260801T010000Z_older.yaml")
    newer = _record(final_state="superseded")
    newer["result"]["completed_at_utc"] = "2026-08-01T02:30:00Z"
    newer["evaluation_id"] = "eval-3333333333333333"
    newer["superseded_by_evaluation_id"] = "eval-0123456789abcdef"
    _write(tmp_path, newer, "20260801T020000Z_newer.yaml")
    survivor = _record()
    survivor["evaluation_id"] = "eval-0123456789abcdef"
    survivor["supersedes_evaluation_id"] = "eval-3333333333333333"
    survivor["superseded_evaluation_ids"] = [
        "eval-2222222222222222",
        "eval-3333333333333333",
    ]
    _write(tmp_path, survivor, "20260801T030000Z_survivor.yaml")
    report = sweep_audit_dir(tmp_path)
    codes = [
        finding["code"]
        for findings in report["records"].values()
        for finding in findings
    ]
    assert codes == []


def test_sweep_flags_dangling_successor_chain(tmp_path: Path) -> None:
    record = _record(final_state="superseded")
    record["result"]["completed_at_utc"] = "2026-08-01T02:00:00Z"
    record["superseded_by_evaluation_id"] = "eval-deadbeefdeadbeef"
    _write(tmp_path, record)
    report = sweep_audit_dir(tmp_path)
    codes = [
        finding["code"]
        for findings in report["records"].values()
        for finding in findings
    ]
    assert codes == ["superseded_missing_successor"]


def test_resupersede_refused(tmp_path: Path) -> None:
    record = _record(final_state="superseded")
    record["superseded_by_evaluation_id"] = "eval-0123456789abcdef"
    path = _write(tmp_path, record)
    with pytest.raises(ValueError, match="already superseded"):
        finalize_audit_record(
            path,
            record,
            final_state="superseded",
            actuals={},
            superseded_by="eval-fedcba9876543210",
        )


# ── receiver-policy threading for re-authorization arbitration ────────────


POLICY_YAML = """\
autonomy:
  default_mode: auto_review
  auto_review_thresholds:
    max_estimated_minutes: 45
    max_expected_files_touched: 5
    destructive_ops: pause
    external_side_effects: allow_pr_artifacts
    auth_config_or_secrets: pause
    dependency_changes: pause
    public_visibility: pause
    git_push_or_deploy: pause
  allow_without_task_profile:
    - brainstorm_request
  private_repo_allowlist:
    - acme/widgets
"""


def _sender_reauth_setup(
    tmp_path: Path,
    *,
    scope_minutes: int = 44,
    actual_minutes: int = 40,
    policy: bool = True,
) -> tuple[Path, Path]:
    """An accepted 30-minute envelope, a breach, and a sender re-auth."""
    envelope = dict(ENVELOPE)
    envelope["estimated_minutes"] = 30
    record = _record(
        decision="auto_accepted",
        completion_kind="auto_accepted",
        final_state="pending",
        envelope=envelope,
    )
    if policy:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(POLICY_YAML, encoding="utf-8")
        record["policy_path"] = str(config_path)
    record_path = _write(tmp_path, record)
    actuals_path = tmp_path / "actuals.yaml"
    actuals_path.write_text(
        yaml.safe_dump({
            "work_started_at_utc": "2026-08-01T01:05:00Z",
            "actual_minutes": actual_minutes,
            "actual_files_touched": 1,
            "paused_at_utc": "2026-08-01T01:45:00Z",
            "side_effects_actual": {
                "creates_or_updates_pr": False,
                "comments_on_github": False,
                "commits_changes": False,
            },
            "reauthorization": {
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-08-01T01:50:00Z",
                    "source_message_id": "msg-20260801015000-alice-re01",
                    "scope": {"max_actual_minutes": scope_minutes},
                },
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    return record_path, actuals_path


def test_cli_checkpoint_arbitrates_sender_reply_under_record_policy(
    tmp_path: Path,
) -> None:
    record_path, actuals_path = _sender_reauth_setup(tmp_path)

    rc = main([str(record_path), "--checkpoint", "--actuals", str(actuals_path)])

    assert rc == 0
    stored = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    checkpoint = stored["result"]["threshold_checkpoint"]
    assert checkpoint["action"] == "resumed_after_reauthorization"
    reauth = checkpoint["reauthorization"]
    assert reauth["channel"] == "sender_reply"
    assert reauth["disposition"] == "resumed"
    assert reauth["scope"]["max_actual_minutes"] == 44
    assert reauth["cleared_paused_at_utc"] == "2026-08-01T01:50:00Z"


def test_cli_checkpoint_caps_sender_scope_at_receiver_thresholds(
    tmp_path: Path,
) -> None:
    record_path, actuals_path = _sender_reauth_setup(
        tmp_path, scope_minutes=60, actual_minutes=50
    )

    rc = main([str(record_path), "--checkpoint", "--actuals", str(actuals_path)])

    assert rc == 4
    stored = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    reauth = stored["result"]["threshold_checkpoint"]["reauthorization"]
    assert reauth["disposition"] == "insufficient"
    assert reauth["requested_scope"]["max_actual_minutes"] == 60
    assert reauth["scope"]["max_actual_minutes"] == 45


def test_cli_checkpoint_without_resolvable_policy_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    record_path, actuals_path = _sender_reauth_setup(tmp_path, policy=False)

    rc = main([str(record_path), "--checkpoint", "--actuals", str(actuals_path)])

    assert rc == 4
    err = capsys.readouterr().err
    assert "receiver policy unresolvable" in err
    assert "fail closed" in err
    stored = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    reauth = stored["result"]["threshold_checkpoint"]["reauthorization"]
    assert reauth["disposition"] == "insufficient"
    # No numeric budget survives without a resolvable policy; boundary
    # booleans record as not-granted.
    assert "max_actual_minutes" not in reauth["scope"]
    assert not any(reauth["scope"].values())


def test_cli_config_override_resolves_policy(tmp_path: Path) -> None:
    record_path, actuals_path = _sender_reauth_setup(tmp_path, policy=False)
    override = tmp_path / "override.yaml"
    override.write_text(POLICY_YAML, encoding="utf-8")

    rc = main([
        str(record_path),
        "--checkpoint",
        "--actuals", str(actuals_path),
        "--config", str(override),
    ])

    assert rc == 0
    stored = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    reauth = stored["result"]["threshold_checkpoint"]["reauthorization"]
    assert reauth["disposition"] == "resumed"


def test_cli_explicit_config_unresolvable_errors(tmp_path: Path) -> None:
    record_path, actuals_path = _sender_reauth_setup(tmp_path)
    original = record_path.read_bytes()

    rc = main([
        str(record_path),
        "--checkpoint",
        "--actuals", str(actuals_path),
        "--config", str(tmp_path / "missing.yaml"),
    ])

    assert rc == 2
    assert record_path.read_bytes() == original


def test_cli_tampered_record_policy_grants_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A policy failing signature authorization is no policy at all.

    An auth-like trailer that fails exact framing is a byte-tamper
    signal (`policy_auth.status: invalid`) — the sender channel must
    fail closed exactly as it does with no resolvable policy, never
    arbitrate against the tampered mapping's thresholds.
    """
    record_path, actuals_path = _sender_reauth_setup(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        POLICY_YAML + 'auth: "not-a-real-signature"\n', encoding="utf-8"
    )

    rc = main([str(record_path), "--checkpoint", "--actuals", str(actuals_path)])

    assert rc == 4
    err = capsys.readouterr().err
    assert "receiver policy unresolvable" in err
    assert "fail closed" in err
    stored = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    reauth = stored["result"]["threshold_checkpoint"]["reauthorization"]
    assert reauth["disposition"] == "insufficient"
    assert "max_actual_minutes" not in reauth["scope"]
    assert not any(reauth["scope"].values())


def test_cli_explicit_tampered_config_errors_bytes_unchanged(
    tmp_path: Path,
) -> None:
    record_path, actuals_path = _sender_reauth_setup(tmp_path, policy=False)
    override = tmp_path / "override.yaml"
    override.write_text(
        POLICY_YAML + 'auth: "not-a-real-signature"\n', encoding="utf-8"
    )
    original = record_path.read_bytes()

    rc = main([
        str(record_path),
        "--checkpoint",
        "--actuals", str(actuals_path),
        "--config", str(override),
    ])

    assert rc == 2
    assert record_path.read_bytes() == original


def test_cli_terminal_parity_arbitrates_sender_reply(tmp_path: Path) -> None:
    record_path, actuals_path = _sender_reauth_setup(tmp_path)
    actuals = yaml.safe_load(actuals_path.read_text(encoding="utf-8"))
    actuals["paused_at_utc"] = "2026-08-01T01:40:00Z"
    actuals["reauthorization"]["sender_reply"]["decided_at_utc"] = (
        "2026-08-01T01:45:00Z"
    )
    actuals["completed_at_utc"] = "2026-08-01T01:50:00Z"
    actuals_path.write_text(yaml.safe_dump(actuals, sort_keys=False), encoding="utf-8")

    rc = main([str(record_path), "--final-state", "done", "--actuals", str(actuals_path)])

    assert rc == 0
    stored = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    assert stored["result"]["final_state"] == "done"
    checkpoint = stored["result"]["threshold_checkpoint"]
    assert checkpoint["action"] == "resumed_after_reauthorization"
    assert checkpoint["reauthorization"]["disposition"] == "resumed"
    assert validate_audit_record(stored) == []


def test_admission_approve_checkpoint_clear_sequence_preserves_both_decisions(
    tmp_path: Path,
) -> None:
    """The full two-decision lifecycle through the CLIs.

    Admission pause -> approve -> checkpoint pause -> recorder clear ->
    finalize done: both human decisions stay readable, the admission
    timestamp intact.
    """
    from record_autonomy_outcome import main as outcome_main

    envelope = dict(ENVELOPE)
    envelope["estimated_minutes"] = 30
    record = _record(envelope=envelope)
    record_path = _write(tmp_path, record)

    assert outcome_main([
        str(record_path),
        "--decision", "approved",
        "--decided-at", "2026-08-01T01:02:00Z",
        "--actor", "alice",
    ]) == 0

    ckpt_actuals = tmp_path / "ckpt.yaml"
    ckpt_actuals.write_text(
        yaml.safe_dump({
            "work_started_at_utc": "2026-08-01T01:05:00Z",
            "actual_minutes": 40,
            "actual_files_touched": 1,
            "paused_at_utc": "2026-08-01T01:45:00Z",
            "side_effects_actual": {
                "creates_or_updates_pr": False,
                "comments_on_github": False,
                "commits_changes": False,
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    assert main([str(record_path), "--checkpoint", "--actuals", str(ckpt_actuals)]) == 4

    assert outcome_main([
        str(record_path),
        "--decision", "approved",
        "--decided-at", "2026-08-01T01:50:00Z",
        "--actor", "alice",
    ]) == 0

    stored = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    assert stored["result"]["human_outcome"]["decided_at_utc"] == (
        "2026-08-01T01:02:00Z"
    )
    assert stored["result"]["threshold_checkpoint"]["reauthorization"][
        "disposition"
    ] == "resumed"

    done_actuals = tmp_path / "done.yaml"
    done_actuals.write_text(
        yaml.safe_dump({
            "actual_minutes": 40,
            "actual_files_touched": 1,
            "completed_at_utc": "2026-08-01T01:50:00Z",
        }, sort_keys=False),
        encoding="utf-8",
    )
    assert main([
        str(record_path), "--final-state", "done", "--actuals", str(done_actuals),
    ]) == 0

    stored = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    assert stored["result"]["final_state"] == "done"
    outcome = stored["result"]["human_outcome"]
    assert outcome["decided_at_utc"] == "2026-08-01T01:02:00Z"
    assert outcome["pause_reason_codes"] == [
        "expected_files_touched_exceeds_threshold"
    ]
    reauth = stored["result"]["threshold_checkpoint"]["reauthorization"]
    assert reauth["channel"] == "receiver_human"
    assert reauth["decided_at_utc"] == "2026-08-01T01:50:00Z"
    assert validate_audit_record(stored) == []
