# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Expected-pause annotations classify evidence without granting authority."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from autonomy_gate import _classify_pause, evaluate_autonomy, extract_task_profile  # noqa: E402


FIXTURES = Path(__file__).parent / "conformance" / "autonomy"


def _load(path):
    return yaml.safe_load(path.read_text())


def _message(annotation="", *, minutes=15, files=2, prose="Inspect the parser."):
    return {
        "id": "msg-20260905090000-alice-pauses",
        "from": "alice",
        "to": "codex",
        "type": "task_request",
        "priority": "P2",
        "created_at_utc": "2026-09-05T09:00:00Z",
        "subject": "Inspect parser behavior",
        "body": (
            f"{prose}\n\ntask_profile:\n"
            f"  estimated_minutes: {minutes}\n"
            "  risk_tier: P2\n"
            f"  expected_files_touched: {files}\n"
            "  destructive_ops: false\n"
            "  external_side_effects: false\n"
            "  touches_auth_config_or_secrets: false\n"
            "  touches_dependencies: false\n"
            "  public_visibility: false\n"
            f"{annotation}"
        ),
    }


def _evaluate(message, config=None):
    if config is None:
        config = _load(FIXTURES / "configs" / "auto_review_standard.yaml")
    return evaluate_autonomy(message, config)


@pytest.mark.parametrize(
    "classification",
    ["designed", "unplanned", "mixed", "undeclared", "declared_capability"],
)
def test_classification_conformance(classification):
    case = _load(FIXTURES / "expected" / f"pause_classification_{classification}.yaml")
    result = _evaluate(_load(FIXTURES / case["message"]), _load(FIXTURES / case["config"]))
    for key, expected in case["expected"].items():
        assert result[key] == expected


def test_declared_capability_demotion_is_a_designed_pause():
    """A demoted head's granular pause is the pause the sender can declare;
    the same wording without the declaration is a hard stop no annotation
    can name away."""
    annotation = "  expected_pauses: [dependency_changes_pause]\n"
    prose = "Install the dependency the parser needs."
    declared = _message(annotation, prose=prose)
    declared["body"] = declared["body"].replace(
        "touches_dependencies: false", "touches_dependencies: true"
    )
    result = _evaluate(declared)
    assert result["reason_codes"] == ["dependency_changes_pause"]
    assert result["pause_classification"] == "designed"
    assert result["unplanned_pause_codes"] == []
    assert result["logged_notes"] == [
        {"code": "lexical_advisory_declared", "matched_pattern": "install dependency"}
    ]

    undeclared = _evaluate(_message(annotation, prose=prose))
    assert undeclared["reason_codes"] == ["hard_stop_external_side_effect"]
    assert undeclared["pause_classification"] == "unplanned"
    assert undeclared["unplanned_pause_codes"] == ["hard_stop_external_side_effect"]
    assert undeclared["logged_notes"] == []


def test_only_fired_reasons_classify_and_approval_is_still_required():
    message = _message(
        "  expected_pauses: [hard_stop_destructive_command, message_valid]\n",
        minutes=100,
        prose="Run rm -rf scratch.",
    )
    result = _evaluate(message)
    assert result["decision"] == "paused"
    assert result["reason_codes"] == ["hard_stop_destructive_command"]
    assert "estimated_minutes_exceeds_threshold" in result["co_occurring_reason_codes"]
    assert result["pause_classification"] == "designed"
    assert result["unplanned_pause_codes"] == []
    assert result["result"]["human_outcome"]["recorded"] is False


