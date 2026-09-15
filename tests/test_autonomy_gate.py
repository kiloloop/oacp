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
from typing import Any, Dict, List, Optional

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from autonomy_gate import (  # noqa: E402
    ADMISSION_AXES,
    BREACH_BASES,
    BREACH_SUB_BASES,
    PINNED_COMPLETION_KINDS,
    PINNED_REASON_CODES,
    RUNTIME_MODEL_ENV_VAR,
    _base_result,
    admission_ledger_codes,
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

        if "result" in expected:
            _assert_subset(expected["result"], decision["result"])

        if "breached" in expected:
            assert decision["breached"] == expected["breached"]

        if "co_occurring_reason_codes" in expected:
            assert (
                decision["co_occurring_reason_codes"]
                == expected["co_occurring_reason_codes"]
            ), expected_path.name

        if "admission_axes" in expected:
            # The ledger is pinned exactly: an extra or missing axis entry
            # is a contract change, not implementation detail.
            assert decision["admission_axes"] == expected["admission_axes"], (
                expected_path.name
            )

        for field in (
            "pause_classification", "expected_pause_codes", "unplanned_pause_codes",
        ):
            if field in expected:
                assert decision[field] == expected[field], expected_path.name

        if "task_profile" in expected:
            _assert_subset(expected["task_profile"], decision["task_profile"])

        if "scope_envelope_source" in expected:
            # Every admitted envelope names where it came from; pinning the
            # source pins the profileless default-envelope path itself.
            assert (
                decision["scope_envelope_source"] == expected["scope_envelope_source"]
            ), expected_path.name

        if "scope_envelope" in expected:
            _assert_subset(expected["scope_envelope"], decision["scope_envelope"])

        for hit in decision["matched_patterns"]:
            assert set(hit) == {
                "pattern",
                "category",
                "span",
                "demotion_basis",
            }, expected_path.name
            assert set(hit["span"]) == {"start", "end"}, expected_path.name
            start, end = hit["span"]["start"], hit["span"]["end"]
            assert 0 <= start < end <= len(message["body"]), expected_path.name
            assert hit["demotion_basis"], expected_path.name

        if decision.get("matched_pattern"):
            blocking_hits = [
                hit
                for hit in decision["matched_patterns"]
                if hit["pattern"] == decision["matched_pattern"]
            ]
            assert blocking_hits, expected_path.name
            assert any(
                hit["demotion_basis"] in {"affirmative", "non_demotable"}
                for hit in blocking_hits
            ), expected_path.name

        assert set(decision["reason_codes"]) <= PINNED_REASON_CODES
        assert "completed_at_utc" in decision["result"]


def test_autonomy_gate_output_uses_canonical_final_states() -> None:
    allowed = {"pending", "done", "paused", "blocked", "superseded", "error", "cancelled"}
    for expected_path in sorted((FIXTURE_ROOT / "expected").glob("*.yaml")):
        fixture = _load_yaml(expected_path)
        config = _load_yaml(FIXTURE_ROOT / fixture["config"])
        message = _load_yaml(FIXTURE_ROOT / fixture["message"])
        actuals = _load_yaml(FIXTURE_ROOT / fixture["actuals"]) if fixture.get("actuals") else None

        decision = evaluate_autonomy(message, config, actuals=actuals)
        assert decision["result"]["final_state"] in allowed
        assert decision["schema_version"] == 2
        assert "human_outcome" in decision["result"]


def test_auto_accepted_admission_is_born_pending() -> None:
    """The evaluator never writes `done`: every auto-accepted birth is
    `pending` with no completion stamp, and only finalization closes it."""
    for expected_path in sorted((FIXTURE_ROOT / "expected").glob("*.yaml")):
        fixture = _load_yaml(expected_path)
        config = _load_yaml(FIXTURE_ROOT / fixture["config"])
        message = _load_yaml(FIXTURE_ROOT / fixture["message"])
        actuals = _load_yaml(FIXTURE_ROOT / fixture["actuals"]) if fixture.get("actuals") else None

        decision = evaluate_autonomy(message, config, actuals=actuals)
        result = decision["result"]
        if decision["decision"] == "auto_accepted":
            assert result["final_state"] == "pending", expected_path.name
            assert result["completion_kind"] == "auto_accepted", expected_path.name
            assert result["completed_at_utc"] is None, expected_path.name
        else:
            assert result["final_state"] != "done", expected_path.name


def test_auto_accepted_birth_ignores_actuals_completion_stamp() -> None:
    # A checkpoint pass may carry the clock endpoint; it stays inside the
    # checkpoint block, never promoted to the record's completion evidence.
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    actuals = _load_yaml(FIXTURE_ROOT / "actuals" / "top_level_side_effect_keys.yaml")
    assert actuals["completed_at_utc"]

    decision = evaluate_autonomy(message, config, actuals=actuals)

    assert decision["decision"] == "auto_accepted"
    assert decision["result"]["final_state"] == "pending"
    assert decision["result"]["completed_at_utc"] is None
    assert decision["result"]["threshold_checkpoint"]["completed_at_utc"] == actuals["completed_at_utc"]


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
    assert decision["result"]["work_started_at_utc"] is None


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
    ("opening_fence", "closing_fence"),
    [("```yaml", "```"), ("~~~~yaml", "~~~~")],
    ids=["backticks", "tildes"],
)
def test_fenced_profile_decoy_does_not_shadow_real_declaration(
    opening_fence: str,
    closing_fence: str,
) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    message["body"] = (
        "## Example\n"
        f"{opening_fence}\n"
        "task_profile:\n"
        "  estimated_minutes: [malformed decoy\n"
        f"{closing_fence}\n\n"
        f"{message['body']}"
    )

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "auto_accepted"
    assert decision["task_profile"]["estimated_minutes"] == 20
    assert decision["task_profile"]["expected_files_touched"] == 1


@pytest.mark.parametrize(
    ("opening_fence", "closing_fence"),
    [("```yaml", "```"), ("~~~~yaml", "~~~~")],
    ids=["backticks", "tildes"],
)
def test_fenced_only_profile_remains_operational(
    opening_fence: str,
    closing_fence: str,
) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    message["body"] = (
        f"{opening_fence}\n"
        f"{message['body']}\n"
        f"{closing_fence}"
    )

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "auto_accepted"
    assert decision["task_profile"]["estimated_minutes"] == 20
    assert decision["task_profile"]["expected_files_touched"] == 1


def test_unclosed_fence_does_not_hide_only_profile() -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    message["body"] = f"```yaml\n{message['body']}"

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "auto_accepted"
    assert decision["task_profile"]["estimated_minutes"] == 20
    assert decision["task_profile"]["expected_files_touched"] == 1


def _lexical_fp_decision(task_text: str) -> tuple[Dict[str, Any], str]:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    body = message["body"].replace(
        "Update one documentation paragraph for clarity.",
        task_text,
    )
    message["body"] = body
    return evaluate_autonomy(message, config), body


def _assert_lexical_hit(
    decision: Dict[str, Any],
    body: str,
    pattern: str,
    demotion_basis: str,
) -> None:
    hits = [
        hit for hit in decision["matched_patterns"] if hit["pattern"] == pattern
    ]
    assert hits
    for hit in hits:
        assert set(hit) == {"pattern", "category", "span", "demotion_basis"}
        assert hit["demotion_basis"] == demotion_basis
        assert set(hit["span"]) == {"start", "end"}
        start, end = hit["span"]["start"], hit["span"]["end"]
        assert 0 <= start < end <= len(body)


_DECLARED_CAPABILITY_PROFILE: Dict[str, Any] = {
    "estimated_minutes": 20,
    "risk_tier": "P2",
    "expected_files_touched": 2,
    "destructive_ops": False,
    "external_side_effects": False,
    "touches_auth_config_or_secrets": False,
    "touches_dependencies": False,
    "public_visibility": False,
}
_PRIVATE_PR_ARTIFACT: Dict[str, Any] = {
    "external_side_effects": True,
    "target_repo": "example-org/private-repo",
    "creates_or_updates_pr": True,
}
_INSTALL_TEXT = "Install the dependency the parser needs and pin it in the lockfile."
_ROTATE_TEXT = "Rotate credentials for the staging bot before the cut."
_PUSH_TEXT = "Push to main once the review approves."


def _declared_capability_decision(
    task_text: str,
    **overrides: Any,
) -> tuple[Dict[str, Any], str]:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "clean_task.yaml")
    profile = {**_DECLARED_CAPABILITY_PROFILE, **overrides}
    block = "\n".join(
        f"  {line}" for line in yaml.safe_dump(profile, sort_keys=False).splitlines()
    )
    body = f"## Task\n{task_text}\n\ntask_profile:\n{block}\n"
    message["body"] = body
    return evaluate_autonomy(message, config), body


@pytest.mark.parametrize(
    ("task_text", "pattern", "field", "reason_code", "notes"),
    [
        (
            _INSTALL_TEXT,
            "install dependency",
            "touches_dependencies",
            "dependency_changes_pause",
            ["install dependency"],
        ),
        (
            _ROTATE_TEXT,
            "rotate credentials",
            "touches_auth_config_or_secrets",
            "auth_config_or_secrets_pause",
            ["rotate credentials", "credentials"],
        ),
    ],
)
def test_declared_capability_routes_the_partnered_head_to_its_granular_pause(
    task_text: str,
    pattern: str,
    field: str,
    reason_code: str,
    notes: list[str],
) -> None:
    decision, body = _declared_capability_decision(task_text, **{field: True})

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [reason_code]
    assert "matched_pattern" not in decision
    assert [note["matched_pattern"] for note in decision["logged_notes"]] == notes
    assert {note["code"] for note in decision["logged_notes"]} == {
        "lexical_advisory_declared"
    }
    _assert_lexical_hit(decision, body, pattern, "profile_true")


