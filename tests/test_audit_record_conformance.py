# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Executable runner for the audit-record integrity fixtures."""

from __future__ import annotations

import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from finalize_autonomy_record import (  # noqa: E402
    FINDING_SEVERITIES,
    sweep_audit_dir,
)


RECORDS_ROOT = Path(__file__).parent / "conformance" / "autonomy" / "records"


def _expected() -> dict:
    data = yaml.safe_load(
        (RECORDS_ROOT / "expected_findings.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(data, dict) and data
    return data


def test_every_record_fixture_is_pinned() -> None:
    expected = _expected()
    fixture_names = {
        path.name
        for path in RECORDS_ROOT.glob("*.yaml")
        if path.name != "expected_findings.yaml"
    }
    assert fixture_names == set(expected)


def test_sweep_matches_pinned_finding_codes(tmp_path: Path) -> None:
    expected = _expected()
    audit_dir = tmp_path / "autonomy_decisions"
    audit_dir.mkdir()
    for name in expected:
        shutil.copy(RECORDS_ROOT / name, audit_dir / name)

    report = sweep_audit_dir(audit_dir)

    assert set(report["records"]) == set(expected)
    for name, findings in sorted(report["records"].items()):
        codes = Counter(finding["code"] for finding in findings)
        assert codes == Counter(expected[name]), name
        for finding in findings:
            assert finding["severity"] == FINDING_SEVERITIES[finding["code"]]

    (duplicate_group,) = report["duplicate_groups"]
    assert duplicate_group["files"] == [
        "duplicate_live_a.yaml",
        "duplicate_live_b.yaml",
    ]


def test_all_finding_codes_have_fixture_coverage() -> None:
    """Every pinned error-severity code appears in at least one fixture.

    ``record_unparsable``, ``missing_completion_kind``,
    ``terminal_paused_without_outcome``, ``decision_kind_incoherent``,
    ``invalid_human_outcome``, ``off_enum_breach_basis``, and the advisory
    ``terminal_missing_actuals``
    are covered by unit tests instead — their shapes are synthetic, not
    corpus-observed.
    """
    expected = _expected()
    pinned_here = {code for codes in expected.values() for code in codes}
    corpus_codes = {
        "off_enum_completion_kind",
        "off_enum_final_state",
        "duplicate_logical_id",
        "duplicate_yaml_key",
        "paused_terminal_checkpoint_action",
        "paused_terminal_completed",
        "breached_empty_fields",
        "noncanonical_checkpoint_axis",
        "breach_basis_incoherent",
    }
    assert corpus_codes <= pinned_here
