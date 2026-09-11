# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Executable runner for the envelope compilation conformance fixtures."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from envelope_compiler import (  # noqa: E402
    ADAPTER_STATES,
    ENVELOPE_COMPILE_ERROR,
    AdapterDetection,
    AdapterDetectionError,
    EnvelopeCompileError,
    build_envelope,
    detect_adapter,
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


def _adapter_from_fixture(fixture: Dict[str, Any]) -> AdapterDetection:
    """Run adapter detection on the inputs a fixture declares.

    The ``adapter`` block names the receiver's card runtime and, for an
    adapter-capable runtime, whether the console script is on PATH and
    registered as a hook. A fixture without the block compiles against a
    resolved ``claude`` adapter, the contract those fixtures pin.
    """
    spec = fixture["adapter"] if "adapter" in fixture else {"runtime": "claude"}
    spec = spec or {}
    console_on_path = bool(spec.get("console_on_path", True))
    registered = bool(spec.get("registered", True))
    probe_error = spec.get("probe_error")

    def which_fn(name: str) -> Optional[str]:
        return f"/opt/oacp/bin/{name}" if console_on_path else None

    def registration_fn(console: str) -> bool:
        if probe_error:
            raise AdapterDetectionError(str(probe_error))
        return registered

    return detect_adapter(
        spec.get("runtime"),
        which_fn=which_fn,
        registration_fn=registration_fn,
        runtime_error=spec.get("runtime_error"),
    )


@pytest.mark.parametrize(
    "expected_path", _expected_fixtures(), ids=lambda path: path.stem
)
def test_envelope_compiler_matches_conformance_fixtures(expected_path: Path) -> None:
    fixture = _load_yaml(expected_path)
    config = _load_yaml(FIXTURE_ROOT / fixture["config"])
    message = _load_yaml(FIXTURE_ROOT / fixture["message"])
    receiver = str(fixture.get("receiver") or "claude")
    expected = fixture["expected"]

    if not expected["compiles"]:
        assert expected["error"] == ENVELOPE_COMPILE_ERROR
        with pytest.raises(EnvelopeCompileError):
            build_envelope(
                message, config, receiver=receiver, project="test-proj"
            )
        return

    envelope = build_envelope(
        message,
        config,
        receiver=receiver,
        project="test-proj",
        adapter=_adapter_from_fixture(fixture),
    )
    _assert_subset(expected["envelope"], envelope, expected_path.stem)

    # Enforcement is earned: a reason is present exactly when it is none.
    assert ("enforcement_reason" in envelope) == (envelope["enforcement"] == "none")

    # Volatile fields are unpinned but must be present and well-formed.
    assert COMPILED_AT_RE.match(envelope["compiled_at_utc"])
    assert re.fullmatch(r"[0-9a-f]{64}", envelope["message_sha256"])
    assert envelope["spec_version"]
    assert envelope["compiler"]
    assert envelope["adapter"]["detail"]


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


def test_fixture_coverage_spans_adapter_states() -> None:
    """Every state of the enforcement grammar is pinned by a fixture."""
    states = set()
    for path in _expected_fixtures():
        fixture = _load_yaml(path)
        if fixture["expected"]["compiles"]:
            states.add(_adapter_from_fixture(fixture).state)
    assert states == set(ADAPTER_STATES)
