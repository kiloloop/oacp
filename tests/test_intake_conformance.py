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
from message_signing import CRYPTO_AVAILABLE  # noqa: E402

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
    label = expected["annotation_label"]
    if not CRYPTO_AVAILABLE and (label or "").startswith("signed-"):
        pytest.skip(
            "golden pins a signature-evaluation outcome; "
            "needs the [crypto] extra"
        )
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


BODY_FIXTURE = yaml.safe_load((FIXTURE_ROOT / "body_schema.yaml").read_text())
BODY_GOLDENS = sorted((FIXTURE_ROOT / "expected" / "body_schema").glob("*.yaml"))
BODY_ROWS = [
    (yaml.safe_load(path.read_text()), case)
    for path in BODY_GOLDENS
    for case in BODY_FIXTURE["cases"]
]


@pytest.mark.skipif(not CRYPTO_AVAILABLE, reason="signed body matrix needs [crypto]")
@pytest.mark.parametrize(
    "fixture,case_name", BODY_ROWS,
    ids=[f"{row['message_type']}__{row['mode']}__{case}" for row, case in BODY_ROWS],
)
def test_body_schema_boundary(fixture, case_name, tmp_path, monkeypatch):
    """Execute validation CLI and real verified gate; read persisted notes back."""
    import json

    import validate_message
    from message_signing import generate_keypair, load_signers, sign_and_append

    kind, mode = fixture["message_type"], fixture["mode"]
    case = BODY_FIXTURE["cases"][case_name]
    expected = fixture["expected"][case_name]
    home = tmp_path / "home"
    receiver = home / "projects" / "body-conformance" / "agents" / "dave"
    inbox = receiver / "inbox"
    inbox.mkdir(parents=True)
    (receiver / "trust").mkdir()
    key = generate_keypair("carol", home)
    stub = json.loads(Path(key["public_stub_path"]).read_text())
    (receiver / "trust" / "allowed_signers.yaml").write_text(yaml.safe_dump({
        "version": 1, "signers": [{
            "agent": "carol", "kid": stub["kid"], "jwk": stub["jwk"],
            "status": "active",
        }],
    }))
    config = yaml.safe_load((FIXTURE_ROOT / "configs" / f"{mode}.yaml").read_text())
    config["autonomy"]["allow_without_task_profile"] = (
        [] if case.get("require_profile") else ["handoff", "handoff_complete"]
    )
    if case.get("always_pause"):
        config["autonomy"]["default_mode"] = "always_pause"
    config_path = receiver / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    body = case.get("body", BODY_FIXTURE["valid_bodies"][kind])
    body += case.get("append_body", "")
    if "repeat_body" in case:
        body = "x" * case["repeat_body"]
    message = {
        "id": "msg-body-conformance", "from": "carol", "to": "dave",
        "type": kind, "priority": "P2", "created_at_utc": "2026-09-14T00:00:00Z",
        "subject": "Body validation", "body": body,
    }
    message.update(case.get("set", {}))
    for field in case.get("remove", []):
        del message[field]
    raw = yaml.safe_dump(message, sort_keys=False).encode()
    signing = case.get("signing", "valid")
    if signing in {"valid", "nonfinal"}:
        raw = sign_and_append(raw, load_signers("carol", home))
        if signing == "nonfinal":
            raw += b"\n"
    elif signing == "malformed":
        raw += b'auth: "not-jws"\n'
    path = inbox / "message.yaml"
    path.write_bytes(raw)

    notes = []
    errors = validate_message.validate_message_file(path, advisories=notes)
    if expected.get("auth_error"):
        assert errors and all("auth" in error.lower() for error in errors)
        if expected["auth_error"] == "framing":
            assert any("final physical line" in error for error in errors)
    else:
        assert errors == expected["validation_errors"]
    assert bool(notes) == expected["advisory"]
    for note in notes:
        assert set(note) == {"code", "severity", "detail"}
        assert note["code"] == "skill_owned_body_schema"
        assert note["severity"] == "advisory"
        assert note["detail"].startswith(kind + " body: ")

    # --quiet suppresses only success, never the one-wave advisory.
    monkeypatch.setattr(sys, "argv", ["validate", str(path), "--quiet"])
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        validate_rc = validate_message.main()
    assert validate_rc == (1 if errors else 0)
    assert stdout.getvalue() == ""
    assert ("ADVISORY: skill_owned_body_schema:" in stderr.getvalue()) == bool(notes)

    audit_dir = receiver / "audit" / "autonomy_decisions"
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        gate_rc = autonomy_gate.main([
            "--config", str(config_path), "--message", str(path),
            "--receiver", "dave", "--oacp-dir", str(home),
            "--audit-dir", str(audit_dir),
        ])
    decision = json.loads(stdout.getvalue())
    assert decision["decision"] == expected["decision"], decision
    rejected = expected["decision"] == "intake_rejected"
    assert gate_rc == (3 if rejected else 0)
    records = list(audit_dir.glob("*.yaml"))
    if rejected:
        assert not records
        assert "logged_notes" not in decision  # verify before parsing body
    else:
        if expected["reason"]:
            assert expected["reason"] in decision["reason_codes"]
        assert "skill_owned_body_schema" not in decision["reason_codes"]
        recorded = [n for n in decision["logged_notes"]
                    if n["code"] == "skill_owned_body_schema"]
        assert recorded == ([] if case.get("always_pause") else notes)
        assert len(records) == 1
        audit = yaml.safe_load(records[0].read_text())
        assert audit["logged_notes"] == decision["logged_notes"]
    assert path.read_bytes() == raw


def test_body_matrix_is_complete():
    assert {(row["message_type"], row["mode"]) for row, _ in BODY_ROWS} == {
        (kind, mode) for kind in ("handoff", "handoff_complete")
        for mode in ("off", "warn", "enforce")
    }
    for path in BODY_GOLDENS:
        assert set(yaml.safe_load(path.read_text())["expected"]) == set(BODY_FIXTURE["cases"])
