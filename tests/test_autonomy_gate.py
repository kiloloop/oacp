# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Executable tests for scripts/autonomy_gate.py."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from autonomy_gate import (  # noqa: E402
    PINNED_COMPLETION_KINDS,
    PINNED_REASON_CODES,
    RUNTIME_MODEL_ENV_VAR,
    _base_result,
    canonical_policy_sha256,
    evaluate_autonomy,
    evaluate_threshold_checkpoint,
    main as autonomy_main,
    normalize_continuation_scope,
    normalize_runtime_model,
    normalize_scope_envelope,
    resolve_runtime_block,
    write_audit_record,
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

        now_utc = None
        if fixture.get("now"):
            now_utc = datetime.strptime(fixture["now"], "%Y-%m-%dT%H:%M:%SZ")

        decision = evaluate_autonomy(
            message,
            config,
            actuals=actuals,
            audit_dir=audit_dir,
            receiver="codex",
            now_utc=now_utc,
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

        if "review_continuation" in expected:
            _assert_subset(
                expected["review_continuation"], decision["review_continuation"]
            )

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


def test_autonomy_gate_cli_writes_audit_and_preserves_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(RUNTIME_MODEL_ENV_VAR, raising=False)
    config_path = FIXTURE_ROOT / "configs" / "auto_review_standard.yaml"
    message_path = FIXTURE_ROOT / "messages" / "clean_task.yaml"
    audit_dir = tmp_path / "audit" / "autonomy_decisions"

    assert autonomy_main([
        "--config",
        str(config_path),
        "--message",
        str(message_path),
        "--audit-dir",
        str(audit_dir),
        "--receiver",
        "codex",
    ]) == 0

    first_output = capsys.readouterr()
    stdout_decision = json.loads(first_output.out)
    assert first_output.err == ""
    audit_files = list(audit_dir.glob("*.yaml"))
    assert len(audit_files) == 1
    assert re.fullmatch(
        r"\d{8}T\d{6}Z_msg-20260512120000-iris-clean1\.yaml",
        audit_files[0].name,
    )
    audit_record = _load_yaml(audit_files[0])
    for key, value in stdout_decision.items():
        assert audit_record[key] == value
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        audit_record["created_at_utc"],
    )
    assert audit_record["message_subject"] == "Small docs cleanup"
    assert audit_record["message_path"] == str(message_path)
    assert audit_record["policy_path"] == str(config_path)
    assert audit_record["thresholds"] == {
        "max_estimated_minutes": 45,
        "max_expected_files_touched": 5,
    }
    runtime = audit_record["runtime"]
    assert runtime["agent"] == "codex"
    assert runtime["model"] is None
    assert runtime["model_source"] is None
    assert runtime["model_unknown_reason"]
    assert audit_files[0].with_name(audit_files[0].name + ".lock").is_file()

    assert autonomy_main([
        "--config",
        str(config_path),
        "--message",
        str(message_path),
        "--audit-dir",
        str(audit_dir),
        "--receiver",
        "codex",
    ]) == 0

    replay_output = capsys.readouterr()
    replay_decision = json.loads(replay_output.out)
    assert replay_decision["decision"] == "paused"
    assert replay_decision["reason_codes"] == ["message_replayed"]
    assert replay_output.err == "NOTE: replay detected; audit record not written\n"
    assert list(audit_dir.glob("*.yaml")) == audit_files


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


@pytest.mark.parametrize(
    "task_text",
    [
        "Out of scope: deploy to production.",
        "Excluded: deploy to production.",
        "Exclude any deploy to production.",
        "This task excludes: deploy to production.",
        "Avoid any deploy to production.",
        "Refrain from any deploy to production.",
        "Prohibited: deploy to production.",
        "Forbidden: deploy to production.",
        "Skip any deploy to production.",
        "Without any deploy to production.",
    ],
)
def test_negation_vocabulary_demotes_same_clause_side_effect(task_text: str) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "private_pr_artifacts.yaml")
    message["body"] = message["body"].replace(
        "Update the existing branch, open a pull request, and post the review comment.",
        task_text,
    )

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "auto_accepted"
    assert {
        "code": "lexical_advisory_negated",
        "matched_pattern": "deploy",
    } in decision["logged_notes"]


@pytest.mark.parametrize(
    "task_text",
    [
        "Out of scope:\n- deploy to production.",
        "## Out of scope\n- deploy to production.",
    ],
)
def test_negation_heading_demotes_side_effect_inside_bounded_block(
    task_text: str,
) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "private_pr_artifacts.yaml")
    message["body"] = message["body"].replace(
        "Update the existing branch, open a pull request, and post the review comment.",
        task_text,
    )

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "auto_accepted"
    assert {
        "code": "lexical_advisory_negated",
        "matched_pattern": "deploy",
    } in decision["logged_notes"]


@pytest.mark.parametrize(
    "task_text",
    [
        "Not in scope:\n- edit documentation.\n\n- deploy to production.",
        "Out of scope:\n- edit documentation.\n## In scope\n- deploy to production.",
    ],
)
def test_negation_heading_does_not_escape_its_block(task_text: str) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "private_pr_artifacts.yaml")
    message["body"] = message["body"].replace(
        "Update the existing branch, open a pull request, and post the review comment.",
        task_text,
    )

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "deploy"


