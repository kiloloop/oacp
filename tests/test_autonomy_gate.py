# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Executable tests for scripts/autonomy_gate.py."""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from autonomy_gate import (  # noqa: E402
    PINNED_COMPLETION_KINDS,
    PINNED_REASON_CODES,
    _base_result,
    canonical_policy_sha256,
    evaluate_autonomy,
    evaluate_threshold_checkpoint,
    normalize_scope_envelope,
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

        if "co_occurring_reason_codes" in expected:
            assert (
                decision["co_occurring_reason_codes"]
                == expected["co_occurring_reason_codes"]
            ), expected_path.name

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


def test_autonomy_gate_self_stamps_evaluator_provenance() -> None:
    import autonomy_gate

    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")

    decision = evaluate_autonomy(message, config)

    prov = decision["evaluator"]
    assert prov["source"] == "scripts/autonomy_gate.py"
    expected = hashlib.sha256(
        Path(autonomy_gate.__file__).read_bytes()
    ).hexdigest()
    assert prov["content_sha256"] == expected
    assert prov["executed"] is True
    # git_sha is best-effort convenience: present only when the file
    # matches the committed blob at HEAD (never asserted non-null — the
    # content hash is the load-bearing identity).
    assert prov["git_sha"] is None or (
        isinstance(prov["git_sha"], str) and prov["git_sha"]
    )
    # provenance is stamped on every decision path, including pauses
    paused = evaluate_autonomy(
        message, _load_yaml(FIXTURE_ROOT / "configs" / "always_pause.yaml")
    )
    assert paused["evaluator"]["content_sha256"] == expected


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


# ── Checkpoint pause stamps (paused_at_utc / breach_basis) ────────────────────


UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _envelope(**overrides: Any) -> Dict[str, Any]:
    profile = {
        "estimated_minutes": 10,
        "expected_files_touched": 1,
        "risk_tier": "P3",
    }
    profile.update(overrides)
    return normalize_scope_envelope(profile)


def test_breached_checkpoint_defaults_pause_stamp_and_basis() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {"actual_minutes": 20, "actual_files_touched": 1},
    )
    assert checkpoint["breached"] is True
    assert checkpoint["breach_basis"] == "realized"
    assert UTC_RE.match(checkpoint["paused_at_utc"])


def test_breached_checkpoint_honors_receiver_pause_stamp() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 20,
            "actual_files_touched": 1,
            "paused_at_utc": "2026-07-17T01:43:00Z",
            "breach_basis": "realized",
        },
    )
    assert checkpoint["paused_at_utc"] == "2026-07-17T01:43:00Z"
    assert checkpoint["breach_basis"] == "realized"


def test_declared_intent_fields_breach_without_realized_effects() -> None:
    # The prospective shape: a declaration correction caught
    # before anything materialized — breached with every realized effect
    # false, never by pretending an outward action already happened.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "declared_intent_fields": ["task_profile.destructive_ops"],
        },
    )
    assert checkpoint["breached"] is True
    assert checkpoint["breached_fields"] == ["task_profile.destructive_ops"]
    assert checkpoint["declaration_errors"] == ["task_profile.destructive_ops"]
    assert checkpoint["breach_basis"] == "declared_intent"
    assert not any(checkpoint["side_effects_actual"].values())
    assert UTC_RE.match(checkpoint["paused_at_utc"])


def test_declared_intent_fields_reject_realized_basis() -> None:
    with pytest.raises(ValueError, match="inconsistent"):
        evaluate_threshold_checkpoint(
            _envelope(),
            {"present": False},
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "breach_basis": "realized",
                "declared_intent_fields": ["task_profile.merges_pr"],
            },
        )


def test_declared_intent_basis_requires_intent_fields() -> None:
    with pytest.raises(ValueError, match="declared_intent_fields"):
        evaluate_threshold_checkpoint(
            _envelope(),
            {"present": False},
            {
                "actual_minutes": 20,
                "actual_files_touched": 1,
                "breach_basis": "declared_intent",
            },
        )


def test_declared_intent_fields_validate_vocabulary() -> None:
    with pytest.raises(ValueError, match="declared_intent_fields"):
        evaluate_threshold_checkpoint(
            _envelope(),
            {"present": False},
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "declared_intent_fields": ["task_profile.estimated_minutes"],
            },
        )


def test_declared_intent_rejects_mixed_numeric_breach() -> None:
    # A checkpoint labeled declared_intent must not silently contain
    # realized breach sources — 20 minutes against a 10-minute envelope
    # is realized drift, not a prospective correction.
    with pytest.raises(ValueError, match="realized breach sources"):
        evaluate_threshold_checkpoint(
            _envelope(),
            {"present": False},
            {
                "actual_minutes": 20,
                "actual_files_touched": 0,
                "declared_intent_fields": ["task_profile.destructive_ops"],
            },
        )


def test_declared_intent_rejects_mixed_realized_effect() -> None:
    # An undeclared realized effect is itself a breach source.
    with pytest.raises(ValueError, match="realized breach sources"):
        evaluate_threshold_checkpoint(
            _envelope(),
            {"present": False},
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "side_effects_actual": {"merges_pr": True},
                "declared_intent_fields": ["task_profile.destructive_ops"],
            },
        )


