# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Doctor setup checks for the org-memory debrief store.

The doctor's contract is setup-level only: directory presence, canonical
path layout, staging leftovers, and irregular entries. It never opens
debrief files — content and format verification belong to the writer
contract and git history.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from oacp_doctor import Severity, check_org_memory  # noqa: E402

CASES_DIR = Path(__file__).resolve().parent / "conformance" / "org_memory" / "cases"
CASE_NAMES = sorted(
    p.name for p in CASES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
)


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_conformance_case(case_name: str, tmp_path: Path) -> None:
    case = CASES_DIR / case_name
    expected = yaml.safe_load((case / "expected.yaml").read_text(encoding="utf-8"))
    root = tmp_path / "oacp"
    shutil.copytree(case / "org-memory", root / "org-memory")

    cat = check_org_memory(root)

    actual = sorted(
        (r.name, r.severity.value)
        for r in cat.results
        if r.severity in (Severity.warn, Severity.error)
    )
    wanted = sorted(
        (finding["name"], finding["severity"])
        for finding in (expected.get("findings") or [])
    )
    assert actual == wanted, [f"{r.name}:{r.severity.value}:{r.message}" for r in cat.results]
    for finding in expected.get("findings") or []:
        needle = finding.get("message_contains")
        if needle:
            assert any(
                r.name == finding["name"] and needle in r.message
                for r in cat.results
            ), f"no {finding['name']} message containing {needle!r}"


# ── Unit checks ──────────────────────────────────────────────────────────


def _write_debrief(root: Path, name: str = "20260825-alice-1f3a9c2b.md") -> Path:
    path = root / "org-memory" / "debriefs" / "demo-project" / "2026" / "08" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nschema_version: 1\n---\nbody\n", encoding="utf-8")
    return path


def _rows(root: Path) -> list:
    return [(r.name, r.severity) for r in check_org_memory(root).results]


def test_canonical_layout_passes(tmp_path: Path) -> None:
    root = tmp_path / "oacp"
    root.mkdir()
    _write_debrief(root)

    assert ("debriefs-layout", Severity.ok) in _rows(root)


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits ignored as root")
def test_content_is_never_opened(tmp_path: Path) -> None:
    # Setup-only contract: a record whose CONTENT is unreadable is still a
    # clean setup — the doctor must not open debrief files at all.
    root = tmp_path / "oacp"
    root.mkdir()
    record = _write_debrief(root)
    os.chmod(record, 0o000)
    try:
        rows = _rows(root)
    finally:
        os.chmod(record, 0o644)
    assert ("debriefs-layout", Severity.ok) in rows
    assert not any(name == "debriefs-unreadable" for name, _ in rows)


def test_staging_artifact_reported(tmp_path: Path) -> None:
    root = tmp_path / "oacp"
    root.mkdir()
    real = _write_debrief(root)
    (real.parent / ".stage.20260825-alice-1f3a9c2b.md.a1b2").write_text(
        "partial", encoding="utf-8"
    )

    assert ("debriefs-staging", Severity.warn) in _rows(root)


def test_symlinked_record_flagged(tmp_path: Path) -> None:
    root = tmp_path / "oacp"
    root.mkdir()
    real = _write_debrief(root)
    (real.parent / "20260825-alice-99zz00aa.md").symlink_to(real)

    rows = _rows(root)
    assert ("debriefs-irregular", Severity.error) in rows
    # The regular record still passes the layout check.
    assert ("debriefs-layout", Severity.ok) in rows


def test_symlinked_directory_flagged_and_not_traversed(tmp_path: Path) -> None:
    root = tmp_path / "oacp"
    root.mkdir()
    _write_debrief(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "org-memory" / "debriefs" / "linked-project").symlink_to(
        outside, target_is_directory=True
    )

    assert ("debriefs-irregular", Severity.error) in _rows(root)


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits ignored as root")
def test_unreadable_directory_is_not_a_clean_empty_store(tmp_path: Path) -> None:
    root = tmp_path / "oacp"
    root.mkdir()
    real = _write_debrief(root)
    blocked = real.parent.parent.parent  # demo-project/
    os.chmod(blocked, 0o000)
    try:
        cat = check_org_memory(root)
    finally:
        os.chmod(blocked, 0o755)
    rows = [(r.name, r.severity) for r in cat.results]
    assert ("debriefs-unreadable", Severity.error) in rows
    assert not any(
        r.name == "debriefs-layout" and "empty store" in r.message
        for r in cat.results
    )


def test_directory_classification_failure_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An is_symlink failure on a directory entry is reported, never raised.
    root = tmp_path / "oacp"
    root.mkdir()
    _write_debrief(root)
    real_is_symlink = Path.is_symlink

    def flaky(self):
        if self.name == "demo-project":
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", flaky)
    cat = check_org_memory(root)
    rows = [(r.name, r.severity) for r in cat.results]
    assert ("debriefs-unreadable", Severity.error) in rows
    assert not any(
        r.name == "debriefs-layout" and "empty store" in r.message
        for r in cat.results
    )


def test_record_classification_failure_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stat failure while classifying a record lands in the unreadable
    # row; other records still pass.
    root = tmp_path / "oacp"
    root.mkdir()
    _write_debrief(root)
    victim = _write_debrief(root, "20260825-bob-77aa88bb.md")
    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self.name == victim.name:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    rows = _rows(root)
    assert ("debriefs-unreadable", Severity.error) in rows
    assert ("debriefs-layout", Severity.ok) in rows
