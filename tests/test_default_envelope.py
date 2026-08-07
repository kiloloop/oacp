# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Default scope envelope for profileless admitted requests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from autonomy_gate import (  # noqa: E402
    DEFAULT_PROFILELESS_ENVELOPE_FILES,
    DEFAULT_PROFILELESS_ENVELOPE_MINUTES,
    SCOPE_ENVELOPE_SOURCE_DEFAULT,
    SCOPE_ENVELOPE_SOURCE_PROFILE,
    default_scope_envelope,
    evaluate_autonomy,
    write_audit_record,
)

FIXTURE_ROOT = Path(__file__).parent / "conformance" / "autonomy"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _standard_config() -> dict:
    return _load(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")


def _profileless_brainstorm() -> dict:
    return _load(FIXTURE_ROOT / "messages" / "brainstorm_without_profile.yaml")


def test_profileless_brainstorm_receives_default_envelope() -> None:
    message = _profileless_brainstorm()
    decision = evaluate_autonomy(message, _standard_config())

    assert decision["decision"] == "auto_accepted"
    assert "task_profile_not_required" in decision["reason_codes"]
    envelope = decision["scope_envelope"]
    assert envelope is not None
    assert envelope["estimated_minutes"] == DEFAULT_PROFILELESS_ENVELOPE_MINUTES
    assert envelope["expected_files_touched"] == DEFAULT_PROFILELESS_ENVELOPE_FILES
    assert envelope["sends_oacp_reply_only"] is True
    assert envelope["risk_tier"] == "P3"  # mirrors the message priority
    for key in (
        "destructive_ops",
        "external_side_effects",
        "touches_auth_config_or_secrets",
        "touches_dependencies",
        "public_visibility",
        "creates_or_updates_pr",
        "comments_on_github",
        "commits_changes",
        "merges_pr",
        "files_issues",
    ):
        assert envelope[key] is False, key
    assert decision["scope_envelope_source"] == SCOPE_ENVELOPE_SOURCE_DEFAULT


def test_default_risk_tier_falls_back_to_p2() -> None:
    message = _profileless_brainstorm()
    message["priority"] = "urgent"
    assert default_scope_envelope(message)["risk_tier"] == "P2"
    message.pop("priority")
    assert default_scope_envelope(message)["risk_tier"] == "P2"


def test_voluntary_profile_on_exempt_type_overrides_default() -> None:
    message = _profileless_brainstorm()
    message["body"] = (
        "Explore options, bounded by an honest voluntary profile.\n"
        "\n"
        "task_profile:\n"
        "  estimated_minutes: 30\n"
        "  risk_tier: P1\n"
        "  expected_files_touched: 3\n"
        "  destructive_ops: false\n"
        "  external_side_effects: false\n"
        "  touches_auth_config_or_secrets: false\n"
        "  touches_dependencies: false\n"
        "  public_visibility: false\n"
        "  sends_oacp_reply_only: true\n"
    )
    decision = evaluate_autonomy(message, _standard_config())

    assert decision["decision"] == "auto_accepted"
    assert "task_profile_present" in decision["reason_codes"]
    envelope = decision["scope_envelope"]
    assert envelope["estimated_minutes"] == 30
    assert envelope["expected_files_touched"] == 3
    assert envelope["risk_tier"] == "P1"
    assert decision["scope_envelope_source"] == SCOPE_ENVELOPE_SOURCE_PROFILE


def test_profiled_task_request_source_is_task_profile() -> None:
    message = _load(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    decision = evaluate_autonomy(message, _standard_config())
    assert decision["decision"] == "auto_accepted"
    assert decision["scope_envelope_source"] == SCOPE_ENVELOPE_SOURCE_PROFILE


def test_pause_before_envelope_construction_has_null_source() -> None:
    message = _profileless_brainstorm()
    message.pop("subject")  # schema-invalid: pauses at Gate 1
    decision = evaluate_autonomy(message, _standard_config())
    assert decision["decision"] == "paused"
    assert decision["scope_envelope"] is None
    assert decision["scope_envelope_source"] is None


def test_default_envelope_binds_the_threshold_checkpoint() -> None:
    # The ruled trade-off, exercised: a long profileless run now pauses
    # against the default bound instead of running silently.
    message = _profileless_brainstorm()
    actuals = {
        "actual_minutes": DEFAULT_PROFILELESS_ENVELOPE_MINUTES + 10,
        "actual_files_touched": 0,
        "side_effects_actual": {},
    }
    decision = evaluate_autonomy(message, _standard_config(), actuals)
    assert decision["decision"] == "paused"
    assert decision["result"]["completion_kind"] == "checkpoint_paused"
    assert decision["result"]["threshold_checkpoint"]["breached"] is True
    assert decision["breached"] == ["actual_minutes"]


def test_audit_writer_refuses_admitted_null_envelope(tmp_path: Path) -> None:
    message = _profileless_brainstorm()
    config = _standard_config()
    decision = evaluate_autonomy(message, config)
    assert decision["decision"] == "auto_accepted"
    decision["scope_envelope"] = None  # simulate the pre-fix unbounded shape

    with pytest.raises(ValueError, match="schema violation"):
        write_audit_record(
            tmp_path,
            decision,
            config=config,
            message=message,
            message_path=tmp_path / "message.yaml",
            policy_path=tmp_path / "config.yaml",
            receiver="codex",
        )
    assert not list(tmp_path.glob("*.yaml"))


def test_admitted_record_with_default_envelope_persists(tmp_path: Path) -> None:
    message = _profileless_brainstorm()
    config = _standard_config()
    decision = evaluate_autonomy(message, config)
    audit_path = write_audit_record(
        tmp_path,
        decision,
        config=config,
        message=message,
        message_path=tmp_path / "message.yaml",
        policy_path=tmp_path / "config.yaml",
        receiver="codex",
    )
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert audit["scope_envelope"]["estimated_minutes"] == (
        DEFAULT_PROFILELESS_ENVELOPE_MINUTES
    )
    assert audit["scope_envelope_source"] == SCOPE_ENVELOPE_SOURCE_DEFAULT
