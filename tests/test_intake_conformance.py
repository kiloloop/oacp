# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Executable intake conformance: verify_mode matrix at the gate's intake.

Each golden under ``tests/conformance/intake/expected/`` names a receiver
config (off/warn/enforce) and a message from the signing corpus, and pins
the intake action, exit code, annotation label, and quarantine behavior.
The real gate CLI runs against a scratch receiver workspace per case.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import autonomy_gate  # noqa: E402

FIXTURE_ROOT = Path(__file__).parent / "conformance" / "intake"
SIGNING_ROOT = Path(__file__).parent / "conformance" / "signing"

EXPECTED_FILES = sorted((FIXTURE_ROOT / "expected").glob("*.yaml"))


def _build_workspace(tmp_path: Path, fixture: dict) -> dict:
    """Scratch OACP home with one receiver ("dave") holding the fixture."""
    home = tmp_path / "oacp_home"
    receiver_dir = home / "projects" / "intake-conformance" / "agents" / "dave"
    inbox = receiver_dir / "inbox"
    trust = receiver_dir / "trust"
    for directory in (home / "keys", inbox, trust):
        directory.mkdir(parents=True)

    config_path = receiver_dir / "config.yaml"
    shutil.copyfile(FIXTURE_ROOT / fixture["config"], config_path)
    shutil.copyfile(
        SIGNING_ROOT / "pins" / "allowed_signers.yaml",
        trust / "allowed_signers.yaml",
    )
    message_path = inbox / Path(fixture["message"]).name
    message_path.write_bytes((FIXTURE_ROOT / fixture["message"]).read_bytes())
    return {
        "home": home,
        "config_path": config_path,
        "message_path": message_path,
        "dead_letter": receiver_dir / "dead_letter",
    }


@pytest.mark.parametrize(
    "expected_path", EXPECTED_FILES, ids=[p.stem for p in EXPECTED_FILES]
)
def test_intake_matches_conformance_golden(
    expected_path: Path, tmp_path: Path
) -> None:
    fixture = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
    expected = fixture["expected"]
    workspace = _build_workspace(tmp_path, fixture)

    original_bytes = workspace["message_path"].read_bytes()
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = autonomy_gate.main(
            [
                "--config", str(workspace["config_path"]),
                "--message", str(workspace["message_path"]),
                "--receiver", "dave",
                "--oacp-dir", str(workspace["home"]),
            ]
        )

    assert exit_code == expected["exit_code"], stderr.getvalue()

    label = expected["annotation_label"]
    if label is None:
        assert "[oacp-auth]" not in stderr.getvalue()
    else:
        assert f"[oacp-auth] {label}" in stderr.getvalue()

    quarantine_files = (
        sorted(workspace["dead_letter"].iterdir())
        if workspace["dead_letter"].is_dir()
        else []
    )
    if expected["quarantined"]:
        assert len(quarantine_files) == 1
        copy = quarantine_files[0]
        assert copy.read_bytes() == original_bytes
        assert (copy.stat().st_mode & 0o777) == 0o600
    else:
        assert quarantine_files == []

    decision = yaml.safe_load(stdout.getvalue())
    if expected["intake_action"] == "reject":
        assert decision["decision"] == "intake_rejected"
        assert decision["verify_mode"] == "enforce"
        assert decision["quarantine_copy"] == str(quarantine_files[0])
    else:
        assert decision["decision"] in {"auto_accepted", "paused"}

    # No-clobber: the original inbox artifact is never touched.
    assert workspace["message_path"].read_bytes() == original_bytes


def test_matrix_is_complete() -> None:
    """4 failure classes x 3 modes, plus the enforce positive control."""
    names = {path.stem for path in EXPECTED_FILES}
    failure_cases = {
        "unsigned_notification",
        "tamper_body_flip",
        "tamper_kid_unknown",
        "signed_revoked",
    }
    wanted = {
        f"{case}__{mode}"
        for case in failure_cases
        for mode in ("off", "warn", "enforce")
    } | {"signed_basic__enforce"}
    assert names == wanted