@pytest.mark.parametrize(
    "task_text",
    [
        "Do the following, without delay:\n- deploy to production.",
        "Skip this introduction:\n- deploy to production.",
    ],
)
def test_ambiguous_negation_forms_do_not_scope_over_heading_blocks(
    task_text: str,
) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "private_pr_artifacts.yaml")
    message["body"] = message["body"].replace(
        "Update the existing branch, open a pull request, and post the review comment.",
        task_text,
    )

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "deploy"


@pytest.mark.parametrize(
    ("task_text", "reason_code", "matched_pattern"),
    [
        (
            "Out of scope:\n- push to main.",
            "hard_stop_external_side_effect",
            "push to main",
        ),
        (
            "Out of scope:\n- commercial pricing changes.",
            "hard_stop_content_sensitivity",
            "pricing",
        ),
    ],
)
def test_negation_heading_does_not_demote_non_demotable_patterns(
    task_text: str,
    reason_code: str,
    matched_pattern: str,
) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "private_pr_artifacts.yaml")
    message["body"] = message["body"].replace(
        "Update the existing branch, open a pull request, and post the review comment.",
        task_text,
    )

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [reason_code]
    assert decision["matched_pattern"] == matched_pattern


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


def test_normalize_runtime_model_folds_case_and_splits_context() -> None:
    assert normalize_runtime_model("GPT-5") == ("gpt-5", None)
    assert normalize_runtime_model("claude-opus-4-8") == ("claude-opus-4-8", None)
    assert normalize_runtime_model("claude-opus-4-8[1m]") == ("claude-opus-4-8", "1m")
    assert normalize_runtime_model(" Claude-Sonnet-5[1M] ") == ("claude-sonnet-5", "1m")
    assert normalize_runtime_model(None) == (None, None)
    assert normalize_runtime_model("   ") == (None, None)


def test_resolve_runtime_block_reads_env_signal() -> None:
    runtime = resolve_runtime_block(
        None,
        "claude",
        env={RUNTIME_MODEL_ENV_VAR: "Claude-Fable-5[1m]"},
    )
    assert runtime == {
        "agent": "claude",
        "model": "claude-fable-5",
        "model_source": f"env:{RUNTIME_MODEL_ENV_VAR}",
        "model_context": "1m",
        "model_raw": "Claude-Fable-5[1m]",
    }


def test_resolve_runtime_block_caller_value_wins_over_env() -> None:
    runtime = resolve_runtime_block(
        {"agent": "codex", "model": "gpt-5"},
        "codex",
        env={RUNTIME_MODEL_ENV_VAR: "some-other-model"},
    )
    assert runtime["model"] == "gpt-5"
    assert runtime["model_source"] == "caller"
    assert "model_raw" not in runtime
    assert "model_context" not in runtime


def test_resolve_runtime_block_normalizes_caller_value() -> None:
    runtime = resolve_runtime_block({"model": "GPT-5"}, "codex", env={})
    assert runtime["agent"] == "codex"
    assert runtime["model"] == "gpt-5"
    assert runtime["model_source"] == "caller"
    assert runtime["model_raw"] == "GPT-5"


def test_resolve_runtime_block_explicit_unknown_without_signal() -> None:
    runtime = resolve_runtime_block(None, "claude", env={})
    assert runtime["model"] is None
    assert runtime["model_source"] is None
    assert runtime["model_unknown_reason"]


def test_resolve_runtime_block_never_reads_requested_model_channels() -> None:
    # Requested-model channels (harness configuration such as
    # ANTHROPIC_MODEL) must never fill the serving-model field: a request
    # can be served by a different model, alias, or context variant, which
    # is the confound the field exists to remove.
    runtime = resolve_runtime_block(
        None,
        "claude",
        env={"ANTHROPIC_MODEL": "claude-opus-5", "CLAUDE_MODEL": "claude-opus-5"},
    )
    assert runtime["model"] is None
    assert runtime["model_unknown_reason"]