@pytest.mark.parametrize(
    "annotation",
    [
        '  expected_pauses: ["future_pause_code", another_code, unknown_code]\n',
        '  expected_pauses: "future_pause_code"\n',
        '  expected_pauses: {unknown: "future_pause_code"}\n',
        '  expected_pauses:\n    - "future_pause_code"\n    - 42\n',
        '  expected_pauses: |\n    future_pause_code\n',
        '  expected_pauses: &annotation ["future_pause_code"]\n',
        '  expected_pauses: &annotation [*annotation]\n',
    ],
)
def test_invalid_advisory_values_warn_without_changing_admission(annotation):
    baseline = _evaluate(_message())
    result = _evaluate(_message(annotation))
    assert result["decision"] == baseline["decision"] == "auto_accepted"
    assert result["reason_codes"] == baseline["reason_codes"]
    assert result["scope_envelope"] == baseline["scope_envelope"]
    assert "pause_classification" not in result
    assert result["matched_patterns"] == baseline["matched_patterns"] == []
    assert all(note["code"] == "expected_pauses_declaration_warning" for note in result["logged_notes"])
    assert result["logged_notes"]


def test_unknown_codes_are_preserved_but_do_not_match():
    result = _evaluate(_message(
        "  expected_pauses: [unknown_code, message_valid, unknown_code, 42]\n",
        minutes=100,
    ))
    assert result["pause_classification"] == "unplanned"
    assert result["expected_pause_codes"] == ["unknown_code", "message_valid", "unknown_code"]
    assert result["unplanned_pause_codes"] == ["estimated_minutes_exceeds_threshold"]
    assert len(result["logged_notes"]) == 4


def test_evidence_in_reason_codes_is_not_a_fired_pause():
    fired = "estimated_minutes_exceeds_threshold"
    evidence = ["message_valid", "schema_valid", "task_type_allowed", "workspace_check_required"]
    designed = _classify_pause([*evidence, fired], True, [fired])
    assert designed["pause_classification"] == "designed"
    assert designed["unplanned_pause_codes"] == []
    unplanned = _classify_pause([*evidence, fired], True, evidence)
    assert unplanned["pause_classification"] == "unplanned"
    assert unplanned["unplanned_pause_codes"] == [fired]


@pytest.mark.parametrize(
    "annotation",
    [
        '  note: &danger "rm -rf scratch"\n  expected_pauses: *danger\n',
        '  note: &danger ["rm -rf scratch"]\n  expected_pauses: *danger\n',
        '  note: &danger "rm -rf scratch"\n  expected_pauses: [*danger]\n',
        '  expected_pauses: [unknown]\n  # rm -rf scratch\n  note: safe\n',
        '  expected_pauses:\n    - unknown\n  # rm -rf scratch\n  note: safe\n',
    ],
)
def test_annotation_never_hides_outside_source(annotation):
    message = _message(annotation)
    result = _evaluate(message)
    assert result["decision"] == "paused"
    assert result["reason_codes"] == ["hard_stop_destructive_command"]
    hits = [hit for hit in result["matched_patterns"] if hit["category"] == "destructive_command"]
    assert hits
    for hit in hits:
        span = hit["span"]
        assert "rm -rf" in message["body"][span["start"]:span["end"]]


def test_flow_profile_keeps_all_hard_stops_and_offsets():
    message = _message()
    profile, error = extract_task_profile(message["body"])
    assert error is None
    profile["expected_pauses"] = ["rm -rf hidden"]
    profile["note"] = "rm -rf visible"
    message["body"] = "Résumé.\ntask_profile: " + yaml.safe_dump(profile, default_flow_style=True)
    result = _evaluate(message)
    assert result["reason_codes"] == ["hard_stop_destructive_command"]
    hits = [hit for hit in result["matched_patterns"] if hit["category"] == "destructive_command"]
    assert len(hits) == 2
    assert {hit["span"]["start"] for hit in hits} == {
        message["body"].index("rm -rf hidden"),
        message["body"].index("rm -rf visible"),
    }


def test_selected_profile_does_not_hide_another_declaration():
    message = _message('  expected_pauses: [unknown]\n')
    message["body"] += '\n```yaml\ntask_profile:\n  expected_pauses: ["rm -rf visible"]\n```\n'
    assert _evaluate(message)["reason_codes"] == ["hard_stop_destructive_command"]


