# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Executable tests for scripts/autonomy_gate.py."""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from autonomy_gate import (  # noqa: E402
    PINNED_REASON_CODES,
    canonical_policy_sha256,
    evaluate_autonomy,
)


FIXTURE_ROOT = Path(__file__).parent / "conformance" / "autonomy"


def _load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _assert_subset(expected: Any, actual: Any) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual
            _assert_subset(value, actual[key])
        return
    assert actual == expected


def test_autonomy_gate_matches_conformance_fixtures(tmp_path: Path) -> None:
    expected_files = sorted((FIXTURE_ROOT / "expected").glob("*.yaml"))
    assert expected_files

    for expected_path in expected_files:
        fixture = _load_yaml(expected_path)
        config = _load_yaml(FIXTURE_ROOT / fixture["config"])
        message = _load_yaml(FIXTURE_ROOT / fixture["message"])
        actuals = None
        if fixture.get("actuals"):
            actuals = _load_yaml(FIXTURE_ROOT / fixture["actuals"])
        audit_dir = None
        if fixture.get("audits"):
            audit_dir = tmp_path / expected_path.stem
            audit_dir.mkdir()
            for audit_ref in fixture["audits"]:
                source = FIXTURE_ROOT / audit_ref
                shutil.copy2(source, audit_dir / source.name)

        decision = evaluate_autonomy(
            message,
            config,
            actuals=actuals,
            audit_dir=audit_dir,
            receiver="codex",
        )
        expected = fixture["expected"]

        assert decision["decision"] == expected["decision"], expected_path.name
        assert decision["mode"] == expected["mode"], expected_path.name
        assert decision["reason_codes"] == expected["reason_codes"], expected_path.name

        if "matched_pattern" in expected:
            assert decision.get("matched_pattern") == expected["matched_pattern"]

        if "logged_notes" in expected:
            expected_patterns = [note["matched_pattern"] for note in expected["logged_notes"]]
            actual_patterns = [
                note["matched_pattern"] for note in decision.get("logged_notes", [])
            ]
            assert actual_patterns == expected_patterns

        if "continuation_grant" in expected:
            _assert_subset(expected["continuation_grant"], decision["continuation_grant"])

        if "result" in expected:
            _assert_subset(expected["result"], decision["result"])

        if "breached" in expected:
            assert decision["breached"] == expected["breached"]

        if "task_profile" in expected:
            _assert_subset(expected["task_profile"], decision["task_profile"])

        assert set(decision["reason_codes"]) <= PINNED_REASON_CODES
        assert "completed_at_utc" in decision["result"]


def test_autonomy_gate_output_uses_canonical_final_states() -> None:
    allowed = {"done", "paused", "blocked", "superseded", "error"}
    for expected_path in sorted((FIXTURE_ROOT / "expected").glob("*.yaml")):
        fixture = _load_yaml(expected_path)
        config = _load_yaml(FIXTURE_ROOT / fixture["config"])
        message = _load_yaml(FIXTURE_ROOT / fixture["message"])
        actuals = _load_yaml(FIXTURE_ROOT / fixture["actuals"]) if fixture.get("actuals") else None

        decision = evaluate_autonomy(message, config, actuals=actuals)
        assert decision["result"]["final_state"] in allowed
        assert decision["schema_version"] == 2
        assert "human_outcome" in decision["result"]


def test_autonomy_gate_records_raw_message_hash_when_path_provided() -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message_path = FIXTURE_ROOT / "messages" / "clean_task.yaml"
    message = _load_yaml(message_path)

    decision = evaluate_autonomy(message, config, message_path=message_path)

    expected_hash = hashlib.sha256(message_path.read_bytes()).hexdigest()
    assert decision["message_sha256"] == expected_hash
    assert "message_hash_recorded" in decision["reason_codes"]


def test_autonomy_gate_records_hash_for_always_pause_mode() -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "always_pause.yaml")
    message_path = FIXTURE_ROOT / "messages" / "clean_task.yaml"
    message = _load_yaml(message_path)

    decision = evaluate_autonomy(message, config, message_path=message_path)

    expected_hash = hashlib.sha256(message_path.read_bytes()).hexdigest()
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["mode_always_pause"]
    assert decision["message_sha256"] == expected_hash


def test_autonomy_gate_records_hash_for_malformed_config() -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "malformed_config.yaml")
    message_path = FIXTURE_ROOT / "messages" / "clean_task.yaml"
    message = _load_yaml(message_path)

    decision = evaluate_autonomy(message, config, message_path=message_path)

    expected_hash = hashlib.sha256(message_path.read_bytes()).hexdigest()
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["config_malformed"]
    assert decision["message_sha256"] == expected_hash


def test_policy_hash_ignores_comments_and_formatting() -> None:
    first = yaml.safe_load(
        """
autonomy:
  default_mode: auto_review  # comment-only difference
  auto_review_thresholds: {max_estimated_minutes: 45}
"""
    )
    second = yaml.safe_load(
        """
autonomy:
  auto_review_thresholds:
    max_estimated_minutes: 45
  default_mode: auto_review
"""
    )

    assert canonical_policy_sha256(first) == canonical_policy_sha256(second)


def test_guardrails_fence_keeps_operative_terms_visible_as_advisories() -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    message["body"] = message["body"].replace(
        "## Task\nUpdate one documentation paragraph for clarity.",
        "## Task\nUpdate one documentation paragraph for clarity.\n\n"
        "```oacp-guardrails\nDeploy to production and change auth config.\n```",
    )

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "auto_accepted"
    patterns = [note["matched_pattern"] for note in decision["logged_notes"]]
    assert "deploy" in patterns
    assert "auth" in patterns
    assert "lexical_advisory" in decision["reason_codes"]