def test_written_record_stamps_normalized_env_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RUNTIME_MODEL_ENV_VAR, "Claude-Fable-5[1m]")
    audit_dir = tmp_path / "audit" / "autonomy_decisions"
    assert autonomy_main([
        "--config",
        str(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml"),
        "--message",
        str(FIXTURE_ROOT / "messages" / "clean_task.yaml"),
        "--audit-dir",
        str(audit_dir),
        "--receiver",
        "claude",
    ]) == 0
    capsys.readouterr()
    (audit_path,) = audit_dir.glob("*.yaml")
    runtime = _load_yaml(audit_path)["runtime"]
    assert runtime["model"] == "claude-fable-5"
    assert runtime["model_context"] == "1m"
    assert runtime["model_source"] == f"env:{RUNTIME_MODEL_ENV_VAR}"
    assert runtime["model_raw"] == "Claude-Fable-5[1m]"


def test_written_record_never_carries_silent_null_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A record written on the current spec carries either a non-null model
    # or an explicit unknown marker with a reason — never a silent null.
    # Historical records are never the writer's to touch: with no signal
    # the writer records null-with-reason rather than inventing a value.
    monkeypatch.delenv(RUNTIME_MODEL_ENV_VAR, raising=False)
    audit_dir = tmp_path / "audit" / "autonomy_decisions"
    assert autonomy_main([
        "--config",
        str(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml"),
        "--message",
        str(FIXTURE_ROOT / "messages" / "clean_task.yaml"),
        "--audit-dir",
        str(audit_dir),
        "--receiver",
        "claude",
    ]) == 0
    capsys.readouterr()
    (audit_path,) = audit_dir.glob("*.yaml")
    runtime = _load_yaml(audit_path)["runtime"]
    assert runtime["model"] is not None or runtime["model_unknown_reason"]


def test_write_audit_record_rejects_off_enum_completion_kind(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit" / "autonomy_decisions"
    decision = {
        "decision": "paused",
        "message_id": "msg-20260512120000-iris-clean1",
        "result": {"completion_kind": "human_approved_completed"},
    }
    with pytest.raises(ValueError, match="completion_kind"):
        write_audit_record(
            audit_dir,
            decision,
            config={},
            message={"subject": "x"},
            message_path=tmp_path / "msg.yaml",
            policy_path=tmp_path / "config.yaml",
            receiver="codex",
        )
    assert not audit_dir.exists()


def test_write_audit_record_rejects_missing_result_block(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="completion_kind"):
        write_audit_record(
            tmp_path / "audit" / "autonomy_decisions",
            {"decision": "paused", "message_id": "msg-x"},
            config={},
            message={"subject": "x"},
            message_path=tmp_path / "msg.yaml",
            policy_path=tmp_path / "config.yaml",
            receiver="codex",
        )


def test_resolve_runtime_block_preserves_whitespace_only_raw_change() -> None:
    # Trimming changes the caller's input, so model_raw must preserve the
    # exact original value even when the change is only surrounding
    # whitespace.
    runtime = resolve_runtime_block({"model": " gpt-5 "}, "codex", env={})
    assert runtime["model"] == "gpt-5"
    assert runtime["model_source"] == "caller"
    assert runtime["model_raw"] == " gpt-5 "


# --- Checkpoint re-authorization arbitration ---


_REAUTH_POLICY = {
    "thresholds": {
        "max_estimated_minutes": 45,
        "max_expected_files_touched": 5,
        "external_side_effects": "allow_pr_artifacts",
    },
    "private_repo_allowlist": ["example-org/private-repo"],
}


def _reauth_actuals(**overrides: Any) -> Dict[str, Any]:
    actuals: Dict[str, Any] = {
        "actual_minutes": 25,
        "actual_files_touched": 1,
        "paused_at_utc": "2026-05-12T12:30:00Z",
    }
    actuals.update(overrides)
    return actuals


def test_reauth_gh_comment_alone_never_clears() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(estimated_minutes=20),
        {"present": False},
        _reauth_actuals(
            reauthorization={"gh_comment": {"decision": "approved", "author": "x"}},
        ),
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["breached"] is True
    assert checkpoint["action"] == "paused_for_reauthorization"
    reauth = checkpoint["reauthorization"]
    assert reauth["disposition"] == "advisory_only"
    assert reauth["channel"] is None
    assert reauth["advisory"] == [
        {"channel": "gh_comment", "decision": "approved", "reason": "never_authoritative"},
    ]


def test_reauth_receiver_human_decline_overrides_sender_approval() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(estimated_minutes=20),
        {"present": False},
        _reauth_actuals(
            reauthorization={
                "receiver_human": {
                    "decision": "declined",
                    "decided_at_utc": "2026-05-12T12:50:00Z",
                },
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                    "scope": {"max_actual_minutes": 30},
                },
            },
        ),
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["action"] == "reauthorization_declined"
    reauth = checkpoint["reauthorization"]
    assert reauth["channel"] == "receiver_human"
    assert reauth["disposition"] == "declined"
    assert reauth["advisory"] == [
        {
            "channel": "sender_reply",
            "decision": "approved",
            "reason": "overridden_by_receiver_human",
        },
    ]


def test_reauth_receiver_human_approval_wins_over_sender_decline() -> None:
    # Precedence is by channel rank in both directions: a later or earlier
    # sender decline never re-opens the receiver-side human's ruling.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(estimated_minutes=20),
        {"present": False},
        _reauth_actuals(
            reauthorization={
                "receiver_human": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:50:00Z",
                    "scope": {"max_actual_minutes": 30},
                },
                "sender_reply": {
                    "decision": "declined",
                    "decided_at_utc": "2026-05-12T12:55:00Z",
                    "source_message_id": "msg-reauth",
                },
            },
        ),
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["action"] == "resumed_after_reauthorization"
    assert checkpoint["reauthorization"]["disposition"] == "resumed"


def test_reauth_sender_scope_bounded_by_receiver_thresholds() -> None:
    # 12 files exceeds the receiver's 5-file admission cap: a sender
    # re-authorization cannot self-serve past the receiver's own policy.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(estimated_minutes=20),
        {"present": False},
        _reauth_actuals(
            actual_files_touched=12,
            reauthorization={
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                    "scope": {"max_actual_minutes": 30, "max_actual_files_touched": 12},
                },
            },
        ),
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["breached"] is True
    assert checkpoint["reauthorization"]["disposition"] == "insufficient"
    assert checkpoint["action"] == "paused_for_reauthorization"


