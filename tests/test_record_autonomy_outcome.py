# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for structured autonomy approval and decline outcomes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import record_autonomy_outcome as outcome_recorder  # noqa: E402
from record_autonomy_outcome import (  # noqa: E402
    build_human_outcome,
    is_checkpoint_paused,
    main,
    record_human_outcome,
)


SCOPE = {
    "max_actual_minutes": 30,
    "max_actual_files_touched": 3,
    "creates_or_updates_pr": True,
    "comments_on_github": True,
    "commits_changes": False,
}
# What SCOPE normalizes to: absent granular fields default to False.
NORMALIZED_SCOPE = {**SCOPE, "merges_pr": False, "files_issues": False}


def _audit(*, requested_grant: bool = False) -> Dict[str, Any]:
    profile: Dict[str, Any] = {}
    if requested_grant:
        profile["continuation_grants"] = {
            "approved_thread_continuation": {"scope": dict(SCOPE)}
        }
    return {
        "schema_version": 1,
        "created_at_utc": "2026-07-11T01:00:00Z",
        "receiver": "codex",
        "message_id": "msg-20260711010000-iris-test",
        "decision": "paused",
        "reason_codes": ["expected_files_touched_exceeds_threshold"],
        "task_profile": profile,
        "result": {"final_state": "paused"},
    }


def test_records_task_approval_latency_without_grant() -> None:
    outcome = build_human_outcome(
        _audit(),
        decision="approved",
        decided_at_utc="2026-07-11T01:02:05Z",
    )

    assert outcome["decision"] == "approved"
    assert outcome["decision_latency_seconds"] == 125
    assert outcome["pause_reason_codes"] == [
        "expected_files_touched_exceeds_threshold"
    ]
    assert outcome["grant"]["decision"] == "not_requested"
    assert outcome["grant"]["request_present"] is False
    assert outcome["grant"]["request_error"] is None
    assert outcome["grant"]["granted_scope"] is None


def test_approved_grant_uses_requested_scope() -> None:
    updated = record_human_outcome(
        _audit(requested_grant=True),
        decision="approved",
        grant_decision="approved",
        decided_at_utc="2026-07-11T01:01:00Z",
    )

    assert updated["schema_version"] == 2
    assert updated["conversation_id"] is None
    assert updated["parent_message_id"] is None
    grant = updated["result"]["human_outcome"]["grant"]
    assert grant["requested_scope"] == NORMALIZED_SCOPE
    assert grant["granted_scope"] == NORMALIZED_SCOPE


def test_malformed_grant_request_does_not_block_task_decline(
    tmp_path: Path,
) -> None:
    audit = _audit(requested_grant=True)
    request = audit["task_profile"]["continuation_grants"][
        "approved_thread_continuation"
    ]
    del request["scope"]["max_actual_minutes"]
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(
        yaml.safe_dump(audit, sort_keys=False),
        encoding="utf-8",
    )

    code = main([
        str(audit_path),
        "--decision",
        "declined",
        "--decided-at",
        "2026-07-11T01:01:00Z",
    ])

    assert code == 0
    updated = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    outcome = updated["result"]["human_outcome"]
    grant = outcome["grant"]
    assert grant["decision"] == "not_requested"
    assert grant["request_present"] is True
    assert grant["request_error"] == "max_actual_minutes_invalid"
    assert grant["requested_scope"] is None


def test_malformed_grant_request_requires_scope_for_approval() -> None:
    audit = _audit(requested_grant=True)
    request = audit["task_profile"]["continuation_grants"][
        "approved_thread_continuation"
    ]
    del request["scope"]["max_actual_minutes"]

    with pytest.raises(
        ValueError,
        match="approved grant requires a valid scope",
    ):
        build_human_outcome(
            audit,
            decision="approved",
            grant_decision="approved",
            decided_at_utc="2026-07-11T01:01:00Z",
        )


def test_modified_grant_requires_explicit_scope() -> None:
    with pytest.raises(ValueError, match="requires --grant-scope-file"):
        build_human_outcome(
            _audit(requested_grant=True),
            decision="modified",
            grant_decision="modified",
            decided_at_utc="2026-07-11T01:01:00Z",
        )


