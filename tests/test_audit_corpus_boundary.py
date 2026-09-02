# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Conformance test: the live audit corpus is the direct children of
``audit/autonomy_decisions/``; anything under a subdirectory is history.

Every corpus reader lists ``audit_dir.glob("*.yaml")`` — the finalizer
sweep and sibling lookup, both doctor passes, the gate's replay lookups,
and the envelope hook's overlay lookup. Nothing else pins that boundary,
and the archive convention (``audit/autonomy_decisions/archive/<sweep>/``)
depends on it: an archived record must never be re-validated, re-counted,
replayed, or consulted as authority. Each case plants a record that WOULD
match if the reader walked recursively, then proves the same record counts
as a direct child, so a refactor to ``rglob``/``os.walk`` fails here rather
than silently widening the corpus.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import autonomy_gate  # noqa: E402
import claude_envelope_hook as hook  # noqa: E402
import finalize_autonomy_record  # noqa: E402
import oacp_doctor  # noqa: E402

RECORDS_ROOT = Path(__file__).parent / "conformance" / "autonomy" / "records"
CLEAN_RECORD = "clean_terminal_done.yaml"
OFF_ENUM_RECORD = "off_enum_kind_executed.yaml"
RECEIVER = "claude"
# Both real history layouts: the archive convention the legacy sweep writes
# and an arbitrary nested directory.
HISTORY_DIRS = (
    Path("archive") / "legacy-pre-finalizer-20260831",
    Path("nested"),
)


