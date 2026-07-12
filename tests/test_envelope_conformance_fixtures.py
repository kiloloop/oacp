# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Executable runner for the envelope compilation conformance fixtures."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from envelope_compiler import (  # noqa: E402
    ENVELOPE_COMPILE_ERROR,
    EnvelopeCompileError,
    build_envelope,
)


FIXTURE_ROOT = Path(__file__).parent / "conformance" / "envelope"
COMPILED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _assert_subset(expected: Any, actual: Any, context: str) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), context
        for key, value in expected.items():
            assert key in actual, f"{context}: missing {key}"
            _assert_subset(value, actual[key], f"{context}.{key}")
        return
    assert actual == expected, f"{context}: {actual!r} != {expected!r}"


def _expected_fixtures():
    files = sorted((FIXTURE_ROOT / "expected").glob("*.yaml"))
    assert files, "envelope conformance fixtures must not be empty"
    return files


@pytest.mark.parametrize(
    "expected_path", _expected_fixtures(), ids=lambda path: path.stem
)
def test_envelope_compiler_matches_conformance_fixtures(expected_path: Path) -> None:
    fixture = _load_yaml(expected_path)
    config = _load_yaml(FIXTURE_ROOT / fixture["config"])
    message = _load_yaml(FIXTURE_ROOT / fixture["message"])
    expected = fixture["expected"]

    if not expected["compiles"]:
        assert expected["error"] == ENVELOPE_COMPILE_ERROR
        with pytest.raises(EnvelopeCompileError):
            build_envelope(
                message, config, receiver="claude", project="test-proj"
            )
        return

    envelope = build_envelope(
        message, config, receiver="claude", project="test-proj"
    )
    _assert_subset(expected["envelope"], envelope, expected_path.stem)

    # Volatile fields are unpinned but must be present and well-formed.
    assert COMPILED_AT_RE.match(envelope["compiled_at_utc"])
    assert re.fullmatch(r"[0-9a-f]{64}", envelope["message_sha256"])
    assert envelope["spec_version"]
    assert envelope["compiler"]


def test_expected_cases_reference_existing_config_and_message() -> None:
    for expected_path in _expected_fixtures():
        fixture = _load_yaml(expected_path)
        assert (FIXTURE_ROOT / fixture["config"]).is_file(), expected_path.name
        assert (FIXTURE_ROOT / fixture["message"]).is_file(), expected_path.name
        expected = fixture["expected"]
        assert isinstance(expected.get("compiles"), bool), expected_path.name
        if expected["compiles"]:
            assert "envelope" in expected, expected_path.name
        else:
            assert expected.get("error") == ENVELOPE_COMPILE_ERROR, expected_path.name


def test_fixture_coverage_spans_success_and_failure() -> None:
    outcomes = {
        _load_yaml(path)["expected"]["compiles"] for path in _expected_fixtures()
    }
    assert outcomes == {True, False}