def test_reauth_sender_cannot_grant_merges_pr() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "declared_intent_fields": ["task_profile.merges_pr"],
            "reauthorization": {
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                    "scope": {"merges_pr": True},
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["reauthorization"]["disposition"] == "insufficient"
    assert checkpoint["action"] == "paused_for_reauthorization"


def test_reauth_boundary_action_grant_is_durable_across_pauses() -> None:
    # A stale answer cannot clear a numeric breach (per-checkpoint
    # consumption), but the boundary action it granted stays granted for
    # the remainder of the task.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T13:10:00Z",
            "declared_intent_fields": ["task_profile.merges_pr"],
            "reauthorization": {
                "receiver_human": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:50:00Z",
                    "scope": {"merges_pr": True},
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["reauthorization"]["disposition"] == "resumed"
    assert checkpoint["action"] == "resumed_after_reauthorization"


def test_reauth_fresh_scopeless_receiver_approval_clears_pause_extent() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(estimated_minutes=20),
        {"present": False},
        _reauth_actuals(
            actual_files_touched=12,
            reauthorization={
                "receiver_human": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:50:00Z",
                },
            },
        ),
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["reauthorization"]["disposition"] == "resumed"
    assert checkpoint["reauthorization"]["cleared_paused_at_utc"] == "2026-05-12T12:30:00Z"


def test_reauth_without_breach_records_not_required() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(estimated_minutes=45),
        {"present": False},
        _reauth_actuals(
            reauthorization={
                "receiver_human": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:50:00Z",
                },
            },
        ),
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["breached"] is False
    assert checkpoint["reauthorization"]["disposition"] == "not_required"


def test_reauth_modified_requires_explicit_scope() -> None:
    with pytest.raises(ValueError, match="modified"):
        evaluate_threshold_checkpoint(
            _envelope(estimated_minutes=20),
            {"present": False},
            _reauth_actuals(
                reauthorization={
                    "receiver_human": {
                        "decision": "modified",
                        "decided_at_utc": "2026-05-12T12:50:00Z",
                    },
                },
            ),
            policy=_REAUTH_POLICY,
        )


def test_reauth_rejects_unknown_channel() -> None:
    with pytest.raises(ValueError, match="unknown channel"):
        evaluate_threshold_checkpoint(
            _envelope(estimated_minutes=20),
            {"present": False},
            _reauth_actuals(reauthorization={"slack_dm": {"decision": "approved"}}),
            policy=_REAUTH_POLICY,
        )


def test_reauth_gh_comment_scope_rejected() -> None:
    with pytest.raises(ValueError, match="advisory"):
        evaluate_threshold_checkpoint(
            _envelope(estimated_minutes=20),
            {"present": False},
            _reauth_actuals(
                reauthorization={
                    "gh_comment": {
                        "decision": "approved",
                        "scope": {"max_actual_minutes": 60},
                    },
                },
            ),
            policy=_REAUTH_POLICY,
        )


def test_reauth_without_policy_fails_closed_for_sender() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(estimated_minutes=20),
        {"present": False},
        _reauth_actuals(
            reauthorization={
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                    "scope": {"max_actual_minutes": 30},
                },
            },
        ),
    )
    assert checkpoint["reauthorization"]["disposition"] == "insufficient"