def test_fenced_indented_profile_keeps_crlf_source_offsets():
    message = _message('  expected_pauses: ["rm -rf hidden"]\n')
    profile_body = message["body"].split("\n\n", 1)[1]
    message["body"] = (
        "Résumé.\r\n```yaml\r\n"
        + "\r\n".join("  " + line for line in profile_body.splitlines())
        + "\r\n```\r\nRun rm -rf visible.\r\n"
    )
    result = _evaluate(message)
    assert result["reason_codes"] == ["hard_stop_destructive_command"]
    hits = [hit for hit in result["matched_patterns"] if hit["category"] == "destructive_command"]
    assert len(hits) == 2
    assert {hit["span"]["start"] for hit in hits} == {
        message["body"].index("rm -rf hidden"),
        message["body"].index("rm -rf visible"),
    }


def test_malformed_profile_syntax_keeps_its_original_pause():
    result = _evaluate(_message("  expected_pauses: [\n"))
    assert result["reason_codes"] == ["task_profile_unparsable"]
    assert result["pause_classification"] == "undeclared"


@pytest.mark.parametrize("kind", ["config", "policy", "mode", "message", "profile", "review"])
def test_early_pause_paths_are_classified(kind):
    message = _message()
    config = _load(FIXTURES / "configs" / "auto_review_standard.yaml")
    kwargs = {}
    if kind == "config":
        config = {"autonomy": []}
    elif kind == "policy":
        kwargs["policy_auth"] = {"status": "invalid"}
    elif kind == "mode":
        config["autonomy"]["default_mode"] = "always_pause"
    elif kind == "message":
        message.pop("id")
    elif kind == "profile":
        message["body"] = "Inspect the parser."
    elif kind == "review":
        message["type"] = "review_request"
    result = evaluate_autonomy(message, config, **kwargs)
    assert result["decision"] == "paused"
    assert result["pause_classification"] == "undeclared"
    assert result["expected_pause_codes"] == []
    assert result["unplanned_pause_codes"] == result["reason_codes"]


def test_annotation_is_available_even_before_profile_admission():
    result = _evaluate(_message("  expected_pauses: [config_malformed]\n"), {"autonomy": []})
    assert result["pause_classification"] == "designed"
    assert result["scope_envelope"] is None


def test_existing_message_is_not_mutated():
    message = _message('  expected_pauses: ["rm -rf scratch"]\n', minutes=100)
    original = copy.deepcopy(message)
    result = _evaluate(message)
    assert message == original
    assert result["task_profile"]["expected_pauses"] == ["rm -rf scratch"]


def test_checkpoint_classification_keeps_admission_ledger_separate():
    case = _load(FIXTURES / "expected" / "checkpoint_breach_pauses.yaml")
    result = evaluate_autonomy(
        _load(FIXTURES / case["message"]),
        _load(FIXTURES / case["config"]),
        actuals=_load(FIXTURES / case["actuals"]),
    )
    assert result["result"]["completion_kind"] == "checkpoint_paused"
    assert result["pause_classification"] == "undeclared"
    assert result["unplanned_pause_codes"] == ["threshold_checkpoint_breached"]


@pytest.mark.parametrize("annotation", [
    '  expected_pauses: ["then run rm -rf scratch", "push to main"]\n',
    '  expected_pauses: "rm -rf scratch"\n',
    '  expected_pauses: {unknown: "rm -rf scratch"}\n',
    '  expected_pauses:\n    - "rm -rf scratch"\n    - 42\n',
    '  expected_pauses: |\n    rm -rf scratch\n',
    '  expected_pauses: &annotation ["rm -rf scratch"]\n',
])
def test_expected_pauses_cannot_suppress_existing_safety_checks(annotation):
    message = _message(annotation)
    result = _evaluate(message)
    assert result["decision"] == "paused"
    assert result["reason_codes"] == ["hard_stop_destructive_command"]
    assert result["pause_classification"] == "unplanned"
    assert result["unplanned_pause_codes"] == ["hard_stop_destructive_command"]
    assert any(note["code"] == "expected_pauses_declaration_warning" for note in result["logged_notes"])
    assert any(hit["category"] == "destructive_command" for hit in result["matched_patterns"])