def test_declared_intent_requires_all_false_effects() -> None:
    # Even a DECLARED realized effect breaks the pinned all-false
    # prospective shape — the record would mix executed work into a
    # caught-before-materialization pause.
    with pytest.raises(ValueError, match="all-false side_effects_actual"):
        evaluate_threshold_checkpoint(
            _envelope(creates_or_updates_pr=True),
            {"present": False},
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "side_effects_actual": {"creates_or_updates_pr": True},
                "declared_intent_fields": ["task_profile.destructive_ops"],
            },
        )


def test_declared_intent_rejects_already_declared_field() -> None:
    # A capability the envelope already declares true needs no prospective
    # correction — the input is a mistake, not a new breach.
    with pytest.raises(ValueError, match="already declared true"):
        evaluate_threshold_checkpoint(
            _envelope(merges_pr=True),
            {"present": False},
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "declared_intent_fields": ["task_profile.merges_pr"],
            },
        )


def test_declared_intent_rejects_grant_covered_field() -> None:
    # An accepted continuation grant is effective authorization — a
    # capability it covers needs no prospective correction either.
    grant_result = {
        "present": True,
        "enabled": True,
        "decision": "accepted",
        "scope": {
            "max_actual_minutes": 30,
            "max_actual_files_touched": 3,
            "merges_pr": True,
        },
    }
    with pytest.raises(ValueError, match="accepted continuation grant"):
        evaluate_threshold_checkpoint(
            _envelope(),
            grant_result,
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "declared_intent_fields": ["task_profile.merges_pr"],
            },
        )


def test_declared_intent_defaults_materialization_false() -> None:
    # The docs example omits predicted_risk_materialized: caught before
    # materialization means the metric is false, not breach-derived true.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "declared_intent_fields": ["task_profile.destructive_ops"],
        },
    )
    assert checkpoint["breached"] is True
    assert checkpoint["predicted_risk_materialized"] is False


def test_declared_intent_rejects_materialized_true() -> None:
    with pytest.raises(ValueError, match="predicted_risk_materialized"):
        evaluate_threshold_checkpoint(
            _envelope(),
            {"present": False},
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "predicted_risk_materialized": True,
                "declared_intent_fields": ["task_profile.destructive_ops"],
            },
        )


def test_declared_intent_rejects_reverse_polarity_field() -> None:
    # sends_oacp_reply_only is restrictive: flipping it false-to-true is
    # not a risky correction and stays outside the intent vocabulary.
    with pytest.raises(ValueError, match="declared_intent_fields"):
        evaluate_threshold_checkpoint(
            _envelope(),
            {"present": False},
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "declared_intent_fields": ["task_profile.sends_oacp_reply_only"],
            },
        )


# ── Declared merges reach the granular path (literal "merge" wording) ────────


def test_declared_merge_word_pauses_on_granular_path() -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(
        FIXTURE_ROOT / "messages" / "private_pr_with_merge.yaml"
    )
    assert "merge it" in message["body"]

    decision = evaluate_autonomy(message, config, receiver="codex")

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["merges_pr_pause"]
    assert {
        "code": "lexical_advisory_declared",
        "matched_pattern": "merge",
    } in decision["logged_notes"]


def test_merge_word_without_declaration_stays_hard_stop() -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(
        FIXTURE_ROOT / "messages" / "private_pr_with_merge.yaml"
    )
    message["body"] = message["body"].replace(
        "merges_pr: true", "merges_pr: false"
    )

    decision = evaluate_autonomy(message, config, receiver="codex")

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "merge"


def test_declared_merge_with_covering_grant_auto_accepts(
    tmp_path: Path,
) -> None:
    # The documented one-human-pass flow: first admission paused with
    # merges_pr_pause, the human approved a continuation grant covering
    # merges_pr, and the follow-up that literally says "merge" is admitted.
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "continuation_grant.yaml")
    message["body"] = message["body"].replace(
        "Continue the already approved review-loop branch work.",
        "Merge the approved review-loop branch once checks pass.",
    )
    message["body"] = message["body"].replace(
        "\n        commits_changes: true",
        "\n        commits_changes: true\n        merges_pr: true",
    )
    message["body"] = message["body"].replace(
        "\n  commits_changes: true",
        "\n  commits_changes: true\n  merges_pr: true",
    )
    prior = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_thread_grant_approved.yaml"
    )
    grant = prior["result"]["human_outcome"]["grant"]
    for scope in (
        prior["task_profile"]["continuation_grants"][
            "approved_thread_continuation"
        ]["scope"],
        grant["requested_scope"],
        grant["granted_scope"],
    ):
        scope["merges_pr"] = True
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
    assert {
        "code": "lexical_advisory_declared",
        "matched_pattern": "merge",
    } in decision["logged_notes"]


def test_unbreached_checkpoint_carries_no_pause_stamp() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {"actual_minutes": 5, "actual_files_touched": 1},
    )
    assert checkpoint["breached"] is False
    assert checkpoint["paused_at_utc"] is None
    assert checkpoint["breach_basis"] is None


def test_invalid_breach_basis_rejected() -> None:
    with pytest.raises(ValueError, match="breach_basis"):
        evaluate_threshold_checkpoint(
            _envelope(),
            {"present": False},
            {
                "actual_minutes": 20,
                "actual_files_touched": 1,
                "breach_basis": "guessed",
            },
        )


def test_unpinned_completion_kind_rejected() -> None:
    checkpoint = evaluate_threshold_checkpoint(None, {"present": False}, None)
    for kind in sorted(PINNED_COMPLETION_KINDS):
        _base_result("paused", kind, checkpoint)
    with pytest.raises(ValueError, match="completion_kind"):
        _base_result("paused", "hard_stop", checkpoint)