def test_grant_request_requires_explicit_grant_decision() -> None:
    with pytest.raises(ValueError, match="explicit --grant-decision"):
        build_human_outcome(
            _audit(requested_grant=True),
            decision="approved",
            decided_at_utc="2026-07-11T01:01:00Z",
        )


def test_declined_task_cannot_approve_grant() -> None:
    with pytest.raises(ValueError, match="declined task"):
        build_human_outcome(
            _audit(requested_grant=True),
            decision="declined",
            grant_decision="approved",
            decided_at_utc="2026-07-11T01:01:00Z",
        )


def test_rejects_unknown_audit_schema() -> None:
    audit = _audit()
    audit["schema_version"] = 99

    with pytest.raises(ValueError, match="schema_version"):
        record_human_outcome(
            audit,
            decision="approved",
            decided_at_utc="2026-07-11T01:01:00Z",
        )


def test_refuses_to_overwrite_recorded_outcome_without_replace() -> None:
    updated = record_human_outcome(
        _audit(),
        decision="approved",
        decided_at_utc="2026-07-11T01:01:00Z",
    )

    with pytest.raises(ValueError, match="already has a recorded human outcome"):
        record_human_outcome(
            updated,
            decision="declined",
            decided_at_utc="2026-07-11T01:02:00Z",
        )

    replaced = record_human_outcome(
        updated,
        replace=True,
        decision="declined",
        decided_at_utc="2026-07-11T01:02:00Z",
    )
    assert replaced["result"]["human_outcome"]["decision"] == "declined"


def _checkpoint_audit() -> Dict[str, Any]:
    """An auto-accepted admission whose in-place §E checkpoint later breached."""
    return {
        "schema_version": 2,
        "created_at_utc": "2026-07-17T01:00:00Z",
        "receiver": "claude",
        "message_id": "msg-20260717010000-alice-ckpt",
        "decision": "auto_accepted",
        "reason_codes": ["message_valid", "task_profile_present"],
        "task_profile": {},
        "result": {
            "final_state": "paused",
            "completion_kind": "checkpoint_paused",
            "threshold_checkpoint": {
                "evaluated": True,
                "breached": True,
                "breached_fields": ["actual_files_touched"],
                "declaration_errors": [],
                "breach_basis": "realized",
                "paused_at_utc": "2026-07-17T01:43:00Z",
            },
        },
    }


def test_checkpoint_pause_measures_latency_from_checkpoint() -> None:
    outcome = build_human_outcome(
        _checkpoint_audit(),
        decision="approved",
        decided_at_utc="2026-07-17T01:44:24Z",
    )
    # 84s from the checkpoint firing, NOT the ~44 minutes from admission.
    assert outcome["decision_latency_seconds"] == 84
    assert outcome["pause_reason_codes"] == ["threshold_checkpoint_breached"]


def test_checkpoint_declaration_error_reasons() -> None:
    audit = _checkpoint_audit()
    audit["result"]["threshold_checkpoint"]["declaration_errors"] = [
        "side_effects_actual.merges_pr"
    ]
    outcome = build_human_outcome(
        audit,
        decision="approved",
        decided_at_utc="2026-07-17T01:44:24Z",
    )
    assert outcome["pause_reason_codes"] == ["declaration_error"]


def test_checkpoint_pause_without_paused_at_refused() -> None:
    audit = _checkpoint_audit()
    audit["result"]["threshold_checkpoint"]["paused_at_utc"] = None
    with pytest.raises(ValueError, match="paused_at_utc"):
        build_human_outcome(
            audit,
            decision="approved",
            decided_at_utc="2026-07-17T01:44:24Z",
        )


def test_decision_before_checkpoint_pause_refused() -> None:
    # After admission (01:00) but before the checkpoint fired (01:43).
    with pytest.raises(ValueError, match="precede"):
        build_human_outcome(
            _checkpoint_audit(),
            decision="approved",
            decided_at_utc="2026-07-17T01:00:30Z",
        )


def test_record_accepts_checkpoint_paused_audit() -> None:
    updated = record_human_outcome(
        _checkpoint_audit(),
        decision="approved",
        decided_at_utc="2026-07-17T01:44:24Z",
        actor="alice",
    )
    outcome = updated["result"]["human_outcome"]
    assert outcome["recorded"] is True
    assert outcome["actor"] == "alice"


