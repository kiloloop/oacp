# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Executable admission-ledger replay: every envelope-derived axis is recorded.

Each case in ``tests/conformance/autonomy/ledger_replay/corpus.yaml`` is an
anonymized reproduction of a fleet audit record whose evaluator early-out
left admission axes unrecorded. The runner rebuilds the message and the
receiver config, evaluates it, and pins three things at once: the verdict
is unchanged (``reason_codes`` and ``matched_pattern``), the ledger holds
exactly the expected axes, and every non-primary axis surfaces through
``co_occurring_reason_codes`` — zero missing axes across the corpus.

The corpus's ``content_sensitivity`` section replays the reply-only
carve-out over every content-sensitivity hard stop of one window plus a
control: the runner pins which records now record the matched term as a
``lexical_advisory_reply_only`` note (verdict taken by the axes the hard
stop used to mask) and which keep the hard stop unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from autonomy_gate import (  # noqa: E402
    admission_ledger_codes,
    evaluate_autonomy,
)

CORPUS_PATH = (
    Path(__file__).parent / "conformance" / "autonomy" / "ledger_replay" / "corpus.yaml"
)


def _corpus() -> Dict[str, Any]:
    data = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data["cases"]
    return data


def _config(policy: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "autonomy": {
            "default_mode": "auto_review",
            "auto_review_thresholds": {
                "max_estimated_minutes": policy["max_estimated_minutes"],
                "max_expected_files_touched": policy["max_expected_files_touched"],
                "destructive_ops": "pause",
                "external_side_effects": policy["external_side_effects"],
                "auth_config_or_secrets": "pause",
                "dependency_changes": "pause",
                "public_visibility": "pause",
                "git_push_or_deploy": "pause",
            },
            "allow_without_task_profile": ["brainstorm_request"],
            "private_repo_allowlist": list(policy["private_repo_allowlist"]),
            "continuation_grants": {"enabled": False},
        }
    }


def _message(case: Dict[str, Any], index: int) -> Dict[str, Any]:
    profile = yaml.safe_dump(case["task_profile"], sort_keys=False).rstrip("\n")
    block = "\n".join(f"  {line}" for line in profile.splitlines())
    body = f"## Task\n{case['body_line']}\n\ntask_profile:\n{block}\n"
    return {
        "id": f"msg-20260818000000-alice-{index:04d}",
        "from": "alice",
        "to": "codex",
        "type": case.get("type", "task_request"),
        "priority": "P2",
        "created_at_utc": "2026-08-18T00:00:00Z",
        "subject": f"Ledger replay {case['case']}",
        "body": body,
    }


def _evaluate(data: Dict[str, Any], case: Dict[str, Any], index: int) -> Dict[str, Any]:
    return evaluate_autonomy(
        _message(case, index), _config(data["policy"]), receiver="codex"
    )


_CORPUS = _corpus()
_CASES = list(enumerate(_CORPUS["cases"], start=1))


@pytest.mark.parametrize(
    "index, case", _CASES, ids=[case["case"] for _index, case in _CASES]
)
def test_ledger_replay_case_records_every_axis(index: int, case: Dict[str, Any]) -> None:
    decision = _evaluate(_CORPUS, case, index)

    # Verdict identity: the ledger never changes what pauses or why.
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == case["expected_reason_codes"]
    if "expected_matched_pattern" in case:
        assert decision.get("matched_pattern") == case["expected_matched_pattern"]

    # Ledger completeness: exactly the axes the envelope implies, no more.
    ledger = decision["admission_axes"]
    assert ledger["evaluated"] is True
    assert set(admission_ledger_codes(ledger)) == set(case["expected_axes"])

    # Surface: every non-primary axis is a co-occurring reason code.
    expected_co = set(case["expected_axes"]) - set(decision["reason_codes"])
    assert set(decision["co_occurring_reason_codes"]) == expected_co
    assert decision["co_occurring_reason_codes"] == sorted(expected_co)