def test_autonomy_gate_pauses_same_receiver_replay(tmp_path: Path) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    audit_path = tmp_path / "20260512T120000Z_replay.yaml"
    audit_path.write_text(
        yaml.safe_dump({
            "receiver": "codex",
            "message_id": message["id"],
            "decision": "auto_accepted",
        }),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(message, config, audit_dir=tmp_path, receiver="codex")

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["message_replayed"]


def test_autonomy_gate_allows_same_receiver_paused_audit(tmp_path: Path) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    audit_path = tmp_path / "20260512T120000Z_paused.yaml"
    audit_path.write_text(
        yaml.safe_dump({
            "receiver": "codex",
            "message_id": message["id"],
            "decision": "paused",
        }),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(message, config, audit_dir=tmp_path, receiver="codex")

    assert decision["decision"] == "auto_accepted"


def test_autonomy_gate_allows_different_receiver_audit(tmp_path: Path) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    audit_path = tmp_path / "20260512T120000Z_other_receiver.yaml"
    audit_path.write_text(
        yaml.safe_dump({
            "receiver": "claude",
            "message_id": message["id"],
            "decision": "auto_accepted",
        }),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(message, config, audit_dir=tmp_path, receiver="codex")

    assert decision["decision"] == "auto_accepted"


def test_sender_declared_continuation_requires_prior_human_approval() -> None:
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "continuation_grant.yaml")

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"][0] == "continuation_grant_missing_approval"
    assert decision["continuation_grant"]["decision"] == "missing_approval"


def test_latest_same_thread_grant_denial_repauses(tmp_path: Path) -> None:
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "continuation_grant.yaml")
    message["conversation_id"] = "conv-20260526-iris-001"
    approved = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_thread_grant_approved.yaml"
    )
    denied = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_thread_grant_approved.yaml"
    )
    denied["message_id"] = "msg-20260526042000-iris-denial"
    outcome = denied["result"]["human_outcome"]
    outcome["decided_at_utc"] = "2026-05-26T04:20:00Z"
    outcome["grant"]["decision"] = "denied"
    outcome["grant"]["granted_scope"] = None
    (tmp_path / "approved.yaml").write_text(
        yaml.safe_dump(approved, sort_keys=False),
        encoding="utf-8",
    )
    (tmp_path / "denied.yaml").write_text(
        yaml.safe_dump(denied, sort_keys=False),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(
        message,
        config,
        audit_dir=tmp_path,
        receiver="codex",
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"][0] == "continuation_grant_denied"
    assert decision["continuation_grant"]["decision"] == "denied"


def test_standing_grant_matches_conversation_beyond_immediate_parent(
    tmp_path: Path,
) -> None:
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "continuation_grant.yaml")
    message["conversation_id"] = "conv-20260526-iris-001"
    message["parent_message_id"] = "msg-20260526042000-iris-intermediate"
    prior = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_thread_grant_approved.yaml"
    )
    (tmp_path / "prior.yaml").write_text(
        yaml.safe_dump(prior, sort_keys=False),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(
        message,
        config,
        audit_dir=tmp_path,
        receiver="codex",
    )

    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["standing_grant_found"] is True
    assert decision["continuation_grant"]["source_message_id"] == prior["message_id"]


def test_standing_grant_does_not_cross_senders(tmp_path: Path) -> None:
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "continuation_grant.yaml")
    message["from"] = "claude"
    message["id"] = "msg-20260526042500-claude-continuation"
    prior = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_thread_grant_approved.yaml"
    )
    (tmp_path / "prior.yaml").write_text(
        yaml.safe_dump(prior, sort_keys=False),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(
        message,
        config,
        audit_dir=tmp_path,
        receiver="codex",
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"][0] == "continuation_grant_missing_approval"


def test_followup_created_before_human_approval_cannot_use_grant(
    tmp_path: Path,
) -> None:
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "continuation_grant.yaml")
    message["created_at_utc"] = "2026-05-26T04:11:00Z"
    prior = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_thread_grant_approved.yaml"
    )
    (tmp_path / "prior.yaml").write_text(
        yaml.safe_dump(prior, sort_keys=False),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(
        message,
        config,
        audit_dir=tmp_path,
        receiver="codex",
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"][0] == "continuation_grant_missing_approval"


def test_prior_standing_grant_does_not_require_sender_to_repeat_request(
    tmp_path: Path,
) -> None:
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "continuation_grant.yaml")
    message["body"] = message["body"].split("\n  continuation_grants:", 1)[0]
    prior = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_thread_grant_approved.yaml"
    )
    (tmp_path / "prior.yaml").write_text(
        yaml.safe_dump(prior, sort_keys=False),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(
        message,
        config,
        audit_dir=tmp_path,
        receiver="codex",
    )

    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["request_present"] is False
    assert decision["continuation_grant"]["standing_grant_found"] is True


def test_followup_declared_files_outside_grant_repauses(tmp_path: Path) -> None:
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "continuation_grant.yaml")
    message["body"] = message["body"].replace(
        "expected_files_touched: 1",
        "expected_files_touched: 4",
    )
    prior = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_thread_grant_approved.yaml"
    )
    (tmp_path / "prior.yaml").write_text(
        yaml.safe_dump(prior, sort_keys=False),
        encoding="utf-8",
    )

    decision = evaluate_autonomy(
        message,
        config,
        audit_dir=tmp_path,
        receiver="codex",
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["continuation_grant_scope_exceeded"]
    assert decision["breached"] == ["task_profile.expected_files_touched"]