def test_record_still_refuses_unbreached_auto_accept() -> None:
    audit = _checkpoint_audit()
    audit["result"]["threshold_checkpoint"]["breached"] = False
    with pytest.raises(ValueError, match="paused"):
        record_human_outcome(
            audit,
            decision="approved",
            decided_at_utc="2026-07-17T01:44:24Z",
        )


def _admission_paused_with_actuals_audit() -> Dict[str, Any]:
    """An admission declaration_error pause evaluated with over-limit actuals.

    The attached checkpoint block breaches, but the pinned completion_kind
    says the pause the human decided on is the admission pause — latency
    must measure from admission, not from the checkpoint stamp.
    """
    return {
        "schema_version": 2,
        "created_at_utc": "2026-07-17T02:00:00Z",
        "receiver": "claude",
        "message_id": "msg-20260717020000-alice-adm",
        "decision": "paused",
        "reason_codes": ["declaration_error"],
        "task_profile": {},
        "result": {
            "final_state": "paused",
            "completion_kind": "admission_paused",
            "threshold_checkpoint": {
                "evaluated": True,
                "breached": True,
                "breached_fields": ["actual_minutes"],
                "declaration_errors": [],
                "breach_basis": "realized",
                "paused_at_utc": "2026-07-17T02:41:00Z",
            },
        },
    }


def test_admission_pause_with_breaching_actuals_is_not_checkpoint() -> None:
    assert is_checkpoint_paused(_admission_paused_with_actuals_audit()) is False


def test_admission_pause_with_breaching_actuals_measures_from_admission() -> None:
    outcome = build_human_outcome(
        _admission_paused_with_actuals_audit(),
        decision="approved",
        decided_at_utc="2026-07-17T02:02:05Z",
    )
    # 125s from admission — the checkpoint stamp (02:41) plays no part in
    # an admission-phase pause.
    assert outcome["decision_latency_seconds"] == 125
    assert outcome["pause_reason_codes"] == ["declaration_error"]


def test_checkpoint_paused_kind_qualifies_even_with_admission_codes() -> None:
    audit = _checkpoint_audit()
    audit["decision"] = "paused"
    audit["reason_codes"] = ["declaration_error"]
    assert is_checkpoint_paused(audit) is True


def test_pre_enum_paused_record_falls_back_to_reason_codes() -> None:
    # Genuinely legacy: schema v1 predates the completion_kind enum.
    audit = _checkpoint_audit()
    audit["schema_version"] = 1
    audit["decision"] = "paused"
    audit["reason_codes"] = ["threshold_checkpoint_breached"]
    del audit["result"]["completion_kind"]
    assert is_checkpoint_paused(audit) is True


def test_current_schema_paused_record_missing_kind_refused() -> None:
    # A schema-v2 paused record without a pinned kind is malformed, not
    # legacy — it must not silently take the reason-code fallback.
    audit = _checkpoint_audit()
    audit["decision"] = "paused"
    audit["reason_codes"] = ["declaration_error"]
    del audit["result"]["completion_kind"]
    with pytest.raises(ValueError, match="completion_kind"):
        is_checkpoint_paused(audit)


def test_current_schema_paused_record_unknown_kind_refused() -> None:
    audit = _checkpoint_audit()
    audit["decision"] = "paused"
    audit["reason_codes"] = ["declaration_error"]
    audit["result"]["completion_kind"] = "hard_stop"
    with pytest.raises(ValueError, match="completion_kind"):
        is_checkpoint_paused(audit)


def test_current_schema_auto_accepted_missing_kind_refused() -> None:
    # The in-place auto-accepted checkpoint shape is not exempt from the
    # completion_kind requirement — a breached checkpoint result without
    # the enum is malformed, not classifiable by decision alone.
    audit = _checkpoint_audit()
    del audit["result"]["completion_kind"]
    with pytest.raises(ValueError, match="completion_kind"):
        is_checkpoint_paused(audit)


def test_current_schema_auto_accepted_unknown_kind_refused() -> None:
    audit = _checkpoint_audit()
    audit["result"]["completion_kind"] = "hard_stop"
    with pytest.raises(ValueError, match="completion_kind"):
        is_checkpoint_paused(audit)