def test_ledger_replay_corpus_has_zero_missing_axes() -> None:
    """Corpus-level replay: the unrecorded-axis count across all cases is 0."""
    missing: List[str] = []
    for index, case in _CASES:
        decision = _evaluate(_CORPUS, case, index)
        recorded = set(decision["reason_codes"])
        recorded |= set(decision.get("co_occurring_reason_codes") or [])
        recorded |= set(admission_ledger_codes(decision.get("admission_axes") or {}))
        missing.extend(
            f"{case['case']}: {axis}"
            for axis in case["expected_axes"]
            if axis not in recorded
        )
    assert not missing, f"{len(missing)} missing admission axes:\n" + "\n".join(missing)


def test_ledger_replay_corpus_documents_the_field_gap() -> None:
    """Every case names at least one axis its field record failed to carry.

    The corpus reproduces 44 unrecorded qualitative axes plus one numeric
    threshold masked by a receiver-side checkpoint overwrite.
    """
    total = 0
    for _index, case in _CASES:
        assert case["field_record_missing"], case["case"]
        assert set(case["field_record_missing"]) <= set(case["expected_axes"]), case["case"]
        total += len(case["field_record_missing"])
    assert total == 45


_CAT5_CASES = list(
    enumerate(_CORPUS["content_sensitivity"]["cases"], start=len(_CASES) + 1)
)


@pytest.mark.parametrize(
    "index, case", _CAT5_CASES, ids=[case["case"] for _index, case in _CAT5_CASES]
)
def test_content_sensitivity_replay_case(index: int, case: Dict[str, Any]) -> None:
    decision = _evaluate(_CORPUS, case, index)

    # Verdict: the carve-out moves the reason, never the pause itself, on
    # this corpus — every record still has an axis that holds.
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == case["expected_reason_codes"]
    if "expected_matched_pattern" in case:
        assert decision.get("matched_pattern") == case["expected_matched_pattern"]
    else:
        assert "matched_pattern" not in decision

    # The category records, never silences: an advisory carries its basis
    # (the note code) and the matched term; a hard stop records none.
    advisories = [
        note
        for note in decision["logged_notes"]
        if note["code"] == "lexical_advisory_reply_only"
    ]
    assert advisories == case.get("expected_notes", [])

    # Ledger completeness is unchanged by the carve-out.
    ledger = decision["admission_axes"]
    assert ledger["evaluated"] is True
    assert set(admission_ledger_codes(ledger)) == set(case["expected_axes"])
    expected_co = set(case["expected_axes"]) - set(decision["reason_codes"])
    assert set(decision.get("co_occurring_reason_codes") or []) == expected_co


def test_content_sensitivity_replay_summary() -> None:
    """Corpus-level replay: exactly the declared reply-only records demote.

    Three of the six recorded hard stops declared the reply-only shape and
    replay as advisories; the three that omitted ``sends_oacp_reply_only``
    and the commit-declaring control keep the recorded hard stop.
    """
    advisory: List[str] = []
    unchanged: List[str] = []
    for index, case in _CAT5_CASES:
        decision = _evaluate(_CORPUS, case, index)
        assert case["recorded_reason_codes"] == ["hard_stop_content_sensitivity"]
        if case.get("expected_notes"):
            assert "hard_stop_content_sensitivity" not in decision["reason_codes"]
            advisory.append(case["case"])
        else:
            assert decision["reason_codes"] == case["recorded_reason_codes"]
            unchanged.append(case["case"])
    assert advisory == ["cat5-01", "cat5-02", "cat5-03"]
    assert unchanged == ["cat5-04", "cat5-05", "cat5-06", "cat5-control"]


def test_replayed_lexical_hits_have_complete_structured_provenance() -> None:
    """Every lexical hit carries an exact span and an explicit disposition."""
    total_hits = 0
    cases_with_hits = 0
    for index, case in _CASES + _CAT5_CASES:
        decision = _evaluate(_CORPUS, case, index)
        body = _message(case, index)["body"]
        hits = decision["matched_patterns"]
        cases_with_hits += bool(hits)
        total_hits += len(hits)
        for hit in hits:
            assert set(hit) == {
                "pattern",
                "category",
                "span",
                "demotion_basis",
            }
            assert set(hit["span"]) == {"start", "end"}
            start, end = hit["span"]["start"], hit["span"]["end"]
            assert 0 <= start < end <= len(body)
            assert hit["demotion_basis"]

    assert cases_with_hits == 18
    assert total_hits == 18
