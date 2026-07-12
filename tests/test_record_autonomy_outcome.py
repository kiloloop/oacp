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
    assert grant["requested_scope"] == SCOPE
    assert grant["granted_scope"] == SCOPE


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

    def fake_flock(_fd: int, operation: int) -> None:
        if operation == outcome_recorder.fcntl.LOCK_EX:
            assert lock_state["held"] is False
            lock_state["held"] = True
        else:
            assert operation == outcome_recorder.fcntl.LOCK_UN
            assert lock_state["held"] is True
            lock_state["held"] = False

    def checked_load(path: Path) -> Dict[str, Any]:
        assert lock_state["held"] is True
        return real_load(path)

    def checked_write(path: Path, data: Dict[str, Any]) -> None:
        assert lock_state["held"] is True
        real_write(path, data)

    monkeypatch.setattr(outcome_recorder.fcntl, "flock", fake_flock)
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