def test_current_schema_auto_accepted_incompatible_kind_refused() -> None:
    # An admission_paused kind cannot live on an auto-accepted decision —
    # the in-place checkpoint re-evaluation stamps checkpoint_paused.
    audit = _checkpoint_audit()
    audit["result"]["completion_kind"] = "admission_paused"
    with pytest.raises(ValueError, match="checkpoint_paused"):
        is_checkpoint_paused(audit)


def test_legacy_auto_accepted_breached_inferred_from_decision() -> None:
    audit = _checkpoint_audit()
    audit["schema_version"] = 1
    del audit["result"]["completion_kind"]
    assert is_checkpoint_paused(audit) is True


def test_actor_with_whitespace_refused() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        build_human_outcome(
            _audit(),
            decision="approved",
            decided_at_utc="2026-07-11T01:02:05Z",
            actor="two words",
        )


def test_cli_warns_on_anonymous_actor(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(
        yaml.safe_dump(_audit(), sort_keys=False),
        encoding="utf-8",
    )
    code = main([
        str(audit_path),
        "--decision",
        "approved",
        "--decided-at",
        "2026-07-11T01:02:05Z",
    ])
    assert code == 0
    assert "anonymous default" in capsys.readouterr().err


def test_cli_stable_actor_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(
        yaml.safe_dump(_audit(), sort_keys=False),
        encoding="utf-8",
    )
    code = main([
        str(audit_path),
        "--decision",
        "approved",
        "--decided-at",
        "2026-07-11T01:02:05Z",
        "--actor",
        "alice",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "anonymous default" not in captured.err
    updated = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert updated["result"]["human_outcome"]["actor"] == "alice"


def test_cli_atomically_updates_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(
        yaml.safe_dump(_audit(), sort_keys=False),
        encoding="utf-8",
    )

    code = main([
        str(audit_path),
        "--decision",
        "declined",
        "--grant-decision",
        "denied",
        "--decided-at",
        "2026-07-11T01:03:00Z",
        "--json",
    ])

    assert code == 0
    updated = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert updated["schema_version"] == 2
    outcome = updated["result"]["human_outcome"]
    assert outcome["decision"] == "declined"
    assert outcome["grant"]["decision"] == "denied"
    assert not list(tmp_path.glob("*.tmp"))


def test_cli_dry_run_does_not_modify_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.yaml"
    original = yaml.safe_dump(_audit(), sort_keys=False)
    audit_path.write_text(original, encoding="utf-8")

    code = main([
        str(audit_path),
        "--decision",
        "approved",
        "--decided-at",
        "2026-07-11T01:01:00Z",
        "--dry-run",
    ])

    assert code == 0
    assert audit_path.read_text(encoding="utf-8") == original


def test_cli_locks_the_read_modify_write_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(
        yaml.safe_dump(_audit(), sort_keys=False),
        encoding="utf-8",
    )
    lock_state = {"held": False}
    real_load = outcome_recorder._load_mapping
    real_write = outcome_recorder._atomic_write_yaml

    # The lock now lives in the shared stable-audit-lock helper
    # (_oacp_constants.locked_audit), which imports fcntl at call time —
    # patching the fcntl module intercepts it.
    import fcntl

    def fake_flock(_fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX:
            assert lock_state["held"] is False
            lock_state["held"] = True
        else:
            assert operation == fcntl.LOCK_UN
            assert lock_state["held"] is True
            lock_state["held"] = False

    def checked_load(path: Path) -> Dict[str, Any]:
        assert lock_state["held"] is True
        return real_load(path)

    def checked_write(path: Path, data: Dict[str, Any]) -> None:
        assert lock_state["held"] is True
        real_write(path, data)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    monkeypatch.setattr(outcome_recorder, "_load_mapping", checked_load)
    monkeypatch.setattr(outcome_recorder, "_atomic_write_yaml", checked_write)

    code = main([
        str(audit_path),
        "--decision",
        "approved",
        "--decided-at",
        "2026-07-11T01:01:00Z",
    ])

    assert code == 0
    assert lock_state["held"] is False
