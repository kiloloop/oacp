# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Executable tests for scripts/autonomy_gate.py."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from autonomy_gate import evaluate_autonomy  # noqa: E402


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


def test_autonomy_gate_matches_conformance_fixtures() -> None:
    expected_files = sorted((FIXTURE_ROOT / "expected").glob("*.yaml"))
    assert expected_files

    for expected_path in expected_files:
        fixture = _load_yaml(expected_path)
        config = _load_yaml(FIXTURE_ROOT / fixture["config"])
        message = _load_yaml(FIXTURE_ROOT / fixture["message"])
        actuals = None
        if fixture.get("actuals"):
            actuals = _load_yaml(FIXTURE_ROOT / fixture["actuals"])

        decision = evaluate_autonomy(message, config, actuals=actuals)
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


def test_autonomy_gate_output_uses_canonical_final_states() -> None:
    allowed = {"done", "paused", "blocked", "superseded", "error"}
    for expected_path in sorted((FIXTURE_ROOT / "expected").glob("*.yaml")):
        fixture = _load_yaml(expected_path)
        config = _load_yaml(FIXTURE_ROOT / fixture["config"])
        message = _load_yaml(FIXTURE_ROOT / fixture["message"])
        actuals = _load_yaml(FIXTURE_ROOT / fixture["actuals"]) if fixture.get("actuals") else None

        decision = evaluate_autonomy(message, config, actuals=actuals)
        assert decision["result"]["final_state"] in allowed


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