def _load(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _dump(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")


def _plant_history(audit_dir: Path, name: str, record: Dict[str, Any]) -> None:
    """Write *record* under every history directory, never as a direct child."""
    for rel in HISTORY_DIRS:
        _dump(audit_dir / rel / name, record)


@pytest.fixture
def corpus(tmp_path: Path) -> Dict[str, Any]:
    project_dir = tmp_path / "projects" / "proj"
    audit_dir = project_dir / "agents" / RECEIVER / "audit" / "autonomy_decisions"
    audit_dir.mkdir(parents=True)
    shutil.copy(RECORDS_ROOT / CLEAN_RECORD, audit_dir / "top.yaml")
    # History carries an off-enum record whose policy_path is orphaned: a
    # recursive sweep reports a finding, a recursive doctor pass reports an
    # orphaned policy ref and counts two records.
    bad = _load(RECORDS_ROOT / OFF_ENUM_RECORD)
    bad["policy_path"] = "agents/claude/config.orphaned.yaml"
    _plant_history(audit_dir, "bad.yaml", bad)
    return {
        "oacp_root": tmp_path,
        "project_dir": project_dir,
        "audit_dir": audit_dir,
        "top": _load(audit_dir / "top.yaml"),
    }


def _promote(corpus: Dict[str, Any], name: str) -> None:
    """Copy a planted history record to a direct child of the same name."""
    audit_dir = corpus["audit_dir"]
    shutil.copy(audit_dir / HISTORY_DIRS[0] / name, audit_dir / name)


def test_history_dirs_are_populated(corpus: Dict[str, Any]) -> None:
    # Guard against the fixture itself going stale: every history copy exists.
    for rel in HISTORY_DIRS:
        assert (corpus["audit_dir"] / rel / "bad.yaml").is_file()


class TestFinalizerSweep:
    def test_sweep_reads_direct_children_only(self, corpus: Dict[str, Any]) -> None:
        report = finalize_autonomy_record.sweep_audit_dir(corpus["audit_dir"])
        assert set(report["records"]) == {"top.yaml"}
        assert report["records"]["top.yaml"] == []
        assert report["duplicate_groups"] == []

    def test_history_record_is_flagged_as_a_direct_child(
        self, corpus: Dict[str, Any]
    ) -> None:
        # Control: the planted record is not silently clean, so the boundary
        # test above cannot pass vacuously.
        _promote(corpus, "bad.yaml")
        report = finalize_autonomy_record.sweep_audit_dir(corpus["audit_dir"])
        assert set(report["records"]) == {"top.yaml", "bad.yaml"}
        assert report["records"]["bad.yaml"]

    def test_cli_sweep_probe(self, corpus: Dict[str, Any]) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "finalize_autonomy_record.py"),
                str(corpus["audit_dir"] / "top.yaml"),
                "--validate",
                "--sweep",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["records"] == {"top.yaml": []}

    def test_live_sibling_lookup_reads_direct_children_only(
        self, corpus: Dict[str, Any]
    ) -> None:
        audit_dir, top = corpus["audit_dir"], corpus["top"]
        _plant_history(audit_dir, "sibling.yaml", top)
        live = finalize_autonomy_record._live_siblings(audit_dir / "top.yaml", top)
        assert live == []
        # Control: the same identity as a direct child is a live sibling.
        _dump(audit_dir / "sibling.yaml", top)
        live = finalize_autonomy_record._live_siblings(audit_dir / "top.yaml", top)
        assert live == ["sibling.yaml"]


class TestDoctor:
    @staticmethod
    def _results(corpus: Dict[str, Any]) -> Dict[str, Any]:
        category = oacp_doctor.check_autonomy(
            corpus["project_dir"], yaml_loader=yaml.safe_load
        )
        return {result.name: result for result in category.results}

    def test_doctor_reads_direct_children_only(self, corpus: Dict[str, Any]) -> None:
        by_name = self._results(corpus)
        integrity = by_name[f"{RECEIVER}/autonomy-audit-integrity"]
        assert integrity.severity is oacp_doctor.Severity.ok
        assert "1 record(s) validated" in integrity.message
        refs = by_name[f"{RECEIVER}/autonomy-policy-refs"]
        assert refs.severity is oacp_doctor.Severity.ok

    def test_history_record_fails_doctor_as_a_direct_child(
        self, corpus: Dict[str, Any]
    ) -> None:
        _promote(corpus, "bad.yaml")
        by_name = self._results(corpus)
        integrity = by_name[f"{RECEIVER}/autonomy-audit-integrity"]
        assert integrity.severity is oacp_doctor.Severity.error
        assert "2 record(s)" in integrity.message
        refs = by_name[f"{RECEIVER}/autonomy-policy-refs"]
        assert refs.severity is oacp_doctor.Severity.warn


class TestGateReplayLookups:
    def test_prior_evaluations_read_direct_children_only(
        self, corpus: Dict[str, Any]
    ) -> None:
        audit_dir, top = corpus["audit_dir"], corpus["top"]
        # A history copy of the live record must not surface as a prior.
        _plant_history(audit_dir, "prior.yaml", top)
        priors = autonomy_gate.find_prior_evaluations(
            audit_dir, RECEIVER, top["message_id"]
        )
        assert [path.name for path, _ in priors] == ["top.yaml"]

    def test_archived_auto_accept_is_not_a_replay(
        self, corpus: Dict[str, Any]
    ) -> None:
        audit_dir, top = corpus["audit_dir"], corpus["top"]
        accepted = copy.deepcopy(top)
        accepted["decision"] = "auto_accepted"
        _plant_history(audit_dir, "accepted.yaml", accepted)
        assert (
            autonomy_gate.prior_auto_accept_exists(
                top["message_id"], RECEIVER, audit_dir
            )
            is False
        )
        # Control: the same record as a direct child is the replay the gate
        # refuses.
        _dump(audit_dir / "accepted.yaml", accepted)
        assert (
            autonomy_gate.prior_auto_accept_exists(
                top["message_id"], RECEIVER, audit_dir
            )
            is True
        )


def _reauthorization_record(message_id: str, message_sha256: str) -> Dict[str, Any]:
    """A complete, coherent resumed re-authorization: the one record shape
    that widens an envelope when it is a direct child."""
    assert "approved" in autonomy_gate.REAUTH_DECISIONS
    return {
        "message_id": message_id,
        "receiver": RECEIVER,
        "message_sha256": message_sha256,
        "result": {
            "final_state": "pending",
            "threshold_checkpoint": {
                "reauthorization": {
                    "presented": True,
                    "channel": autonomy_gate.REAUTH_GOVERNING_CHANNELS[0],
                    "decision": "approved",
                    "disposition": "resumed",
                    "scope": {"max_actual_files_touched": 9},
                }
            },
        },
    }


class TestEnvelopeOverlay:
    def test_overlay_lookup_reads_direct_children_only(
        self, corpus: Dict[str, Any]
    ) -> None:
        context = hook.WorkspaceContext(
            oacp_root=corpus["oacp_root"],
            project="proj",
            receiver=RECEIVER,
            message_id="msg-20260901000000-alice-0099",
            message_sha256="a" * 64,
        )
        audit_dir = context.audit_dir()
        assert audit_dir == corpus["audit_dir"]
        record = _reauthorization_record(context.message_id, context.message_sha256)
        _plant_history(audit_dir, "reauth.yaml", record)
        assert hook._reauthorized_scope(context) is None
        # Control: as a direct child the same record widens the bound.
        _dump(audit_dir / "reauth.yaml", record)
        assert hook._reauthorized_scope(context) == {"max_actual_files_touched": 9}