@pytest.mark.parametrize(
    ("task_text", "pattern", "field"),
    [
        (_INSTALL_TEXT, "install dependency", "touches_dependencies"),
        (_ROTATE_TEXT, "rotate credentials", "touches_auth_config_or_secrets"),
    ],
)
def test_declared_false_keeps_the_partnered_head_hard(
    task_text: str,
    pattern: str,
    field: str,
) -> None:
    decision, body = _declared_capability_decision(task_text, **{field: False})

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == pattern
    assert decision["logged_notes"] == []
    _assert_lexical_hit(decision, body, pattern, "non_demotable")


@pytest.mark.parametrize(
    ("task_text", "pattern", "field"),
    [
        (_INSTALL_TEXT, "install dependency", "touches_dependencies"),
        (_ROTATE_TEXT, "rotate credentials", "touches_auth_config_or_secrets"),
    ],
)
def test_contradictory_profile_keeps_the_partnered_head_hard(
    task_text: str,
    pattern: str,
    field: str,
) -> None:
    # A PR artifact declared against `external_side_effects: false` is a
    # declaration error: the gate trusts none of that profile's flags.
    decision, body = _declared_capability_decision(
        task_text, **{field: True, "creates_or_updates_pr": True}
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == pattern
    assert "declaration_error" in decision["co_occurring_reason_codes"]
    assert decision["logged_notes"] == []
    _assert_lexical_hit(decision, body, pattern, "non_demotable")


@pytest.mark.parametrize(
    "overrides",
    [
        {**_PRIVATE_PR_ARTIFACT, "merges_pr": True},
        {**_PRIVATE_PR_ARTIFACT},
    ],
)
def test_default_branch_push_head_has_no_declarable_partner(
    overrides: Dict[str, Any],
) -> None:
    decision, body = _declared_capability_decision(_PUSH_TEXT, **overrides)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "push to main"
    assert decision["logged_notes"] == []
    _assert_lexical_hit(decision, body, "push to main", "non_demotable")


def test_declared_capability_negated_occurrence_keeps_its_own_basis() -> None:
    # Contextual demotion is occurrence-scoped and evaluated first: a negated
    # occurrence records `negated`, not `profile_true`, and the declared flag
    # still carries the verdict.
    decision, body = _declared_capability_decision(
        "Do not install dependencies for the parser.", touches_dependencies=True
    )

    assert decision["reason_codes"] == ["dependency_changes_pause"]
    assert [note["code"] for note in decision["logged_notes"]] == [
        "lexical_advisory_negated"
    ]
    _assert_lexical_hit(decision, body, "install dependency", "negated")


@pytest.mark.parametrize(
    ("task_text", "pattern"),
    [
        ("Update the auth helper documentation.", "auth"),
        ("Update the secret handling docs.", "secrets"),
        ("Update the credential helper documentation.", "credentials"),
        ("Update the runtime config files for the local agent.", "config"),
    ],
)
def test_declared_true_sensitive_scope_routes_to_the_granular_pause(
    task_text: str,
    pattern: str,
) -> None:
    decision, body = _declared_capability_decision(
        task_text, touches_auth_config_or_secrets=True
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["auth_config_or_secrets_pause"]
    assert "matched_pattern" not in decision
    assert decision["logged_notes"] == [
        {"code": "lexical_advisory_declared", "matched_pattern": pattern}
    ]
    _assert_lexical_hit(decision, body, pattern, "profile_true")


def test_contradictory_profile_keeps_sensitive_scope_hard() -> None:
    decision, body = _declared_capability_decision(
        "Update the auth helper documentation.",
        touches_auth_config_or_secrets=True,
        creates_or_updates_pr=True,
    )

    assert decision["reason_codes"] == ["hard_stop_sensitive_scope"]
    assert decision["matched_pattern"] == "auth"
    assert "declaration_error" in decision["co_occurring_reason_codes"]
    _assert_lexical_hit(decision, body, "auth", "affirmative")


def test_lexical_fp_merge_method_reference_fixture() -> None:
    decision, body = _lexical_fp_decision(
        "Document the repository's squash-only merge method."
    )

    assert decision["decision"] == "auto_accepted"
    assert decision["reason_codes"][-2:] == ["lexical_advisory", "workspace_check_required"]
    _assert_lexical_hit(decision, body, "merge", "reference_only")


@pytest.mark.parametrize(
    "task_text",
    [
        "Describe what pip install pulls into the runtime dependencies.",
        "Explain how install/build is read as dependency-class behavior.",
    ],
)
def test_lexical_fp_descriptive_install_reference_fixture(task_text: str) -> None:
    decision, body = _lexical_fp_decision(task_text)

    assert decision["decision"] == "auto_accepted"
    assert decision["reason_codes"][-2:] == ["lexical_advisory", "workspace_check_required"]
    _assert_lexical_hit(decision, body, "install dependency", "reference_only")


def test_lexical_fp_negated_non_demotable_contexts_fixture() -> None:
    decision, body = _lexical_fp_decision(
        "Out of scope: anything on the public repo. Do not install dependencies."
    )

    assert decision["decision"] == "auto_accepted"
    assert decision["reason_codes"][-2:] == ["lexical_advisory", "workspace_check_required"]
    _assert_lexical_hit(decision, body, "public repo", "negated")
    _assert_lexical_hit(decision, body, "install dependency", "negated")


def test_non_demotable_context_demotion_does_not_cross_clause() -> None:
    decision, body = _lexical_fp_decision(
        "Do not install a local tool. Install dependencies for the task."
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "install dependency"
    _assert_lexical_hit(decision, body, "install dependency", "non_demotable")


def test_install_reference_does_not_demote_affirmative_match_in_same_clause() -> None:
    decision, _body = _lexical_fp_decision(
        "Install the new dependencies after you note what pip install pulls "
        "for runtime dependencies"
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "install dependency"
    install_hits = [
        hit
        for hit in decision["matched_patterns"]
        if hit["pattern"] == "install dependency"
    ]
    assert [hit["demotion_basis"] for hit in install_hits] == [
        "non_demotable",
        "reference_only",
    ]


def test_install_reference_does_not_demote_later_affirmative_match() -> None:
    decision, _body = _lexical_fp_decision(
        "Describe what pip install pulls for runtime dependencies before you "
        "install new dependencies"
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "install dependency"
    install_hits = [
        hit
        for hit in decision["matched_patterns"]
        if hit["pattern"] == "install dependency"
    ]
    assert [hit["demotion_basis"] for hit in install_hits] == [
        "reference_only",
        "non_demotable",
    ]


def test_public_repo_negation_does_not_cross_exception_in_same_clause() -> None:
    decision, _body = _lexical_fp_decision(
        "Out of scope: the public repo except publish to the public repo"
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_sensitive_scope"]
    assert decision["matched_pattern"] == "public repo"
    public_repo_hits = [
        hit
        for hit in decision["matched_patterns"]
        if hit["pattern"] == "public repo"
    ]
    assert [hit["demotion_basis"] for hit in public_repo_hits] == [
        "negated",
        "non_demotable",
    ]


def test_wrapped_public_repository_provenance_matches_hard_stop() -> None:
    decision, body = _lexical_fp_decision(
        "Out of scope: anything on the public\nrepository."
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_sensitive_scope"]
    assert decision["matched_pattern"] == "public repo"
    _assert_lexical_hit(decision, body, "public repo", "non_demotable")


def test_install_match_does_not_bridge_negated_and_affirmative_occurrences() -> None:
    decision, body = _lexical_fp_decision(
        "Do not install tooling except install the runtime dependencies"
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "install dependency"
    _assert_lexical_hit(decision, body, "install dependency", "non_demotable")


def test_install_match_with_internal_negation_remains_hard() -> None:
    decision, body = _lexical_fp_decision(
        "Do not install tooling without runtime dependencies"
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "install dependency"
    _assert_lexical_hit(decision, body, "install dependency", "non_demotable")


def test_one_negation_cannot_demote_two_install_dependency_occurrences() -> None:
    decision, _body = _lexical_fp_decision(
        "Do not install development dependencies while install runtime dependencies"
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "install dependency"
    install_hits = [
        hit
        for hit in decision["matched_patterns"]
        if hit["pattern"] == "install dependency"
    ]
    assert [hit["demotion_basis"] for hit in install_hits] == [
        "negated",
        "non_demotable",
    ]


def test_fresh_negation_can_govern_later_install_dependency_occurrence() -> None:
    decision, body = _lexical_fp_decision(
        "Do not install development dependencies while do not install runtime "
        "dependencies"
    )

    assert decision["decision"] == "auto_accepted"
    assert decision["reason_codes"][-2:] == [
        "lexical_advisory",
        "workspace_check_required",
    ]
    _assert_lexical_hit(decision, body, "install dependency", "negated")


@pytest.mark.parametrize(
    "intervening_wording",
    [
        "aside from",
        "other than",
        "apart from",
        "besides",
        "save for",
        "excluding",
        "though",
        "whereas",
        "yet",
        "instead",
        "unrecognized connective",
    ],
)
def test_one_negation_cannot_demote_two_public_repo_occurrences(
    intervening_wording: str,
) -> None:
    decision, _body = _lexical_fp_decision(
        "Out of scope: the public repo "
        f"{intervening_wording} publish to the public repo"
    )

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_sensitive_scope"]
    assert decision["matched_pattern"] == "public repo"
    public_repo_hits = [
        hit
        for hit in decision["matched_patterns"]
        if hit["pattern"] == "public repo"
    ]
    assert [hit["demotion_basis"] for hit in public_repo_hits] == [
        "negated",
        "non_demotable",
    ]


def test_fresh_negation_can_govern_later_public_repo_occurrence() -> None:
    decision, body = _lexical_fp_decision(
        "Out of scope: the public repo whereas do not publish to the public repo"
    )

    assert decision["decision"] == "auto_accepted"
    assert decision["reason_codes"][-2:] == [
        "lexical_advisory",
        "workspace_check_required",
    ]
    _assert_lexical_hit(decision, body, "public repo", "negated")


@pytest.mark.parametrize(
    "task_text",
    [
        "Do not touch the staging config, but publish the docs to the public repo",
        "Do not touch the staging config, except publish the docs to the public repo",
        "Do not touch the staging config, however publish the docs to the public repo",
        "Out of scope: the staging config, but publish the docs to the public repo",
        "Do not touch the staging config, aside from publish to the public repo",
        "Do not touch the staging config, then publish to the public repo",
    ],
)
def test_unrelated_negation_does_not_demote_first_public_repo_occurrence(
    task_text: str,
) -> None:
    decision, body = _lexical_fp_decision(task_text)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_sensitive_scope"]
    assert decision["matched_pattern"] == "public repo"
    _assert_lexical_hit(decision, body, "public repo", "non_demotable")


@pytest.mark.parametrize(
    "task_text",
    [
        "Do not review anything, but install the runtime dependencies",
        "Do not change the lockfile except install the runtime dependencies",
        "Avoid touching the lockfile, but install the runtime dependencies",
        "Skip the cleanup step, then install the runtime dependencies",
    ],
)
def test_unrelated_negation_does_not_demote_first_install_occurrence(
    task_text: str,
) -> None:
    decision, body = _lexical_fp_decision(task_text)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    assert decision["matched_pattern"] == "install dependency"
    _assert_lexical_hit(decision, body, "install dependency", "non_demotable")


@pytest.mark.parametrize(
    ("task_text", "pattern"),
    [
        ("Do not publish to the public repo", "public repo"),
        ("Avoid using the public repo", "public repo"),
        (
            "Do not run package installs that affect runtime dependencies",
            "install dependency",
        ),
        ("Avoid installing runtime dependencies", "install dependency"),
    ],
)
def test_direct_negation_still_governs_first_hard_stop_occurrence(
    task_text: str,
    pattern: str,
) -> None:
    decision, body = _lexical_fp_decision(task_text)

    assert decision["decision"] == "auto_accepted"
    assert decision["reason_codes"][-2:] == [
        "lexical_advisory",
        "workspace_check_required",
    ]
    _assert_lexical_hit(decision, body, pattern, "negated")


@pytest.mark.parametrize(
    ("task_text", "pattern", "reason_code"),
    [
        (
            "Do not proceed without installing the runtime dependencies",
            "install dependency",
            "hard_stop_external_side_effect",
        ),
        (
            "There is no reason to skip installing the runtime dependencies",
            "install dependency",
            "hard_stop_external_side_effect",
        ),
        (
            "Do not skip installing the runtime dependencies",
            "install dependency",
            "hard_stop_external_side_effect",
        ),
        (
            "Do not avoid installing the runtime dependencies",
            "install dependency",
            "hard_stop_external_side_effect",
        ),
        (
            "Never skip installing the runtime dependencies",
            "install dependency",
            "hard_stop_external_side_effect",
        ),
        (
            "Do not exclude installing the runtime dependencies",
            "install dependency",
            "hard_stop_external_side_effect",
        ),
        (
            "Do not skip the public repo",
            "public repo",
            "hard_stop_sensitive_scope",
        ),
        (
            "Do not avoid the public repo",
            "public repo",
            "hard_stop_sensitive_scope",
        ),
        (
            "Do not exclude the public repo",
            "public repo",
            "hard_stop_sensitive_scope",
        ),
        (
            "Never avoid the public repo",
            "public repo",
            "hard_stop_sensitive_scope",
        ),
        (
            "Out of scope: do not publish to the public repo",
            "public repo",
            "hard_stop_sensitive_scope",
        ),
    ],
)
def test_stacked_negation_cannot_demote_first_hard_stop_occurrence(
    task_text: str,
    pattern: str,
    reason_code: str,
) -> None:
    decision, body = _lexical_fp_decision(task_text)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [reason_code]
    assert decision["matched_pattern"] == pattern
    _assert_lexical_hit(decision, body, pattern, "non_demotable")


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
    assert decision["reason_codes"][0] == "continuation_grant_revoked"
    assert decision["continuation_grant"]["decision"] == "revoked"


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


# ── Checkpoint sub-basis (waiting_on_peer) ────────────────────────────────────


def _time_breach_actuals(**overrides: Any) -> Dict[str, Any]:
    actuals: Dict[str, Any] = {"actual_minutes": 20, "actual_files_touched": 1}
    actuals.update(overrides)
    return actuals


def test_waiting_on_peer_sub_basis_stamps_on_realized_time_breach() -> None:
    checkpoint = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        _time_breach_actuals(breach_sub_basis="waiting_on_peer"),
    )
    assert checkpoint["breached"] is True
    assert checkpoint["breached_fields"] == ["actual_minutes"]
    assert checkpoint["breach_basis"] == "realized"
    assert checkpoint["breach_sub_basis"] == "waiting_on_peer"


def test_sub_basis_is_null_on_every_other_checkpoint() -> None:
    unbreached = evaluate_threshold_checkpoint(
        _envelope(),
        {"present": False},
        {"actual_minutes": 5, "actual_files_touched": 1},
    )
    assert unbreached["breach_sub_basis"] is None
    breached = evaluate_threshold_checkpoint(
        _envelope(), {"present": False}, _time_breach_actuals()
    )
    assert breached["breach_basis"] == "realized"
    assert breached["breach_sub_basis"] is None
    unevaluated = evaluate_threshold_checkpoint(None, {"present": False}, None)
    assert unevaluated["breach_sub_basis"] is None


@pytest.mark.parametrize(
    ("actuals", "match"),
    [
        # unbreached checkpoint: nothing to refine
        (
            _time_breach_actuals(actual_minutes=5, breach_sub_basis="waiting_on_peer"),
            "breached checkpoint",
        ),
        # files-only breach: the time axis did not overrun
        (
            _time_breach_actuals(
                actual_minutes=5,
                actual_files_touched=3,
                breach_sub_basis="waiting_on_peer",
            ),
            "time-axis",
        ),
        # prospective breach: nothing realized, so nothing was spent waiting
        (
            {
                "actual_minutes": 1,
                "actual_files_touched": 0,
                "declared_intent_fields": ["task_profile.merges_pr"],
                "breach_sub_basis": "waiting_on_peer",
            },
            "realized",
        ),
        # off-vocabulary value
        (_time_breach_actuals(breach_sub_basis="reviewing"), "breach_sub_basis must be"),
    ],
)
def test_sub_basis_rejected_where_it_cannot_apply(
    actuals: Dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        evaluate_threshold_checkpoint(_envelope(), {"present": False}, actuals)


def test_breach_basis_grammar_is_enumerated() -> None:
    # The finite grammar, pinned: a new basis or sub-basis is a contract
    # change that lands here together with the spec, never silently.
    assert BREACH_BASES == ("declared_intent", "realized")
    assert BREACH_SUB_BASES == ("waiting_on_peer",)
    for basis in BREACH_BASES:
        for sub_basis in BREACH_SUB_BASES:
            actuals = _time_breach_actuals(
                breach_basis=basis, breach_sub_basis=sub_basis
            )
            if basis == "realized":
                checkpoint = evaluate_threshold_checkpoint(
                    _envelope(), {"present": False}, actuals
                )
                assert checkpoint["breach_basis"] == basis
                assert checkpoint["breach_sub_basis"] == sub_basis
            else:
                # declared_intent on a time overrun is already inconsistent
                # input; a sub-basis never rescues it.
                with pytest.raises(ValueError):
                    evaluate_threshold_checkpoint(
                        _envelope(), {"present": False}, actuals
                    )
    for bad in ("", "WAITING_ON_PEER", "waiting-on-peer", "peer_wait", "realized"):
        with pytest.raises(ValueError, match="breach_sub_basis"):
            evaluate_threshold_checkpoint(
                _envelope(),
                {"present": False},
                _time_breach_actuals(breach_sub_basis=bad),
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
    assert checkpoint["reauthorization"]["cleared_paused_at_utc"] == "2026-05-12T12:50:00Z"


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


# --- Generic thread continuation grants ---


_THREAD_NOW = datetime.strptime("2026-05-26T12:30:00Z", "%Y-%m-%dT%H:%M:%SZ")
_THREAD_TYPES = (
    "task_request",
    "question",
    "brainstorm_request",
    "brainstorm_followup",
    "handoff",
    "review_request",
    "review_addressed",
)


def _thread_config() -> Dict[str, Any]:
    return _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )


def _thread_scope(**overrides: Any) -> Dict[str, Any]:
    scope = {
        "allowed_types": list(_THREAD_TYPES),
        "max_round": 3,
        "expires_at_utc": "2026-06-30T00:00:00Z",
        "max_actual_minutes": 30,
        "max_actual_files_touched": 3,
        "writes_findings_packet": True,
        "sends_oacp_reply": True,
    }
    scope.update(overrides)
    return scope


def _thread_message(
    message_type: str = "review_request", **overrides: Any
) -> Dict[str, Any]:
    message = _load_yaml(FIXTURE_ROOT / "messages" / "thread_grant_request.yaml")
    message["type"] = message_type
    body: Dict[str, Any] = {"round": 2, "description": "Continue the bounded work."}
    if message_type not in {
        "review_request",
        "review_addressed",
        "review_feedback",
        "review_lgtm",
    }:
        body["task_profile"] = {
            "estimated_minutes": 10,
            "risk_tier": "P2",
            "expected_files_touched": 1,
            "destructive_ops": False,
            "external_side_effects": False,
            "touches_auth_config_or_secrets": False,
            "touches_dependencies": False,
            "public_visibility": False,
            "sends_oacp_reply_only": True,
        }
    if message_type == "handoff":
        body.update(
            {
                "source_agent": "alice",
                "target_agent": "codex",
                "intent": "Continue bounded work",
                "artifacts_to_review": ["notes.txt"],
                "definition_of_done": ["Write the conclusion"],
                "context_bundle": {
                    "files_touched": [
                        {"path": "notes.txt", "rationale": "Source material"}
                    ],
                    "decisions_made": [
                        {
                            "decision": "Read the notes",
                            "alternatives_considered": ["Recreate notes"],
                        }
                    ],
                    "blockers_hit": [
                        {"blocker": "none", "workarounds_attempted": ["n/a"]}
                    ],
                    "suggested_next_steps": ["Complete the conclusion"],
                },
            }
        )
    if message_type == "handoff":

        class IndentedDumper(yaml.SafeDumper):
            def increase_indent(
                self, flow: bool = False, indentless: bool = False
            ) -> Any:
                return super().increase_indent(flow, False)

        message["body"] = yaml.dump(body, Dumper=IndentedDumper, sort_keys=False)
    else:
        message["body"] = yaml.safe_dump(body, sort_keys=False)
    message.update(overrides)
    return message


def _write_thread_audit(tmp_path: Path, audit: Dict[str, Any], name: str) -> None:
    (tmp_path / name).write_text(
        yaml.safe_dump(audit, sort_keys=False), encoding="utf-8"
    )


def _write_thread_grant_audit(
    tmp_path: Path,
    name: str = "grant.yaml",
    *,
    scope: Optional[Dict[str, Any]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    audit = _load_yaml(FIXTURE_ROOT / "audits" / "prior_generic_grant_approved.yaml")
    audit["result"]["human_outcome"]["grant"]["granted_scope"] = (
        _thread_scope() if scope is None else scope
    )
    audit.update(overrides)
    _write_thread_audit(tmp_path, audit, name)
    return audit


def _thread_decision(
    tmp_path: Path,
    message: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    kwargs.setdefault("now_utc", _THREAD_NOW)
    return evaluate_autonomy(
        _thread_message() if message is None else message,
        _thread_config(),
        audit_dir=tmp_path,
        receiver=kwargs.pop("receiver", "codex"),
        **kwargs,
    )


@pytest.mark.parametrize("message_type", _THREAD_TYPES)
def test_thread_grant_explicitly_allows_each_work_type(
    tmp_path: Path, message_type: str
) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(tmp_path, _thread_message(message_type))
    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["decision"] == "accepted"
    assert decision["continuation_grant"]["effective_round"] == 2
    assert "review_continuation" not in decision


@pytest.mark.parametrize("message_type", ["review_feedback", "review_lgtm"])
def test_reviewer_outputs_never_start_work(tmp_path: Path, message_type: str) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(tmp_path, _thread_message(message_type))
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["continuation_grant_type_not_granted"]
    assert decision["continuation_grant"]["decision"] == "context_only"


@pytest.mark.parametrize("message_type", _THREAD_TYPES)
def test_thread_grant_requires_explicit_type_membership(
    tmp_path: Path, message_type: str
) -> None:
    allowed = [kind for kind in _THREAD_TYPES if kind != message_type]
    _write_thread_grant_audit(tmp_path, scope=_thread_scope(allowed_types=allowed))
    decision = _thread_decision(tmp_path, _thread_message(message_type))
    assert decision["decision"] == "paused"
    assert "continuation_grant_type_not_granted" in decision["reason_codes"]


@pytest.mark.parametrize(
    "changes",
    [
        {"from": "bob", "id": "msg-20260526120000-bob-0002"},
        {
            "conversation_id": "conv-20260526-other-001",
            "parent_message_id": "msg-20260526110000-alice-other",
        },
    ],
)
def test_thread_grant_does_not_cross_sender_or_thread(
    tmp_path: Path, changes: Dict[str, Any]
) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(tmp_path, _thread_message(**changes))
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["continuation_grant_missing_approval"]


def test_thread_grant_does_not_cross_receivers(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(tmp_path, receiver="claude")
    assert decision["reason_codes"] == ["continuation_grant_missing_approval"]


def test_thread_grant_requires_thread_binding(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    message = _thread_message()
    message.pop("conversation_id")
    message.pop("parent_message_id")
    assert _thread_decision(tmp_path, message)["reason_codes"] == [
        "continuation_grant_missing_thread"
    ]


def test_thread_grant_parent_only_binding_still_works(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    message = _thread_message()
    message.pop("conversation_id")
    assert _thread_decision(tmp_path, message)["decision"] == "auto_accepted"


def test_thread_grant_approval_is_not_retroactive(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    message = _thread_message(created_at_utc="2026-05-26T09:04:00Z")
    assert _thread_decision(tmp_path, message)["reason_codes"] == [
        "continuation_grant_missing_approval"
    ]


def test_thread_grant_recognition_off_does_not_honor_stored_approval(
    tmp_path: Path,
) -> None:
    _write_thread_grant_audit(tmp_path)
    config = _thread_config()
    config["autonomy"]["continuation_grants"]["enabled"] = False
    decision = evaluate_autonomy(
        _thread_message(),
        config,
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_THREAD_NOW,
    )
    assert decision["reason_codes"] == ["continuation_grant_ignored_disabled"]


def test_thread_grant_sender_claim_is_not_approval(tmp_path: Path) -> None:
    message = _thread_message(
        body=yaml.safe_dump(
            {
                "round": 2,
                "continuation_grants": {
                    "approved_thread_continuation": {"scope": _thread_scope()}
                },
            }
        )
    )
    decision = _thread_decision(tmp_path, message)
    assert decision["reason_codes"] == ["continuation_grant_missing_approval"]


def test_review_without_a_grant_requires_approval(tmp_path: Path) -> None:
    assert _thread_decision(tmp_path)["reason_codes"] == [
        "continuation_grant_missing_approval"
    ]


def test_ordinary_task_without_grant_keeps_normal_admission(tmp_path: Path) -> None:
    decision = _thread_decision(tmp_path, _thread_message("task_request"))
    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["decision"] == "not_present"


@pytest.mark.parametrize(
    "message_time,evaluation_time",
    [
        ("2026-06-30T00:00:01Z", "2026-06-29T23:59:59Z"),
        ("2026-05-26T12:05:00Z", "2026-06-30T00:00:01Z"),
    ],
)
def test_thread_grant_expiry_uses_both_clocks(
    tmp_path: Path, message_time: str, evaluation_time: str
) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(
        tmp_path,
        _thread_message(created_at_utc=message_time),
        now_utc=datetime.strptime(evaluation_time, "%Y-%m-%dT%H:%M:%SZ"),
    )
    assert decision["reason_codes"] == ["continuation_grant_expired"]


def test_thread_grant_accepts_exact_expiry_boundary(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    boundary = "2026-06-30T00:00:00Z"
    decision = _thread_decision(
        tmp_path,
        _thread_message(created_at_utc=boundary),
        now_utc=datetime.strptime(boundary, "%Y-%m-%dT%H:%M:%SZ"),
    )
    assert decision["decision"] == "auto_accepted"


def test_thread_grant_future_approval_cannot_govern_forward_dated_message(
    tmp_path: Path,
) -> None:
    audit = _write_thread_grant_audit(tmp_path)
    audit["result"]["human_outcome"]["decided_at_utc"] = "2026-05-26T12:31:00Z"
    _write_thread_audit(tmp_path, audit, "grant.yaml")
    message = _thread_message(created_at_utc="2026-05-26T12:32:00Z")
    assert _thread_decision(tmp_path, message)["reason_codes"] == [
        "continuation_grant_missing_approval"
    ]


def test_thread_grant_accepts_timezone_aware_clock(tmp_path: Path) -> None:
    from datetime import timezone

    _write_thread_grant_audit(tmp_path)
    assert (
        _thread_decision(tmp_path, now_utc=_THREAD_NOW.replace(tzinfo=timezone.utc))[
            "decision"
        ]
        == "auto_accepted"
    )


@pytest.mark.parametrize(
    "denied_at,expected",
    [
        ("2026-05-26T12:10:00Z", "revoked"),
        ("2026-05-26T09:05:00Z", "revoked"),
        ("2026-05-26T12:31:00Z", "accepted"),
    ],
)
def test_thread_grant_denial_arbitrates_on_evaluation_clock(
    tmp_path: Path, denied_at: str, expected: str
) -> None:
    _write_thread_grant_audit(tmp_path)
    denial = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_generic_grant_denied_postmessage.yaml"
    )
    denial["result"]["human_outcome"]["decided_at_utc"] = denied_at
    _write_thread_audit(tmp_path, denial, "denial.yaml")
    decision = _thread_decision(tmp_path)
    assert decision["continuation_grant"]["decision"] == expected
    if expected == "revoked":
        assert decision["reason_codes"] == ["continuation_grant_revoked"]


def test_thread_grant_newer_approval_restores_revoked_authority(tmp_path: Path) -> None:
    denial = _load_yaml(
        FIXTURE_ROOT / "audits" / "prior_generic_grant_denied_later.yaml"
    )
    denial["result"]["human_outcome"]["decided_at_utc"] = "2026-05-26T08:00:00Z"
    _write_thread_audit(tmp_path, denial, "denial.yaml")
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(tmp_path)
    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["effective_round"] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"round": 4},
        {"round": 0},
        {"round": True},
        {"round": "2"},
    ],
)
def test_thread_grant_declared_round_cannot_escape_bounds(
    tmp_path: Path, changes: Dict[str, Any]
) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(tmp_path, _thread_message(body=yaml.safe_dump(changes)))
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [
        "continuation_grant_round_exceeded"
        if changes["round"] == 4
        else "continuation_grant_scope_exceeded"
    ]


def test_thread_grant_missing_round_uses_receiver_floor(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(
        tmp_path, _thread_message(body="description: Continue the work.\n")
    )
    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["effective_round"] == 2


def test_thread_grant_round_floor_counts_unique_started_mixed_types(
    tmp_path: Path,
) -> None:
    _write_thread_grant_audit(tmp_path)
    for index, kind in enumerate(["question", "review_addressed"], start=1):
        audit = _load_yaml(FIXTURE_ROOT / "audits" / "prior_thread_invocation.yaml")
        audit.update(
            message_id=f"msg-20260526113000-alice-start{index}", message_type=kind
        )
        _write_thread_audit(tmp_path, audit, f"started{index}.yaml")
        _write_thread_audit(tmp_path, audit, f"duplicate{index}.yaml")
    decision = _thread_decision(tmp_path, _thread_message(body="round: 1\n"))
    assert decision["reason_codes"] == ["continuation_grant_round_exceeded"]
    assert decision["continuation_grant"]["effective_round"] == 4


@pytest.mark.parametrize(
    "ignored",
    [
        "approved_unstarted",
        "auto_accepted_unstarted",
        "denied",
        "unanswered",
        "context_only",
        "superseded",
        "future_started",
        "future_audit",
        "other_sender",
        "other_receiver",
        "other_thread",
        "output_type",
    ],
)
def test_thread_grant_round_floor_excludes_non_invocations(
    tmp_path: Path, ignored: str
) -> None:
    _write_thread_grant_audit(tmp_path, scope=_thread_scope(max_round=2))
    extra = _load_yaml(FIXTURE_ROOT / "audits" / "prior_thread_invocation.yaml")
    extra["message_id"] = "msg-20260526113000-alice-extra"
    if ignored in {"approved_unstarted", "denied", "unanswered", "context_only"}:
        extra["decision"] = "paused"
    if ignored in {
        "approved_unstarted",
        "auto_accepted_unstarted",
        "denied",
        "unanswered",
        "context_only",
    }:
        extra["result"].pop("work_started_at_utc", None)
    if ignored in {"approved_unstarted", "denied"}:
        extra["result"]["human_outcome"] = {
            "recorded": True,
            "decision": "approved" if ignored == "approved_unstarted" else "declined",
            "decided_at_utc": "2026-05-26T11:31:00Z",
        }
    elif ignored == "context_only":
        extra["decision"] = "auto_accepted"
        extra["message_type"] = "review_addressed"
        extra["result"]["work_started_at_utc"] = "2026-05-26T11:31:00Z"
        extra["continuation_grant"] = {"decision": "context_only"}
    elif ignored == "superseded":
        extra["result"]["final_state"] = "superseded"
    elif ignored == "future_started":
        extra["result"]["work_started_at_utc"] = "2026-05-26T12:31:00Z"
    elif ignored == "future_audit":
        extra["created_at_utc"] = "2026-05-26T12:31:00Z"
    elif ignored == "other_sender":
        extra["sender"] = "bob"
    elif ignored == "other_receiver":
        extra["receiver"] = "claude"
    elif ignored == "other_thread":
        extra["conversation_id"] = "conv-20260526-other-001"
    elif ignored == "output_type":
        extra["message_type"] = "review_feedback"
    if ignored == "denied":
        extra["result"]["actual_minutes"] = 1
    _write_thread_audit(tmp_path, extra, "extra.yaml")
    decision = _thread_decision(tmp_path)
    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["effective_round"] == 2


@pytest.mark.parametrize(
    "evidence",
    [
        "work_started_at_utc",
        "actual_minutes",
        "actual_files_touched",
        "terminal_completion",
    ],
)
def test_thread_grant_round_floor_accepts_actual_execution_evidence(
    tmp_path: Path, evidence: str
) -> None:
    _write_thread_grant_audit(tmp_path, scope=_thread_scope(max_round=2))
    extra = _load_yaml(FIXTURE_ROOT / "audits" / "prior_thread_invocation.yaml")
    extra["result"].pop("work_started_at_utc")
    if evidence == "work_started_at_utc":
        extra["result"][evidence] = "2026-05-26T11:31:00Z"
    elif evidence == "terminal_completion":
        extra["result"].update(
            final_state="done", completed_at_utc="2026-05-26T11:45:00Z"
        )
    else:
        extra["result"][evidence] = 1
    _write_thread_audit(tmp_path, extra, "started.yaml")
    decision = _thread_decision(tmp_path)
    assert decision["reason_codes"] == ["continuation_grant_round_exceeded"]
    assert decision["continuation_grant"]["effective_round"] == 3


def test_thread_grant_replayed_message_pauses(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    replay = _load_yaml(FIXTURE_ROOT / "audits" / "prior_thread_invocation.yaml")
    replay["message_id"] = _thread_message()["id"]
    _write_thread_audit(tmp_path, replay, "replay.yaml")
    assert _thread_decision(tmp_path)["reason_codes"] == ["message_replayed"]


def test_thread_grant_does_not_parse_workflow_subject_or_head(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    message = _thread_message(
        body=yaml.safe_dump(
            {
                "round": 2,
                "repo": "example-org/another",
                "pr": 999,
                "declared_head": "a" * 12,
                "subject": "Changed workflow artifact",
            }
        )
    )
    decision = _thread_decision(
        tmp_path,
        message,
        actuals={
            "actual_minutes": 0,
            "actual_files_touched": 0,
            "review": {"live_head": "b" * 40},
        },
    )
    assert decision["decision"] == "auto_accepted"
    assert "head_check" not in decision["continuation_grant"]
    assert not any("head_mismatch" in code for code in decision["reason_codes"])


def test_thread_grant_extra_side_effect_pauses(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    message = _thread_message(
        body=yaml.safe_dump(
            {
                "round": 2,
                "side_effects": [
                    "writes_findings_packet",
                    "sends_oacp_reply",
                    "submits_github_review",
                ],
            }
        )
    )
    decision = _thread_decision(tmp_path, message)
    assert decision["reason_codes"] == ["continuation_grant_scope_exceeded"]
    assert "submits_github_review" in decision["continuation_grant"]["exceeded_fields"]


def test_thread_grant_authorized_review_side_effect_accepts(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path, scope=_thread_scope(submits_github_review=True))
    message = _thread_message(
        body="round: 2\nside_effects: [writes_findings_packet, sends_oacp_reply, submits_github_review]\n"
    )
    assert _thread_decision(tmp_path, message)["decision"] == "auto_accepted"


def test_review_grant_defaults_still_require_findings_and_reply(tmp_path: Path) -> None:
    _write_thread_grant_audit(
        tmp_path, scope=_thread_scope(writes_findings_packet=False)
    )
    decision = _thread_decision(tmp_path)
    assert decision["reason_codes"] == ["continuation_grant_scope_exceeded"]
    assert "writes_findings_packet" in decision["continuation_grant"]["exceeded_fields"]


@pytest.mark.parametrize(
    "actuals,field",
    [
        ({"actual_minutes": 31, "actual_files_touched": 1}, "actual_minutes"),
        ({"actual_minutes": 1, "actual_files_touched": 4}, "actual_files_touched"),
        (
            {
                "actual_minutes": 1,
                "actual_files_touched": 1,
                "side_effects_actual": {"submits_github_review": True},
            },
            "side_effects_actual.submits_github_review",
        ),
    ],
)
def test_grant_derived_review_envelope_enforces_checkpoint_bounds(
    tmp_path: Path, actuals: Dict[str, Any], field: str
) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(tmp_path, actuals=actuals)
    assert decision["decision"] == "paused"
    checkpoint = decision["result"]["threshold_checkpoint"]
    assert checkpoint["breached"] is True
    assert field in checkpoint["breached_fields"]


def test_generic_grant_never_bypasses_task_lexical_gate(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path)
    message = _thread_message("task_request")
    body = yaml.safe_load(message["body"])
    body["description"] = "Run rm -rf on the output directory."
    message["body"] = yaml.safe_dump(body, sort_keys=False)
    decision = _thread_decision(tmp_path, message)
    assert decision["decision"] == "paused"
    assert "hard_stop_destructive_command" in decision["reason_codes"]


def test_grant_derived_review_bound_is_durable(tmp_path: Path) -> None:
    audit_source = tmp_path / "source"
    audit_source.mkdir()
    _write_thread_grant_audit(audit_source)
    message = _thread_message()
    decision = _thread_decision(audit_source, message)
    assert decision["decision"] == "auto_accepted"
    assert decision["scope_envelope_source"] == "continuation_grant"
    assert decision["scope_envelope"]["estimated_minutes"] == 30
    assert decision["scope_envelope"]["expected_files_touched"] == 3
    path = write_audit_record(
        tmp_path / "out",
        decision,
        config=_thread_config(),
        message=message,
        message_path=tmp_path / "message.yaml",
        policy_path=tmp_path / "policy.yaml",
        receiver="codex",
    )
    written = _load_yaml(path)
    assert written["continuation_grant"]["decision"] == "accepted"
    assert written["scope_envelope"] == decision["scope_envelope"]
    assert "review_continuation" not in written


@pytest.mark.parametrize(
    "body",
    [
        "round: 4\nround: 1\n",
        "round: 4\nside_effects: [submits_github_review\n",
    ],
)
def test_thread_grant_rejects_ambiguous_or_broken_generic_declarations(
    tmp_path: Path,
    body: str,
) -> None:
    _write_thread_grant_audit(tmp_path)
    decision = _thread_decision(tmp_path, _thread_message(body=body))
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["continuation_grant_scope_exceeded"]


@pytest.mark.parametrize(
    "enabled,reason",
    [
        (True, "continuation_grant_missing_approval"),
        (False, "continuation_grant_ignored_disabled"),
    ],
)
@pytest.mark.parametrize("malformed", [False, True], ids=["valid-list", "broken-list"])
def test_profiled_question_cannot_self_authorize_declared_generic_effect(
    tmp_path: Path,
    enabled: bool,
    reason: str,
    malformed: bool,
) -> None:
    message = _thread_message("question")
    if malformed:
        message["body"] += "\nside_effects: [submits_github_review\n"
    else:
        body = yaml.safe_load(message["body"])
        body["side_effects"] = ["submits_github_review"]
        message["body"] = yaml.safe_dump(body, sort_keys=False)
    config = _thread_config()
    config["autonomy"]["continuation_grants"]["enabled"] = enabled
    decision = evaluate_autonomy(
        message,
        config,
        audit_dir=tmp_path,
        receiver="codex",
        now_utc=_THREAD_NOW,
    )
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [reason]


@pytest.mark.parametrize("has_standing_grant", [False, True])
def test_plain_markdown_profiled_task_keeps_normal_grant_matching(
    tmp_path: Path,
    has_standing_grant: bool,
) -> None:
    if has_standing_grant:
        _write_thread_grant_audit(tmp_path)
    message = _thread_message("task_request")
    profile = yaml.safe_load(message["body"])["task_profile"]
    profile_text = yaml.safe_dump(profile, sort_keys=False)
    message["body"] = (
        "## Task\nRead the module and record a conclusion.\n\n"
        "task_profile:\n"
        + "\n".join("  " + line for line in profile_text.splitlines())
        + "\n"
    )
    decision = _thread_decision(tmp_path, message)
    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["decision"] == (
        "accepted" if has_standing_grant else "not_present"
    )


def test_thread_round_floor_ignores_future_manual_authorization(tmp_path: Path) -> None:
    _write_thread_grant_audit(tmp_path, scope=_thread_scope(max_round=2))
    extra = _load_yaml(FIXTURE_ROOT / "audits" / "prior_thread_invocation.yaml")
    extra["decision"] = "paused"
    extra["result"]["human_outcome"] = {
        "recorded": True,
        "decision": "approved",
        "decided_at_utc": "2026-05-26T12:31:00Z",
    }
    _write_thread_audit(tmp_path, extra, "future-approval.yaml")
    decision = _thread_decision(tmp_path)
    assert decision["decision"] == "auto_accepted"
    assert decision["continuation_grant"]["effective_round"] == 2


_GRANT_GRAMMAR = _load_yaml(FIXTURE_ROOT / "thread_grants" / "corpus.yaml")


@pytest.mark.parametrize(
    "case", _GRANT_GRAMMAR["scope_cases"], ids=lambda case: case["case"]
)
def test_generic_scope_grammar_matrix(case: Dict[str, Any]) -> None:
    scope = dict(_GRANT_GRAMMAR["base_scope"])
    scope.update(case.get("set", {}))
    for key in case.get("remove", []):
        scope.pop(key, None)
    normalized, error = normalize_continuation_scope(scope)
    if case["valid"]:
        assert error is None
        assert normalized["allowed_types"] == sorted(set(scope["allowed_types"]))
        for key in (
            "max_round",
            "expires_at_utc",
            "max_actual_minutes",
            "max_actual_files_touched",
        ):
            assert normalized[key] == scope[key]
    else:
        assert normalized is None
        assert error == case["error"]


@pytest.mark.parametrize(
    "scope",
    [
        {"max_actual_minutes": 30, "max_actual_files_touched": 3},
        {
            "review_loop": {
                "repository": "example-org/widget",
                "pr_number": 88,
                "allowed_types": ["review_request"],
                "max_round": 3,
                "expires_at_utc": "2026-06-30T00:00:00Z",
            }
        },
    ],
)
def test_stored_legacy_scope_needs_fresh_bounded_grant(
    tmp_path: Path, scope: Dict[str, Any]
) -> None:
    _write_thread_grant_audit(tmp_path, scope=scope)
    decision = _thread_decision(tmp_path)
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["continuation_grant_missing_scope"]
    assert decision["continuation_grant"]["decision"] == "invalid"


# ── evaluation identity and supersession ─────────────────────────────────


def _run_gate_cli(config_path: Path, message_path: Path, audit_dir: Path) -> int:
    return autonomy_main([
        "--config",
        str(config_path),
        "--message",
        str(message_path),
        "--audit-dir",
        str(audit_dir),
        "--receiver",
        "codex",
    ])


def test_audit_record_carries_evaluation_identity(tmp_path: Path) -> None:
    config_path = FIXTURE_ROOT / "configs" / "auto_review_standard.yaml"
    message_path = FIXTURE_ROOT / "messages" / "ambiguous_scope.yaml"
    audit_dir = tmp_path / "audit" / "autonomy_decisions"

    assert _run_gate_cli(config_path, message_path, audit_dir) == 0

    (record_path,) = audit_dir.glob("*.yaml")
    record = _load_yaml(record_path)
    assert re.fullmatch(r"eval-[0-9a-f]{16}", record["evaluation_id"])
    assert record["supersedes_evaluation_id"] is None


def test_identical_reevaluation_adopts_instead_of_duplicating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = FIXTURE_ROOT / "configs" / "auto_review_standard.yaml"
    message_path = FIXTURE_ROOT / "messages" / "ambiguous_scope.yaml"
    audit_dir = tmp_path / "audit" / "autonomy_decisions"

    assert _run_gate_cli(config_path, message_path, audit_dir) == 0
    capsys.readouterr()
    (record_path,) = audit_dir.glob("*.yaml")
    record = _load_yaml(record_path)

    assert _run_gate_cli(config_path, message_path, audit_dir) == 0
    output = capsys.readouterr()
    decision = json.loads(output.out)
    assert "adopted existing evaluation" in output.err
    assert decision["evaluation_id"] == record["evaluation_id"]
    assert decision["adopted_audit_record"] == str(record_path)
    assert list(audit_dir.glob("*.yaml")) == [record_path]


def test_amended_message_supersedes_prior_evaluation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = FIXTURE_ROOT / "configs" / "auto_review_standard.yaml"
    message_path = tmp_path / "message.yaml"
    shutil.copy(FIXTURE_ROOT / "messages" / "ambiguous_scope.yaml", message_path)
    audit_dir = tmp_path / "audit" / "autonomy_decisions"

    assert _run_gate_cli(config_path, message_path, audit_dir) == 0
    capsys.readouterr()
    (first_path,) = audit_dir.glob("*.yaml")
    first = _load_yaml(first_path)

    amended = _load_yaml(message_path)
    amended["body"] += "\n\nAmendment: narrow the sweep to docs/ only.\n"
    message_path.write_text(
        yaml.safe_dump(amended, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    assert _run_gate_cli(config_path, message_path, audit_dir) == 0
    output = capsys.readouterr()
    assert "superseded 1 prior evaluation(s)" in output.err
    decision = json.loads(output.out)

    records = {path: _load_yaml(path) for path in audit_dir.glob("*.yaml")}
    assert len(records) == 2
    stale = records[first_path]
    (successor,) = [rec for path, rec in records.items() if path != first_path]
    assert stale["result"]["final_state"] == "superseded"
    assert stale["result"]["completed_at_utc"]
    assert stale["superseded_by_evaluation_id"] == successor["evaluation_id"]
    assert successor["supersedes_evaluation_id"] == first["evaluation_id"]
    assert decision["supersedes_evaluation_id"] == first["evaluation_id"]


# ── round-1 review regressions (F-001, F-006) ────────────────────────────


def _minimal_paused_decision(message_sha: str) -> Dict[str, Any]:
    return {
        "decision": "paused",
        "mode": "auto_review",
        "reason_codes": ["estimated_minutes_exceeds_threshold"],
        "scope_envelope": None,
        "message_sha256": message_sha,
        "policy_sha256": "p0licy",
        "schema_version": 2,
        "receiver": "codex",
        "message_id": "msg-20260801010000-alice-race",
        "result": {
            "final_state": "paused",
            "completion_kind": "admission_paused",
        },
    }


def test_persist_evaluation_serializes_logical_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent different-byte evaluations leave exactly one live record.

    The sleep after the prior scan guarantees the scans overlap unless the
    whole scan/write/supersede sequence is serialized by logical identity.
    """
    import threading
    import time

    import autonomy_gate

    audit_dir = tmp_path / "autonomy_decisions"
    message = {"id": "msg-20260801010000-alice-race", "subject": "race"}

    real_scan = autonomy_gate.find_prior_evaluations

    def slow_scan(*args: Any, **kwargs: Any) -> Any:
        result = real_scan(*args, **kwargs)
        time.sleep(0.15)
        return result

    monkeypatch.setattr(autonomy_gate, "find_prior_evaluations", slow_scan)

    outcomes: Dict[str, Dict[str, Any]] = {}

    def run(sha: str) -> None:
        outcomes[sha] = autonomy_gate.persist_evaluation(
            audit_dir,
            _minimal_paused_decision(sha),
            config={},
            message=message,
            message_path=Path("/dev/null"),
            policy_path=Path("/dev/null"),
            receiver="codex",
        )

    threads = [threading.Thread(target=run, args=(sha,)) for sha in ("aa11", "bb22")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in audit_dir.glob("*.yaml")
    ]
    assert len(records) == 2
    live = [r for r in records if r["result"]["final_state"] != "superseded"]
    stale = [r for r in records if r["result"]["final_state"] == "superseded"]
    assert len(live) == 1
    assert len(stale) == 1
    assert stale[0]["superseded_by_evaluation_id"] == live[0]["evaluation_id"]


def test_persist_evaluation_adopt_self_heals_crashed_transaction(tmp_path: Path) -> None:
    """Adoption supersedes a live prior left behind by a crashed writer."""
    import autonomy_gate

    audit_dir = tmp_path / "autonomy_decisions"
    message = {"id": "msg-20260801010000-alice-race", "subject": "race"}

    first = autonomy_gate.persist_evaluation(
        audit_dir,
        _minimal_paused_decision("aa11"),
        config={},
        message=message,
        message_path=Path("/dev/null"),
        policy_path=Path("/dev/null"),
        receiver="codex",
    )
    # Simulate the crashed half-transaction: a second different-byte record
    # written without its predecessor ever being superseded.
    stale_path = Path(first["audit_path"])
    stale = yaml.safe_load(stale_path.read_text(encoding="utf-8"))
    crashed = dict(stale)
    crashed["message_sha256"] = "bb22"
    crashed["evaluation_id"] = "eval-00000000000000bb"
    crashed_path = audit_dir / "20990101T000000Z_msg-crashed.yaml"
    crashed_path.write_text(yaml.safe_dump(crashed, sort_keys=False), encoding="utf-8")

    outcome = autonomy_gate.persist_evaluation(
        audit_dir,
        _minimal_paused_decision("bb22"),
        config={},
        message=message,
        message_path=Path("/dev/null"),
        policy_path=Path("/dev/null"),
        receiver="codex",
    )
    assert outcome["action"] == "adopted"
    assert outcome["evaluation_id"] == "eval-00000000000000bb"
    healed = yaml.safe_load(stale_path.read_text(encoding="utf-8"))
    assert healed["result"]["final_state"] == "superseded"
    assert healed["superseded_by_evaluation_id"] == "eval-00000000000000bb"


def test_supersede_fails_closed_on_duplicate_key_evidence(tmp_path: Path) -> None:
    """Automatic supersession must not normalize duplicate-key YAML."""
    from autonomy_gate import DuplicateKeyError, supersede_audit_record

    fixture = (
        FIXTURE_ROOT / "records" / "duplicate_yaml_key_logged_notes.yaml"
    )
    target = tmp_path / fixture.name
    shutil.copy(fixture, target)
    original = target.read_bytes()

    with pytest.raises(DuplicateKeyError):
        supersede_audit_record(target, superseded_by="eval-0123456789abcdef")
    assert target.read_bytes() == original


def test_persist_evaluation_fails_closed_on_malformed_predecessor(tmp_path: Path) -> None:
    """A live predecessor that cannot be superseded fails the transaction.

    No successor may be written and no success reported while ambiguous
    predecessor evidence stays live — partial success recreates exactly
    the duplicate-live shape the transaction exists to prevent.
    """
    import autonomy_gate

    audit_dir = tmp_path / "autonomy_decisions"
    audit_dir.mkdir(parents=True)
    malformed = audit_dir / "20260801T010000Z_msg-race.yaml"
    malformed.write_text(
        "schema_version: 2\n"
        "receiver: codex\n"
        "message_id: msg-20260801010000-alice-race\n"
        "message_sha256: aa11\n"
        "policy_sha256: p0licy\n"
        "decision: paused\n"
        "logged_notes:\n"
        "- code: lexical_advisory_declared\n"
        "  matched_pattern: merge\n"
        "logged_notes: []\n"
        "result:\n"
        "  final_state: paused\n"
        "  completion_kind: admission_paused\n",
        encoding="utf-8",
    )
    original = malformed.read_bytes()

    with pytest.raises(ValueError, match="logical-identity transaction failed"):
        autonomy_gate.persist_evaluation(
            audit_dir,
            _minimal_paused_decision("bb22"),
            config={},
            message={"id": "msg-20260801010000-alice-race", "subject": "race"},
            message_path=Path("/dev/null"),
            policy_path=Path("/dev/null"),
            receiver="codex",
        )
    assert malformed.read_bytes() == original
    remaining = sorted(path.name for path in audit_dir.glob("*.yaml"))
    assert remaining == [malformed.name]


def test_persist_evaluation_oserror_after_write_rolls_back_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem failure during supersession restores the pre-call state.

    Catching only validation errors would leave the just-written successor
    live next to the never-closed predecessor — two live evaluations for
    one logical identity.
    """
    import autonomy_gate

    audit_dir = tmp_path / "autonomy_decisions"
    message = {"id": "msg-20260801010000-alice-race", "subject": "race"}

    first = autonomy_gate.persist_evaluation(
        audit_dir,
        _minimal_paused_decision("aa11"),
        config={},
        message=message,
        message_path=Path("/dev/null"),
        policy_path=Path("/dev/null"),
        receiver="codex",
    )
    predecessor_path = Path(first["audit_path"])
    original = predecessor_path.read_bytes()

    def broken_supersede(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        autonomy_gate, "_supersede_audit_record_locked", broken_supersede
    )
    with pytest.raises(ValueError, match="pre-call state restored"):
        autonomy_gate.persist_evaluation(
            audit_dir,
            _minimal_paused_decision("bb22"),
            config={},
            message=message,
            message_path=Path("/dev/null"),
            policy_path=Path("/dev/null"),
            receiver="codex",
        )
    assert predecessor_path.read_bytes() == original
    remaining = sorted(path.name for path in audit_dir.glob("*.yaml"))
    assert remaining == [predecessor_path.name]


def test_persist_evaluation_partial_supersession_restores_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure after one of two predecessor writes restores both.

    Rolling back only the successor would leave the first predecessor
    superseded_by an evaluation that no longer exists — the dangling
    link the sweep flags as superseded_missing_successor.
    """
    import autonomy_gate

    audit_dir = tmp_path / "autonomy_decisions"
    message = {"id": "msg-20260801010000-alice-race", "subject": "race"}

    first = autonomy_gate.persist_evaluation(
        audit_dir,
        _minimal_paused_decision("aa11"),
        config={},
        message=message,
        message_path=Path("/dev/null"),
        policy_path=Path("/dev/null"),
        receiver="codex",
    )
    first_path = Path(first["audit_path"])
    stale = yaml.safe_load(first_path.read_text(encoding="utf-8"))
    crashed = dict(stale)
    crashed["message_sha256"] = "cc33"
    crashed["evaluation_id"] = "eval-00000000000000cc"
    crashed_path = audit_dir / "20990101T000000Z_msg-crashed.yaml"
    crashed_path.write_text(yaml.safe_dump(crashed, sort_keys=False), encoding="utf-8")
    original_first = first_path.read_bytes()
    original_crashed = crashed_path.read_bytes()

    real_supersede = autonomy_gate._supersede_audit_record_locked
    calls = {"count": 0}

    def flaky_supersede(path: Path, **kwargs: Any) -> None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise ValueError("second predecessor write refused")
        real_supersede(path, **kwargs)

    monkeypatch.setattr(
        autonomy_gate, "_supersede_audit_record_locked", flaky_supersede
    )
    with pytest.raises(ValueError, match="pre-call state restored"):
        autonomy_gate.persist_evaluation(
            audit_dir,
            _minimal_paused_decision("bb22"),
            config={},
            message=message,
            message_path=Path("/dev/null"),
            policy_path=Path("/dev/null"),
            receiver="codex",
        )
    assert calls["count"] == 2
    assert first_path.read_bytes() == original_first
    assert crashed_path.read_bytes() == original_crashed
    remaining = sorted(path.name for path in audit_dir.glob("*.yaml"))
    assert remaining == sorted([first_path.name, crashed_path.name])


def test_persist_evaluation_serializes_with_per_record_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conforming per-record writer is never erased by rollback.

    The transaction holds every affected record's locked_audit across
    capture, mutation, and restore — a writer that takes the documented
    per-record lock mid-transaction (human-outcome or message_auth
    style) blocks until the rollback completes, and its committed update
    lands on the restored record instead of being overwritten by it.
    """
    import threading
    import time

    import autonomy_gate
    from _oacp_constants import atomic_replace_yaml, locked_audit

    audit_dir = tmp_path / "autonomy_decisions"
    message = {"id": "msg-20260801010000-alice-race", "subject": "race"}

    first = autonomy_gate.persist_evaluation(
        audit_dir,
        _minimal_paused_decision("aa11"),
        config={},
        message=message,
        message_path=Path("/dev/null"),
        policy_path=Path("/dev/null"),
        receiver="codex",
    )
    first_path = Path(first["audit_path"])
    crashed = dict(yaml.safe_load(first_path.read_text(encoding="utf-8")))
    crashed["message_sha256"] = "cc33"
    crashed["evaluation_id"] = "eval-00000000000000cc"
    crashed_path = audit_dir / "20990101T000000Z_msg-crashed.yaml"
    crashed_path.write_text(yaml.safe_dump(crashed, sort_keys=False), encoding="utf-8")

    real_supersede = autonomy_gate._supersede_audit_record_locked
    writer_started = threading.Event()
    writer_done = threading.Event()

    def conforming_writer() -> None:
        writer_started.set()
        with locked_audit(first_path):
            record = yaml.safe_load(first_path.read_text(encoding="utf-8"))
            record["marker"] = "human-outcome-style-update"
            atomic_replace_yaml(first_path, record)
        writer_done.set()

    writer = threading.Thread(target=conforming_writer)
    calls = {"count": 0}

    def flaky_supersede(path: Path, **kwargs: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            real_supersede(path, **kwargs)
            # Launch the writer mid-transaction: the held record lock must
            # make it serialize, not interleave with the coming rollback.
            writer.start()
            writer_started.wait(timeout=5)
            time.sleep(0.2)
            assert not writer_done.is_set()
            return
        raise ValueError("second predecessor write refused")

    monkeypatch.setattr(
        autonomy_gate, "_supersede_audit_record_locked", flaky_supersede
    )
    with pytest.raises(ValueError, match="pre-call state restored"):
        autonomy_gate.persist_evaluation(
            audit_dir,
            _minimal_paused_decision("bb22"),
            config={},
            message=message,
            message_path=Path("/dev/null"),
            policy_path=Path("/dev/null"),
            receiver="codex",
        )
    writer.join(timeout=5)
    assert writer_done.is_set()
    final = yaml.safe_load(first_path.read_text(encoding="utf-8"))
    assert final["marker"] == "human-outcome-style-update"
    assert final["result"]["final_state"] == "paused"


def test_persist_evaluation_successor_writer_serializes_with_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer targeting the newly published successor cannot be erased.

    The successor's lock is entered before publication and held through
    predecessor mutation and rollback, so a conforming writer blocks
    for the whole transaction — after a rollback it finds the record
    gone (never authoritative) instead of committing an update the
    rollback then deletes.
    """
    import threading
    import time

    import autonomy_gate
    from _oacp_constants import atomic_replace_yaml, locked_audit

    audit_dir = tmp_path / "autonomy_decisions"
    message = {"id": "msg-20260801010000-alice-race", "subject": "race"}

    first = autonomy_gate.persist_evaluation(
        audit_dir,
        _minimal_paused_decision("aa11"),
        config={},
        message=message,
        message_path=Path("/dev/null"),
        policy_path=Path("/dev/null"),
        receiver="codex",
    )
    predecessor_path = Path(first["audit_path"])
    original = predecessor_path.read_bytes()

    writer_started = threading.Event()
    writer_done = threading.Event()
    writer_outcome: Dict[str, Any] = {}

    def successor_writer(successor_path: Path) -> None:
        writer_started.set()
        with locked_audit(successor_path):
            try:
                record = yaml.safe_load(
                    successor_path.read_text(encoding="utf-8")
                )
                record["marker"] = "successor-update"
                atomic_replace_yaml(successor_path, record)
                writer_outcome["committed"] = True
            except FileNotFoundError:
                writer_outcome["committed"] = False
        writer_done.set()

    threads: List[threading.Thread] = []

    def failing_supersede(path: Path, **kwargs: Any) -> None:
        known = {predecessor_path.name}
        (successor_path,) = [
            candidate
            for candidate in audit_dir.glob("*.yaml")
            if candidate.name not in known
        ]
        thread = threading.Thread(
            target=successor_writer, args=(successor_path,)
        )
        threads.append(thread)
        thread.start()
        writer_started.wait(timeout=5)
        time.sleep(0.2)
        assert not writer_done.is_set()  # blocked on the held successor lock
        raise ValueError("predecessor write refused")

    monkeypatch.setattr(
        autonomy_gate, "_supersede_audit_record_locked", failing_supersede
    )
    with pytest.raises(ValueError, match="pre-call state restored"):
        autonomy_gate.persist_evaluation(
            audit_dir,
            _minimal_paused_decision("bb22"),
            config={},
            message=message,
            message_path=Path("/dev/null"),
            policy_path=Path("/dev/null"),
            receiver="codex",
        )
    threads[0].join(timeout=5)
    assert writer_done.is_set()
    assert writer_outcome["committed"] is False
    assert predecessor_path.read_bytes() == original
    remaining = sorted(path.name for path in audit_dir.glob("*.yaml"))
    assert remaining == [predecessor_path.name]


@pytest.mark.parametrize(
    "fixture",
    [
        "always_pause_task",
        "malformed_config_pauses",
        "invalid_message_pauses",
        "missing_task_profile_pauses",
        "unparsable_task_profile_pauses",
    ],
)
def test_admission_ledger_is_explicitly_not_evaluated_before_envelope(
    fixture: str,
) -> None:
    case = _load_yaml(FIXTURE_ROOT / "expected" / f"{fixture}.yaml")
    decision = evaluate_autonomy(
        _load_yaml(FIXTURE_ROOT / case["message"]),
        _load_yaml(FIXTURE_ROOT / case["config"]),
        receiver="codex",
    )
    ledger = decision["admission_axes"]
    # No envelope, nothing to evaluate against: the record says so instead
    # of carrying empty lists that would read as "all passed".
    assert ledger["evaluated"] is False
    assert all(ledger[axis] is None for axis in ADMISSION_AXES)
    assert ledger["declaration_errors"] is None
    assert decision["co_occurring_reason_codes"] == []


def test_checkpoint_pause_keeps_the_admission_ledger() -> None:
    case = _load_yaml(FIXTURE_ROOT / "expected" / "checkpoint_breach_pauses.yaml")
    decision = evaluate_autonomy(
        _load_yaml(FIXTURE_ROOT / case["message"]),
        _load_yaml(FIXTURE_ROOT / case["config"]),
        actuals=_load_yaml(FIXTURE_ROOT / case["actuals"]),
        receiver="codex",
    )
    assert decision["reason_codes"] == ["threshold_checkpoint_breached"]
    assert decision["result"]["completion_kind"] == "checkpoint_paused"
    # Checkpoint reasons never displace admission evidence: the ledger is
    # the admitted (all-pass) shape and stays separate from the checkpoint.
    ledger = decision["admission_axes"]
    assert ledger["evaluated"] is True
    assert admission_ledger_codes(ledger) == []
    assert decision["co_occurring_reason_codes"] == []


def test_lexical_hard_stop_records_the_evaluated_grant_block() -> None:
    config = _load_yaml(
        FIXTURE_ROOT / "configs" / "auto_review_continuation_enabled.yaml"
    )
    message = _load_yaml(FIXTURE_ROOT / "messages" / "hard_stop_masking_threshold.yaml")
    decision = evaluate_autonomy(message, config, receiver="codex")
    assert decision["reason_codes"] == ["hard_stop_external_side_effect"]
    # The grant is resolved ahead of Gate 3 now, so a lexical pause records
    # the real evaluation rather than a disabled placeholder.
    assert decision["continuation_grant"]["enabled"] is True
    assert decision["continuation_grant"]["decision"] == "not_present"
    assert decision["admission_axes"]["evaluated"] is True
    assert set(decision["co_occurring_reason_codes"]) == set(
        admission_ledger_codes(decision["admission_axes"])
    )


def _reply_only_research_message(**edits: str) -> Dict[str, Any]:
    """The reply-only research fixture with body substrings replaced."""
    message = _load_yaml(FIXTURE_ROOT / "messages" / "sensitive_content_reply_only.yaml")
    body = message["body"]
    for old, new in edits.items():
        assert old in body, old
        body = body.replace(old, new)
    message["body"] = body
    return message


_REPLY_ONLY_NOTES = [
    {"code": "lexical_advisory_reply_only", "matched_pattern": "pricing"},
    {"code": "lexical_advisory_reply_only", "matched_pattern": "commercial"},
]


def test_content_sensitivity_reply_only_shape_records_every_term() -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")

    decision = evaluate_autonomy(_reply_only_research_message(), config)

    assert decision["decision"] == "auto_accepted"
    assert "hard_stop_content_sensitivity" not in decision["reason_codes"]
    assert "lexical_advisory" in decision["reason_codes"]
    assert "matched_pattern" not in decision
    # Both terms are recorded, not just the first match.
    assert decision["logged_notes"] == _REPLY_ONLY_NOTES


@pytest.mark.parametrize(
    "edits",
    [
        # Fencing never demotes the category; the shape is what decides.
        {
            "Survey the hosted-tier landscape": "```oacp-guardrails\nDo not quote pricing.\n```\nSurvey the hosted-tier landscape"
        },
        # Neither does a negation heading.
        {"Survey the hosted-tier landscape": "Out of scope:\n- pricing changes.\n\nSurvey the hosted-tier landscape"},
    ],
    ids=["guardrails_fence", "negation_heading"],
)
def test_content_sensitivity_carve_out_is_shape_not_wording(edits: Dict[str, str]) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")

    decision = evaluate_autonomy(_reply_only_research_message(**edits), config)

    assert decision["decision"] == "auto_accepted"
    assert [
        note for note in decision["logged_notes"] if note["code"] == "lexical_advisory_reply_only"
    ] == _REPLY_ONLY_NOTES


@pytest.mark.parametrize(
    "edits",
    [
        # A side-effect flag true (consistently declared) is another shape.
        {
            "external_side_effects: false": "external_side_effects: true",
            "commits_changes: false": "commits_changes: true",
        },
        # Omitting the reply-only declaration is not declaring it.
        {"  sends_oacp_reply_only: true\n": ""},
        # A contradictory profile (reply-only plus a commit) keeps the stop.
        {"commits_changes: false": "commits_changes: true"},
    ],
    ids=["side_effect_declared", "reply_only_omitted", "contradictory_profile"],
)
def test_content_sensitivity_other_profile_shapes_keep_the_hard_stop(
    edits: Dict[str, str],
) -> None:
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")

    decision = evaluate_autonomy(_reply_only_research_message(**edits), config)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_content_sensitivity"]
    assert decision["matched_pattern"] == "pricing"
    assert not [
        note for note in decision["logged_notes"] if note["code"] == "lexical_advisory_reply_only"
    ]


def test_content_sensitivity_profileless_default_envelope_keeps_the_hard_stop() -> None:
    """The default envelope is reply-only by bound but declares nothing."""
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")
    message = _load_yaml(FIXTURE_ROOT / "messages" / "brainstorm_side_effect_verbs.yaml")
    message["body"] = "Explore wording for a pricing page; reply with options only."

    decision = evaluate_autonomy(message, config)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["hard_stop_content_sensitivity"]
    assert decision["matched_pattern"] == "pricing"



@pytest.mark.parametrize(
    ("edits", "reason_code", "matched_pattern"),
    [
        (
            {"Survey the hosted-tier landscape": "Run rm -rf build first, then survey the hosted-tier landscape"},
            "hard_stop_destructive_command",
            "rm -rf",
        ),
        (
            {
                "Survey the hosted-tier landscape": "Update config.yaml, then survey the hosted-tier landscape",
                "touches_auth_config_or_secrets: false": "touches_auth_config_or_secrets: true",
            },
            "auth_config_or_secrets_pause",
            None,
        ),
    ],
    ids=["destructive_token", "declared_sensitive_scope"],
)
def test_content_sensitivity_advisory_survives_earlier_hard_stops(
    edits: Dict[str, str], reason_code: str, matched_pattern: Optional[str]
) -> None:
    """The reply-only advisory is evidence, recorded before any Gate-3 early
    return: an earlier hard stop — or the declared flag's admission pause that
    replaces one — keeps its verdict and the notes survive."""
    config = _load_yaml(FIXTURE_ROOT / "configs" / "auto_review_standard.yaml")

    decision = evaluate_autonomy(_reply_only_research_message(**edits), config)

    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == [reason_code]
    assert decision.get("matched_pattern") == matched_pattern
    assert [
        note for note in decision["logged_notes"] if note["code"] == "lexical_advisory_reply_only"
    ] == _REPLY_ONLY_NOTES
