# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Executable tests for scripts/autonomy_gate.py."""

from __future__ import annotations

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