def test_reauth_scopeless_human_approval_clears_boundary_pause() -> None:
    # A fresh scope-less approval clears the CURRENT pause in full —
    # boundary fields included — without creating anything durable.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "declared_intent_fields": ["task_profile.comments_on_github"],
            "reauthorization": {
                "receiver_human": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:50:00Z",
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    reauth = checkpoint["reauthorization"]
    assert reauth["disposition"] == "resumed"
    assert checkpoint["action"] == "resumed_after_reauthorization"
    assert reauth["scope"] is None
    assert reauth["requested_scope"] is None


def test_reauth_stale_scopeless_answer_does_not_clear_boundary_pause() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T13:10:00Z",
            "declared_intent_fields": ["task_profile.comments_on_github"],
            "reauthorization": {
                "receiver_human": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:50:00Z",
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["reauthorization"]["disposition"] == "stale"


def test_reauth_sender_cannot_grant_artifact_on_unlisted_repo() -> None:
    # target_repo is empty/unlisted: the admission predicate would pause
    # this shape, so the sender channel cannot grant it at a checkpoint.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "declared_intent_fields": ["task_profile.creates_or_updates_pr"],
            "reauthorization": {
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                    "scope": {"creates_or_updates_pr": True},
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["reauthorization"]["disposition"] == "insufficient"


def test_reauth_sender_cannot_grant_commit_only_action() -> None:
    # A standalone commits_changes carries no artifact-class anchor, so
    # allow_pr_artifacts would pause it at admission — the sender cannot
    # grant it at a checkpoint either, even on an allowlisted target.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(target_repo="example-org/private-repo"),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "declared_intent_fields": ["task_profile.commits_changes"],
            "reauthorization": {
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                    "scope": {"commits_changes": True},
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["reauthorization"]["disposition"] == "insufficient"


def test_reauth_sender_grants_artifact_on_allowlisted_repo() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(target_repo="example-org/private-repo"),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "declared_intent_fields": ["task_profile.creates_or_updates_pr"],
            "reauthorization": {
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                    "scope": {"creates_or_updates_pr": True},
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    reauth = checkpoint["reauthorization"]
    assert reauth["disposition"] == "resumed"
    assert reauth["scope"]["creates_or_updates_pr"] is True


def test_reauth_scopeless_sender_cannot_clear_ungrantable_boundary() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "declared_intent_fields": ["task_profile.merges_pr"],
            "reauthorization": {
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["reauthorization"]["disposition"] == "insufficient"


def test_reauth_sender_record_preserves_effective_capped_scope() -> None:
    # The durable audit surface records what was actually granted (capped
    # at the receiver's policy), with the raw request kept as provenance.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(estimated_minutes=20),
        {"present": False},
        {
            "actual_minutes": 40,
            "actual_files_touched": 1,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "reauthorization": {
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                    "scope": {"max_actual_minutes": 100},
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    reauth = checkpoint["reauthorization"]
    assert reauth["disposition"] == "resumed"
    assert reauth["scope"]["max_actual_minutes"] == 45
    assert reauth["requested_scope"]["max_actual_minutes"] == 100


@pytest.mark.parametrize(
    "legacy_field",
    [
        "destructive_ops",
        "touches_auth_config_or_secrets",
        "touches_dependencies",
        "public_visibility",
    ],
)
def test_reauth_scopeless_sender_cannot_clear_legacy_risk_boundary(
    legacy_field: str,
) -> None:
    # Legacy risk fields are receiver-side authority only: even a fresh
    # scope-less sender approval on an envelope that already qualifies as
    # an allowlisted PR artifact must not clear them.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(
            target_repo="example-org/private-repo",
            external_side_effects=True,
            creates_or_updates_pr=True,
        ),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "declared_intent_fields": [f"task_profile.{legacy_field}"],
            "reauthorization": {
                "sender_reply": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:40:00Z",
                    "source_message_id": "msg-reauth",
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    assert checkpoint["reauthorization"]["disposition"] == "insufficient"


def test_reauth_scopeless_human_clears_legacy_risk_boundary() -> None:
    # The receiver-side human retains full coverage of the current pause,
    # legacy risk fields included — nothing durable is recorded.
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {
            "actual_minutes": 1,
            "actual_files_touched": 0,
            "paused_at_utc": "2026-05-12T12:30:00Z",
            "declared_intent_fields": ["task_profile.destructive_ops"],
            "reauthorization": {
                "receiver_human": {
                    "decision": "approved",
                    "decided_at_utc": "2026-05-12T12:50:00Z",
                },
            },
        },
        policy=_REAUTH_POLICY,
    )
    reauth = checkpoint["reauthorization"]
    assert reauth["disposition"] == "resumed"
    assert reauth["scope"] is None


# --- Review-loop continuation grants ---


# Fixture grants expire 2026-06-30; accept-path tests evaluate at a pinned
# in-window moment so the real clock can never flip them to expired.
_REVIEW_NOW = datetime.strptime("2026-05-26T12:30:00Z", "%Y-%m-%dT%H:%M:%SZ")


def _review_config() -> Dict[str, Any]:
    return _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )


def _review_body(**overrides: Any) -> str:
    data: Dict[str, Any] = {
        "pr": 88,
        "repo": "example-org/widget",
        "round": 2,
        "branch": "alice/widget-logging",
        "diff_summary": "Round 2 re-review.",
    }
    data.update(overrides)
    return yaml.safe_dump(data, sort_keys=False)


def _review_message(**overrides: Any) -> Dict[str, Any]:
    message = _load_yaml(
        FIXTURE_ROOT / "messages" / "review_continuation_request.yaml"
    )
    message.update(overrides)
    return message


def _write_review_grant_audit(
    tmp_path: Path,
    name: str = "grant.yaml",
    **mutations: Any,
) -> Dict[str, Any]:
    audit = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_grant_approved.yaml"
    )
    scope_mutations = mutations.pop("review_loop", None)
    audit.update(mutations)
    if scope_mutations is not None:
        audit["result"]["human_outcome"]["grant"]["granted_scope"][
            "review_loop"
        ].update(scope_mutations)
    (tmp_path / name).write_text(
        yaml.safe_dump(audit, sort_keys=False), encoding="utf-8"
    )
    return audit


def test_review_feedback_is_context_only() -> None:
    message = _review_message(
        type="review_feedback",
        body="findings_packet: packets/findings/example_r1.yaml\nround: 1\nblocking_count: 1\n",
    )
    decision = evaluate_autonomy(message, _review_config())
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_context_only"]
    assert decision["review_continuation"]["decision"] == "context_only"


def test_review_lgtm_is_context_only(tmp_path: Path) -> None:
    # Reviewer-output types never start reviewer work, grant or no grant.
    _write_review_grant_audit(tmp_path)
    message = _review_message(
        type="review_lgtm",
        body="quality_gate_result: pass\nmerge_ready: true\n",
    )
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["review_continuation"]["decision"] == "context_only"


def test_review_addressed_context_only_without_explicit_type_grant(
    tmp_path: Path,
) -> None:
    _write_review_grant_audit(tmp_path)
    message = _review_message(
        type="review_addressed",
        body=_review_body(commit_sha="a" * 40, changes_summary="fixes"),
    )
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_context_only"]


def test_review_addressed_auto_continues_when_grant_lists_it(
    tmp_path: Path,
) -> None:
    _write_review_grant_audit(
        tmp_path,
        review_loop={"allowed_types": ["review_request", "review_addressed"]},
    )
    message = _review_message(
        type="review_addressed",
        body=_review_body(commit_sha="a" * 40, changes_summary="fixes"),
    )
    decision = evaluate_autonomy(
        message,
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "auto_accepted"
    assert decision["review_continuation"]["decision"] == "accepted"


def test_review_request_without_thread_requires_confirmation(
    tmp_path: Path,
) -> None:
    _write_review_grant_audit(tmp_path)
    message = _review_message()
    message.pop("conversation_id", None)
    message.pop("parent_message_id", None)
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [
        "review_continuation_confirmation_required"
    ]


def test_review_grant_does_not_cross_senders(tmp_path: Path) -> None:
    _write_review_grant_audit(tmp_path)
    message = _review_message(
        **{"from": "bob", "id": "msg-20260526123000-bob-rr2"}
    )
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [
        "review_continuation_confirmation_required"
    ]


def test_review_grant_does_not_cross_receivers(tmp_path: Path) -> None:
    _write_review_grant_audit(tmp_path)
    decision = evaluate_autonomy(
        _review_message(), _review_config(), audit_dir=tmp_path, receiver="claude"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [
        "review_continuation_confirmation_required"
    ]


def test_review_grant_decided_after_message_cannot_govern(
    tmp_path: Path,
) -> None:
    _write_review_grant_audit(tmp_path)
    message = _review_message(created_at_utc="2026-05-26T09:04:00Z")
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [
        "review_continuation_confirmation_required"
    ]


def test_review_grant_expired_pauses(tmp_path: Path) -> None:
    _write_review_grant_audit(
        tmp_path, review_loop={"expires_at_utc": "2026-05-26T11:00:00Z"}
    )
    decision = evaluate_autonomy(
        _review_message(), _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_expired"]


def test_review_grant_cross_repo_pauses(tmp_path: Path) -> None:
    _write_review_grant_audit(tmp_path)
    message = _review_message(body=_review_body(repo="example-org/other"))
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_scope_exceeded"]
    assert decision["review_continuation"]["exceeded_fields"] == ["repository"]


def test_review_request_without_repo_declaration_pauses(tmp_path: Path) -> None:
    # Scope matching fails closed: a request that does not declare its
    # repository cannot be confirmed in-scope.
    _write_review_grant_audit(tmp_path)
    body = _review_body()
    message = _review_message(
        body="\n".join(
            line for line in body.splitlines() if not line.startswith("repo:")
        )
    )
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_scope_exceeded"]
    assert "repository" in decision["review_continuation"]["exceeded_fields"]


def test_review_side_effect_expansion_pauses(tmp_path: Path) -> None:
    _write_review_grant_audit(tmp_path)
    message = _review_message(
        body=_review_body(
            side_effects=[
                "writes_findings_packet",
                "sends_oacp_reply",
                "submits_github_review",
            ]
        )
    )
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_scope_exceeded"]
    assert decision["review_continuation"]["exceeded_fields"] == [
        "permitted_side_effects.submits_github_review"
    ]


def test_review_round_floor_comes_from_receiver_audit_trail(
    tmp_path: Path,
) -> None:
    # Three earlier review_request audits exist in the thread; a sender
    # re-declaring "round: 1" cannot reset the count below the receiver's
    # own floor of 4.
    _write_review_grant_audit(tmp_path)
    for index in range(2):
        _write_review_grant_audit(
            tmp_path,
            name=f"round{index}.yaml",
            message_id=f"msg-20260526100{index}00-alice-r{index}",
        )
    message = _review_message(body=_review_body(round=1))
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_round_exceeded"]
    assert decision["review_continuation"]["effective_round"] == 4


def test_review_grant_invalid_scope_pauses(tmp_path: Path) -> None:
    _write_review_grant_audit(
        tmp_path, review_loop={"allowed_types": ["review_lgtm"]}
    )
    decision = evaluate_autonomy(
        _review_message(), _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_loop_invalid"]
    assert decision["review_continuation"]["decision"] == "invalid"


def test_review_task_grant_without_review_scope_keeps_confirmation(
    tmp_path: Path,
) -> None:
    # A standing task-continuation grant carries no review authority.
    audit = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_grant_approved.yaml"
    )
    del audit["result"]["human_outcome"]["grant"]["granted_scope"]["review_loop"]
    (tmp_path / "grant.yaml").write_text(
        yaml.safe_dump(audit, sort_keys=False), encoding="utf-8"
    )
    decision = evaluate_autonomy(
        _review_message(), _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [
        "review_continuation_confirmation_required"
    ]


def test_review_undeclared_head_is_recorded_not_blocking(tmp_path: Path) -> None:
    _write_review_grant_audit(tmp_path)
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        actuals={"review": {"live_head": "b" * 40}},
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "auto_accepted"
    assert "review_continuation_head_mismatch" not in decision["reason_codes"]
    head_check = decision["review_continuation"]["head_check"]
    assert head_check["status"] == "undeclared"
    assert head_check["live_head"] == "b" * 40


def test_review_short_prefix_declaration_is_mismatch(tmp_path: Path) -> None:
    # A truncated declaration can never satisfy the exact-head guard even
    # when the live head starts with it.
    _write_review_grant_audit(tmp_path)
    live = "c" * 40
    message = _review_message(body=_review_body(declared_head=live[:12]))
    decision = evaluate_autonomy(
        message,
        _review_config(),
        actuals={"review": {"live_head": live}},
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "auto_accepted"
    assert "review_continuation_head_mismatch" in decision["reason_codes"]
    assert decision["review_continuation"]["head_check"]["status"] == "mismatch"


def test_review_replayed_request_pauses(tmp_path: Path) -> None:
    _write_review_grant_audit(tmp_path)
    message = _review_message()
    replay = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_grant_approved.yaml"
    )
    replay["message_id"] = message["id"]
    replay["decision"] = "auto_accepted"
    (tmp_path / "replay.yaml").write_text(
        yaml.safe_dump(replay, sort_keys=False), encoding="utf-8"
    )
    decision = evaluate_autonomy(
        message, _review_config(), audit_dir=tmp_path, receiver="codex"
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["message_replayed"]


def test_review_only_scope_normalizes_with_zero_task_budgets() -> None:
    scope, error = normalize_continuation_scope(
        {
            "review_loop": {
                "repository": "example-org/widget",
                "pr_number": 88,
                "allowed_types": ["review_request"],
                "max_round": 3,
                "expires_at_utc": "2026-06-30T00:00:00Z",
                "permitted_side_effects": {
                    "writes_findings_packet": True,
                    "sends_oacp_reply": True,
                },
            }
        }
    )
    assert error is None
    assert scope["max_actual_minutes"] == 0
    assert scope["max_actual_files_touched"] == 0
    assert scope["review_loop"]["repository"] == "example-org/widget"
    assert scope["review_loop"]["permitted_side_effects"][
        "submits_github_review"
    ] is False


def test_review_scope_without_review_loop_still_requires_budgets() -> None:
    scope, error = normalize_continuation_scope({"creates_or_updates_pr": True})
    assert scope is None
    assert error == "max_actual_minutes_invalid"


def test_review_accepted_audit_record_writes_without_task_envelope(
    tmp_path: Path,
) -> None:
    audit_source = tmp_path / "audits"
    audit_source.mkdir()
    _write_review_grant_audit(audit_source)
    config = _review_config()
    message = _review_message()
    decision = evaluate_autonomy(
        message, config, audit_dir=audit_source, receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "auto_accepted"
    audit_path = write_audit_record(
        tmp_path / "out",
        decision,
        config=config,
        message=message,
        message_path=tmp_path / "message.yaml",
        policy_path=tmp_path / "config.yaml",
        receiver="codex",
    )
    written = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert written["review_continuation"]["decision"] == "accepted"
    assert written["scope_envelope"] is None


def test_review_grant_expired_at_evaluation_time_pauses(tmp_path: Path) -> None:
    # Delayed delivery: the request predates expiry, but the grant is dead
    # by the time the receiver evaluates it. created_at_utc is
    # sender-controlled and must not be able to dodge expiry.
    _write_review_grant_audit(tmp_path)
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=datetime.strptime("2026-07-01T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"),
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_expired"]


def test_review_forward_dated_message_cannot_dodge_expiry(
    tmp_path: Path,
) -> None:
    # Both clocks bound the grant: a message stamped past expiry is expired
    # even when evaluation time is still inside the window.
    _write_review_grant_audit(
        tmp_path, review_loop={"expires_at_utc": "2026-05-26T12:10:00Z"}
    )
    message = _review_message(created_at_utc="2026-05-26T12:15:00Z")
    decision = evaluate_autonomy(
        message,
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=datetime.strptime("2026-05-26T12:05:00Z", "%Y-%m-%dT%H:%M:%SZ"),
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_expired"]


def test_review_denial_before_evaluation_revokes_queued_work(
    tmp_path: Path,
) -> None:
    # Revoke-before-processing: the denial postdates the request but
    # predates evaluation — queued work must not run on the old approval.
    _write_review_grant_audit(tmp_path)
    denial = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_grant_denied_postmessage.yaml"
    )
    (tmp_path / "denial.yaml").write_text(
        yaml.safe_dump(denial, sort_keys=False), encoding="utf-8"
    )
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=datetime.strptime("2026-05-26T12:10:00Z", "%Y-%m-%dT%H:%M:%SZ"),
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_revoked"]


def test_review_regrant_after_denial_restores_continuation(
    tmp_path: Path,
) -> None:
    # A denial is not a tombstone: a newer human approval (still predating
    # the request) re-establishes standing continuation.
    denial = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_grant_denied_later.yaml"
    )
    denial["result"]["human_outcome"]["decided_at_utc"] = "2026-05-26T08:00:00Z"
    (tmp_path / "denial.yaml").write_text(
        yaml.safe_dump(denial, sort_keys=False), encoding="utf-8"
    )
    _write_review_grant_audit(tmp_path)  # approval decided 09:05
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "auto_accepted"
    assert decision["review_continuation"]["decision"] == "accepted"


def test_review_addressed_admissions_consume_round_budget(
    tmp_path: Path,
) -> None:
    # Every admitted invocation consumes a round unit — grant-listed
    # review_addressed rounds included, so they cannot repeat unbounded.
    _write_review_grant_audit(
        tmp_path,
        review_loop={
            "allowed_types": ["review_request", "review_addressed"],
            "max_round": 2,
        },
    )
    accepted_addr = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_addressed_accepted.yaml"
    )
    (tmp_path / "addr1.yaml").write_text(
        yaml.safe_dump(accepted_addr, sort_keys=False), encoding="utf-8"
    )
    message = _review_message(
        type="review_addressed",
        body=_review_body(commit_sha="a" * 40, changes_summary="again"),
    )
    decision = evaluate_autonomy(
        message,
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_round_exceeded"]
    assert decision["review_continuation"]["effective_round"] == 3


def test_review_denial_then_regrant_does_not_burn_rounds(tmp_path: Path) -> None:
    # A declined request never ran a reviewer — it must not charge
    # max_round, so the newer approval re-establishes the full two-round
    # grant it promised.
    denial = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_grant_denied_later.yaml"
    )
    denial["result"]["human_outcome"]["decided_at_utc"] = "2026-05-26T08:00:00Z"
    (tmp_path / "denial.yaml").write_text(
        yaml.safe_dump(denial, sort_keys=False), encoding="utf-8"
    )
    _write_review_grant_audit(tmp_path, review_loop={"max_round": 2})
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "auto_accepted"
    assert decision["review_continuation"]["effective_round"] == 2


def test_review_unanswered_pause_does_not_burn_rounds(tmp_path: Path) -> None:
    # A paused review_request with no recorded human outcome started
    # nothing — it consumes no round budget.
    _write_review_grant_audit(tmp_path, review_loop={"max_round": 2})
    unanswered = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_grant_approved.yaml"
    )
    unanswered["message_id"] = "msg-20260526100000-alice-unanswered"
    del unanswered["result"]["human_outcome"]
    (tmp_path / "unanswered.yaml").write_text(
        yaml.safe_dump(unanswered, sort_keys=False), encoding="utf-8"
    )
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "auto_accepted"
    assert decision["review_continuation"]["effective_round"] == 2


def test_review_aware_utc_now_accepts(tmp_path: Path) -> None:
    # The clock parameter accepts ordinary timezone-aware UTC datetimes,
    # matching message_expired.
    from datetime import timezone

    _write_review_grant_audit(tmp_path)
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW.replace(tzinfo=timezone.utc),
    )
    assert decision["decision"] == "auto_accepted"


def test_review_aware_utc_now_arbitrates_revocation(tmp_path: Path) -> None:
    from datetime import timezone

    _write_review_grant_audit(tmp_path)
    denial = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_grant_denied_postmessage.yaml"
    )
    (tmp_path / "denial.yaml").write_text(
        yaml.safe_dump(denial, sort_keys=False), encoding="utf-8"
    )
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=datetime.strptime(
            "2026-05-26T12:10:00Z", "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc),
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["review_continuation_revoked"]


def test_review_context_only_addressed_audits_do_not_consume_rounds(
    tmp_path: Path,
) -> None:
    _write_review_grant_audit(tmp_path)
    context_only = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_review_addressed_accepted.yaml"
    )
    context_only["decision"] = "paused"
    context_only["reason_codes"] = ["review_continuation_context_only"]
    (tmp_path / "ctx.yaml").write_text(
        yaml.safe_dump(context_only, sort_keys=False), encoding="utf-8"
    )
    decision = evaluate_autonomy(
        _review_message(),
        _review_config(),
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_REVIEW_NOW,
    )
    assert decision["decision"] == "auto_accepted"
    assert decision["review_continuation"]["effective_round"] == 2
