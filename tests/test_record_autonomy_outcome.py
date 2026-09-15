# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for structured autonomy approval and decline outcomes."""

from __future__ import annotations

import sys
import copy
import json
import subprocess
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
from finalize_autonomy_record import validate_audit_record  # noqa: E402


SCOPE = {
    "allowed_types": ["task_request"],
    "max_round": 3,
    "expires_at_utc": "2026-07-20T00:00:00Z",
    "max_actual_minutes": 30,
    "max_actual_files_touched": 3,
    "creates_or_updates_pr": True,
    "comments_on_github": True,
    "commits_changes": False,
}
# What SCOPE normalizes to: absent granular fields default to False.
NORMALIZED_SCOPE = {
    **SCOPE, "merges_pr": False, "files_issues": False,
    "writes_findings_packet": False, "sends_oacp_reply": False,
    "submits_github_review": False,
}


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


@pytest.mark.parametrize("modification", [
    {},
    {"task_profile": {"estimated_minutes": 60}, "note": "Allow the larger task"},
    {"custom": {"choices": [True, None, 3], "label": "a: b\nsecond line"}},
])
def test_modification_cli_round_trip_preserves_admission_and_auth(
    tmp_path: Path, modification: Dict[str, Any],
) -> None:
    audit = _audit()
    audit["schema_version"] = 2
    audit["scope_envelope"] = {"estimated_minutes": 30}
    audit["result"].update({
        "completion_kind": "admission_paused",
        "message_auth": {"status": "verified", "payload_sha256": "abc"},
    })
    assert validate_audit_record(audit) == []
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(yaml.safe_dump(audit))
    modification_path = tmp_path / "modification.yaml"
    modification_path.write_text(yaml.safe_dump(modification))
    result = subprocess.run([
        sys.executable, "-m", "oacp.cli", "autonomy-outcome", str(audit_path),
        "--decision", "modified", "--modification-file", str(modification_path),
        "--actor", "alice", "--decided-at", "2026-07-11T01:02:05Z", "--json",
    ], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "modification unrecorded" not in result.stderr
    updated = yaml.safe_load(audit_path.read_text())
    assert updated["schema_version"] == 2
    assert updated["scope_envelope"] == audit["scope_envelope"]
    assert updated["result"]["message_auth"] == audit["result"]["message_auth"]
    assert updated["result"]["human_outcome"]["modification"] == modification
    assert json.loads(result.stdout)["human_outcome"]["modification"] == modification
    assert validate_audit_record(updated) == []


def test_modification_without_file_remains_compatible_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(yaml.safe_dump(_audit()))
    assert main([
        str(audit_path), "--decision", "modified", "--actor", "alice",
        "--decided-at", "2026-07-11T01:02:05Z",
    ]) == 0
    assert capsys.readouterr().err.strip() == (
        "WARNING: modification unrecorded; use --modification-file"
    )
    outcome = yaml.safe_load(audit_path.read_text())["result"]["human_outcome"]
    assert outcome["decision"] == "modified"
    assert "modification" not in outcome


@pytest.mark.parametrize("contents", [None, "null\n", "[]\n", "text\n", "x: [\n"])
def test_invalid_modification_file_preserves_audit(
    tmp_path: Path, contents: Any,
) -> None:
    audit_path = tmp_path / "audit.yaml"
    original = yaml.safe_dump(_audit())
    audit_path.write_text(original)
    modification_path = tmp_path / "modification.yaml"
    if contents is not None:
        modification_path.write_text(contents)
    assert main([
        str(audit_path), "--decision", "modified", "--actor", "alice",
        "--modification-file", str(modification_path),
    ]) == 2
    assert audit_path.read_text() == original


@pytest.mark.parametrize("decision", ["approved", "declined"])
def test_modification_requires_modified_decision(decision: str) -> None:
    audit = _audit()
    original = copy.deepcopy(audit)
    with pytest.raises(ValueError, match="only for a modified decision"):
        record_human_outcome(audit, decision=decision, modification={})
    assert audit == original


def test_modification_dry_run_leaves_audit_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    audit_path = tmp_path / "audit.yaml"
    original = yaml.safe_dump(_audit())
    audit_path.write_text(original)
    modification_path = tmp_path / "modification.yaml"
    modification_path.write_text("note: approved change\n")
    assert main([
        str(audit_path), "--decision", "modified", "--actor", "alice",
        "--modification-file", str(modification_path), "--dry-run", "--json",
    ]) == 0
    assert audit_path.read_text() == original
    assert json.loads(capsys.readouterr().out)["human_outcome"]["modification"] == {
        "note": "approved change",
    }


def test_unrepresentable_modification_json_fails_before_write(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.yaml"
    original = yaml.safe_dump(_audit())
    audit_path.write_text(original)
    modification_path = tmp_path / "modification.yaml"
    modification_path.write_text("2026-09-01: a date key\n")
    assert main([
        str(audit_path), "--decision", "modified", "--actor", "alice",
        "--modification-file", str(modification_path), "--json",
    ]) == 2
    assert audit_path.read_text() == original


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


def test_generic_review_scope_round_trips_flat_bounds() -> None:
    scope = {
        "allowed_types": ["review_addressed", "review_request"],
        "max_round": 3,
        "expires_at_utc": "2026-07-20T00:00:00Z",
        "max_actual_minutes": 30,
        "max_actual_files_touched": 3,
        "writes_findings_packet": True,
        "sends_oacp_reply": True,
        "submits_github_review": True,
    }
    updated = record_human_outcome(
        _audit(),
        decision="approved",
        grant_decision="approved",
        granted_scope=scope,
        decided_at_utc="2026-07-11T01:01:00Z",
    )
    granted = updated["result"]["human_outcome"]["grant"]["granted_scope"]
    for key, value in scope.items():
        assert granted[key] == value
    assert "review_loop" not in granted


@pytest.mark.parametrize("missing", ["allowed_types", "max_round", "expires_at_utc"])
def test_generic_grant_approval_requires_every_bound(missing: str) -> None:
    scope = dict(SCOPE)
    del scope[missing]
    with pytest.raises(ValueError, match="continuation_grant_missing_scope"):
        record_human_outcome(
            _audit(),
            decision="approved",
            grant_decision="approved",
            granted_scope=scope,
            decided_at_utc="2026-07-11T01:01:00Z",
        )


@pytest.mark.parametrize(
    "scope",
    [
        {"max_actual_minutes": 30, "max_actual_files_touched": 3},
        {**SCOPE, "review_loop": {"allowed_types": ["review_request"], "max_round": 3}},
    ],
)
def test_legacy_scope_cannot_be_reapproved_without_migration(
    scope: Dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="continuation_grant_missing_scope"):
        record_human_outcome(
            _audit(),
            decision="approved",
            grant_decision="approved",
            granted_scope=scope,
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
    real_write = outcome_recorder.atomic_replace_yaml

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
    monkeypatch.setattr(outcome_recorder, "atomic_replace_yaml", checked_write)

    code = main([
        str(audit_path),
        "--decision",
        "approved",
        "--decided-at",
        "2026-07-11T01:01:00Z",
    ])

    assert code == 0
    assert lock_state["held"] is False


# ── checkpoint clears on records with a recorded outcome ──────────────────


def _cleared_admission_checkpoint_audit() -> Dict[str, Any]:
    """A paused admission, human-approved, whose later §E checkpoint breached.

    Two human decisions are in flight: the admission approval already
    recorded in ``result.human_outcome``, and the checkpoint clear about to
    be recorded. The recorder must route the clear into
    ``threshold_checkpoint.reauthorization`` and preserve the admission
    outcome.
    """
    return {
        "schema_version": 2,
        "created_at_utc": "2026-08-01T01:09:00Z",
        "receiver": "claude",
        "message_id": "msg-20260801010900-alice-ck90",
        "decision": "paused",
        "reason_codes": ["estimated_minutes_exceeds_threshold"],
        "task_profile": {},
        "result": {
            "final_state": "paused",
            "completion_kind": "checkpoint_paused",
            "human_outcome": {
                "recorded": True,
                "actor": "alice",
                "decision": "approved",
                "decided_at_utc": "2026-08-01T01:11:00Z",
                "decision_latency_seconds": 120,
                "pause_reason_codes": ["estimated_minutes_exceeds_threshold"],
                "grant": {
                    "decision": "approved",
                    "request_present": True,
                    "request_error": None,
                    "requested_scope": dict(NORMALIZED_SCOPE),
                    "granted_scope": dict(NORMALIZED_SCOPE),
                },
            },
            "threshold_checkpoint": {
                "evaluated": True,
                "actual_minutes": 69,
                "actual_files_touched": 3,
                "side_effects_actual": {},
                "breached": True,
                "breached_fields": ["actual_minutes"],
                "declaration_errors": [],
                "breach_basis": "realized",
                "paused_at_utc": "2026-08-01T02:21:00Z",
                "action": "paused_for_reauthorization",
                "reauthorization": {
                    "presented": False,
                    "channel": None,
                    "decision": None,
                    "decided_at_utc": None,
                    "actor": None,
                    "source_message_id": None,
                    "requested_scope": None,
                    "scope": None,
                    "disposition": "unanswered",
                    "cleared_paused_at_utc": None,
                    "advisory": [],
                },
            },
        },
    }


def test_checkpoint_clear_routes_to_reauthorization() -> None:
    updated = record_human_outcome(
        _cleared_admission_checkpoint_audit(),
        decision="approved",
        decided_at_utc="2026-08-01T02:24:00Z",
        actor="alice",
    )

    reauth = updated["result"]["threshold_checkpoint"]["reauthorization"]
    assert reauth["presented"] is True
    assert reauth["channel"] == "receiver_human"
    assert reauth["decision"] == "approved"
    assert reauth["decided_at_utc"] == "2026-08-01T02:24:00Z"
    assert reauth["actor"] == "alice"
    assert reauth["requested_scope"] is None
    assert reauth["scope"] is None
    assert reauth["disposition"] == "resumed"
    assert reauth["cleared_paused_at_utc"] == "2026-08-01T02:24:00Z"
    checkpoint = updated["result"]["threshold_checkpoint"]
    assert checkpoint["action"] == "resumed_after_reauthorization"


def test_checkpoint_clear_preserves_admission_outcome_and_grant() -> None:
    audit = _cleared_admission_checkpoint_audit()
    before = dict(audit["result"]["human_outcome"])

    updated = record_human_outcome(
        audit,
        decision="approved",
        decided_at_utc="2026-08-01T02:24:00Z",
        actor="alice",
    )

    outcome = updated["result"]["human_outcome"]
    assert outcome == before
    assert outcome["decided_at_utc"] == "2026-08-01T01:11:00Z"
    assert outcome["grant"]["granted_scope"] == NORMALIZED_SCOPE


def test_checkpoint_clear_cannot_replace_admission_modification() -> None:
    audit = _cleared_admission_checkpoint_audit()
    audit["result"]["human_outcome"]["modification"] = {"note": "Original delta"}
    original = copy.deepcopy(audit)
    with pytest.raises(ValueError, match="records an admission outcome"):
        record_human_outcome(
            audit, decision="modified", modification={"note": "New delta"},
            actor="alice", decided_at_utc="2026-08-01T02:24:00Z",
        )
    assert audit == original
    cleared = record_human_outcome(
        audit, decision="approved", actor="alice",
        decided_at_utc="2026-08-01T02:24:00Z",
    )
    assert cleared["result"]["human_outcome"] == original["result"]["human_outcome"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_modified_checkpoint_clear_fails_before_modification_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture, dry_run: bool,
) -> None:
    audit_path = tmp_path / "audit.yaml"
    original = yaml.safe_dump(_cleared_admission_checkpoint_audit())
    audit_path.write_text(original)
    args = [str(audit_path), "--decision", "modified", "--actor", "alice"]
    if dry_run:
        args.append("--dry-run")
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "scope-less and cannot be modified" in captured.err
    assert "modification unrecorded" not in captured.err
    assert captured.out == ""
    assert audit_path.read_text() == original


def test_checkpoint_clear_declined_records_declined_disposition() -> None:
    updated = record_human_outcome(
        _cleared_admission_checkpoint_audit(),
        decision="declined",
        decided_at_utc="2026-08-01T02:24:00Z",
        actor="alice",
    )

    reauth = updated["result"]["threshold_checkpoint"]["reauthorization"]
    assert reauth["disposition"] == "declined"
    assert reauth["cleared_paused_at_utc"] is None
    checkpoint = updated["result"]["threshold_checkpoint"]
    assert checkpoint["action"] == "reauthorization_declined"
    assert updated["result"]["human_outcome"]["decision"] == "approved"


def test_checkpoint_clear_refuses_replace() -> None:
    with pytest.raises(ValueError, match="checkpoint clear"):
        record_human_outcome(
            _cleared_admission_checkpoint_audit(),
            replace=True,
            decision="approved",
            decided_at_utc="2026-08-01T02:24:00Z",
            actor="alice",
        )


def test_checkpoint_clear_refuses_modified() -> None:
    with pytest.raises(ValueError, match="scope-less"):
        record_human_outcome(
            _cleared_admission_checkpoint_audit(),
            decision="modified",
            decided_at_utc="2026-08-01T02:24:00Z",
            actor="alice",
        )


def test_checkpoint_clear_refuses_grant_machinery() -> None:
    with pytest.raises(ValueError, match="admission-time machinery"):
        record_human_outcome(
            _cleared_admission_checkpoint_audit(),
            decision="approved",
            grant_decision="approved",
            decided_at_utc="2026-08-01T02:24:00Z",
            actor="alice",
        )


def test_checkpoint_clear_before_pause_refused() -> None:
    with pytest.raises(ValueError, match="precede"):
        record_human_outcome(
            _cleared_admission_checkpoint_audit(),
            decision="approved",
            decided_at_utc="2026-08-01T02:20:00Z",
            actor="alice",
        )


def test_checkpoint_clear_requires_evaluated_actuals() -> None:
    audit = _cleared_admission_checkpoint_audit()
    audit["result"]["threshold_checkpoint"]["actual_minutes"] = None
    with pytest.raises(ValueError, match="evaluated integer actual_minutes"):
        record_human_outcome(
            audit,
            decision="approved",
            decided_at_utc="2026-08-01T02:24:00Z",
            actor="alice",
        )


def test_checkpoint_without_prior_outcome_still_records_human_outcome() -> None:
    # The single-decision shape (auto-accepted admission, first human
    # decision answers the checkpoint): nothing exists to preserve, so the
    # decision lands in human_outcome as before.
    updated = record_human_outcome(
        _checkpoint_audit(),
        decision="approved",
        decided_at_utc="2026-07-17T01:44:24Z",
        actor="alice",
    )
    outcome = updated["result"]["human_outcome"]
    assert outcome["recorded"] is True
    assert outcome["pause_reason_codes"] == ["threshold_checkpoint_breached"]
    reauth = updated["result"]["threshold_checkpoint"].get("reauthorization")
    assert reauth is None


def test_cli_checkpoint_clear_reports_disposition(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(
        yaml.safe_dump(_cleared_admission_checkpoint_audit(), sort_keys=False),
        encoding="utf-8",
    )

    code = main([
        str(audit_path),
        "--decision",
        "approved",
        "--decided-at",
        "2026-08-01T02:24:00Z",
        "--actor",
        "alice",
    ])

    assert code == 0
    stored = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert stored["result"]["human_outcome"]["decided_at_utc"] == (
        "2026-08-01T01:11:00Z"
    )
    reauth = stored["result"]["threshold_checkpoint"]["reauthorization"]
    assert reauth["disposition"] == "resumed"


@pytest.mark.parametrize("with_prior_outcome", [True, False])
@pytest.mark.parametrize(
    "evidence",
    [
        {"final_state": "done", "completed_at_utc": "2026-08-01T02:25:00Z"},
        {"final_state": "error"},
        {"final_state": "superseded"},
        {"final_state": "cancelled"},
        {"completed_at_utc": "2026-08-01T02:25:00Z"},
    ],
    ids=["done", "error", "superseded", "cancelled", "completed-at-only"],
)
def test_closed_record_refuses_every_outcome_write(
    with_prior_outcome: bool, evidence: Dict[str, Any]
) -> None:
    """Completion evidence closes the record against BOTH routing shapes.

    The prior-outcome shape routes to the checkpoint clear and the
    no-prior-outcome shape routes to the admission branch — a closed
    record must refuse the write on either route, for every evidence
    kind (terminal final_state, or a completion stamp alone)."""
    audit = _cleared_admission_checkpoint_audit()
    if not with_prior_outcome:
        del audit["result"]["human_outcome"]
    audit["result"].update(evidence)

    with pytest.raises(ValueError, match="closed"):
        record_human_outcome(
            audit,
            decision="approved",
            decided_at_utc="2026-08-01T02:26:00Z",
            actor="bob",
        )


def test_legacy_done_without_completion_stamp_is_still_live() -> None:
    """A pre-0.5.2 receipt hand-finalized `done` with no completion stamp is
    unfinalized history, not a closed record: the outcome write proceeds so
    the record can still be finalized properly once."""
    audit = _cleared_admission_checkpoint_audit()
    del audit["result"]["human_outcome"]
    audit["result"]["final_state"] = "done"
    audit["result"]["completed_at_utc"] = None

    updated = record_human_outcome(
        audit,
        decision="approved",
        decided_at_utc="2026-08-01T02:26:00Z",
        actor="bob",
    )

    assert updated["result"]["human_outcome"]["recorded"] is True


def test_cli_clear_on_terminal_conformance_fixture_leaves_bytes_unchanged(
    tmp_path: Path,
) -> None:
    """The shipped damage-signature fixture is historical evidence: a
    post-completion clear must exit nonzero without rewriting a byte."""
    fixture = (
        Path(__file__).resolve().parent
        / "conformance"
        / "autonomy"
        / "records"
        / "admission_outcome_replaced_by_clear.yaml"
    )
    target = tmp_path / "terminal.yaml"
    target.write_bytes(fixture.read_bytes())
    original = target.read_bytes()

    code = main([
        str(target),
        "--decision",
        "approved",
        "--decided-at",
        "2026-08-01T02:26:00Z",
        "--actor",
        "bob",
    ])

    assert code == 2
    assert target.read_bytes() == original


def test_cli_terminal_record_without_prior_outcome_leaves_bytes_unchanged(
    tmp_path: Path,
) -> None:
    """The no-prior-outcome adjacent of the fixture probe: a finalized
    checkpoint record with its human_outcome removed must refuse a
    post-completion outcome write without rewriting a byte."""
    audit = _cleared_admission_checkpoint_audit()
    del audit["result"]["human_outcome"]
    audit["result"]["final_state"] = "done"
    audit["result"]["completed_at_utc"] = "2026-08-01T02:25:00Z"
    target = tmp_path / "terminal_no_outcome.yaml"
    target.write_text(yaml.safe_dump(audit, sort_keys=False), encoding="utf-8")
    original = target.read_bytes()

    code = main([
        str(target),
        "--decision",
        "approved",
        "--decided-at",
        "2026-08-01T02:26:00Z",
        "--actor",
        "bob",
    ])

    assert code == 2
    assert target.read_bytes() == original
