# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Structural tests for autonomy conformance fixtures."""

from __future__ import annotations

from pathlib import Path

import yaml


FIXTURE_ROOT = Path(__file__).parent / "conformance" / "autonomy"


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_expected_cases_reference_existing_config_and_message() -> None:
    expected_files = sorted((FIXTURE_ROOT / "expected").glob("*.yaml"))
    assert expected_files, "expected autonomy decisions must not be empty"

    for expected_path in expected_files:
        data = _load_yaml(expected_path)
        config = FIXTURE_ROOT / data["config"]
        message = FIXTURE_ROOT / data["message"]

        assert config.is_file(), f"{expected_path.name} references missing {config}"
        assert message.is_file(), f"{expected_path.name} references missing {message}"
        if data.get("actuals"):
            actuals = FIXTURE_ROOT / data["actuals"]
            assert actuals.is_file(), f"{expected_path.name} references missing {actuals}"
        assert data["expected"]["decision"] in {"auto_accepted", "paused", "rejected"}
        assert data["expected"]["mode"] in {"always_pause", "auto_review"}
        assert isinstance(data["expected"]["reason_codes"], list)
        assert data["expected"]["reason_codes"]
        result = data["expected"].get("result")
        if result:
            assert result.get("final_state") in {"done", "paused", "blocked", "superseded", "error"}


def test_no_sender_trust_fixture_surface() -> None:
    forbidden = "trusted_senders"
    for yaml_path in sorted(FIXTURE_ROOT.rglob("*.yaml")):
        text = yaml_path.read_text(encoding="utf-8")
        assert forbidden not in text, f"{yaml_path} must not reintroduce sender trust"


def test_matched_pattern_fixture_coverage() -> None:
    expected_patterns = {
        "rm -rf",
        "--force",
        "--no-verify",
        "--dangerously-skip-permissions",
        "deploy",
        "auth",
        "all files",
    }
    found = set()
    for expected_path in (FIXTURE_ROOT / "expected").glob("*.yaml"):
        data = _load_yaml(expected_path)
        pattern = data["expected"].get("matched_pattern")
        if pattern:
            found.add(pattern)

    assert expected_patterns <= found
