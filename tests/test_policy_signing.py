# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Policy-file signing: authorized-policy identity, fail-closed tamper."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import autonomy_gate  # noqa: E402
import trust_cli  # noqa: E402
from message_signing import (  # noqa: E402
    CRYPTO_AVAILABLE,
    generate_keypair,
    load_signers,
    sign_and_append,
    split_signed_message,
)
from message_verify import intake_verify, load_allowed_signers  # noqa: E402
from policy_signing import (  # noqa: E402
    POLICY_KIND_ALLOWED_SIGNERS,
    POLICY_KIND_RECEIVER_CONFIG,
    POLICY_STATUS_INVALID,
    POLICY_STATUS_UNSIGNED,
    POLICY_STATUS_VERIFIED,
    PolicyAuthError,
    policy_context,
    require_policy_authorized,
    sign_policy_file,
    verify_policy_bytes,
    verify_policy_file,
)
from send_inbox_message import build_message_dict, render_yaml  # noqa: E402

PROJECT = "policy-signing"
CONFIG_CTX = policy_context(PROJECT, "dave", POLICY_KIND_RECEIVER_CONFIG)
PINS_CTX = policy_context(PROJECT, "dave", POLICY_KIND_ALLOWED_SIGNERS)

pytestmark = pytest.mark.skipif(
    not CRYPTO_AVAILABLE, reason="requires the cryptography extra"
)

CONFIG_TEXT = """\
signing:
  verify_mode: enforce
autonomy:
  default_mode: auto_review
  auto_review_thresholds:
    max_estimated_minutes: 45
    max_expected_files_touched: 5
    destructive_ops: pause
    external_side_effects: pause
    auth_config_or_secrets: pause
    dependency_changes: pause
    public_visibility: pause
    git_push_or_deploy: pause
  allow_without_task_profile:
    - notification
"""


@pytest.fixture()
def workspace(tmp_path: Path) -> dict:
    home = tmp_path / "oacp_home"
    (home / "keys").mkdir(parents=True)
    receiver_dir = home / "projects" / "policy-signing" / "agents" / "dave"
    (receiver_dir / "inbox").mkdir(parents=True)
    (receiver_dir / "trust").mkdir(parents=True)

    report = generate_keypair("dave", home)
    stub = json.loads(Path(report["public_stub_path"]).read_text(encoding="utf-8"))

    config_path = receiver_dir / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    pins_path = receiver_dir / "trust" / "allowed_signers.yaml"
    pins_path.write_text(
        "version: 1\n"
        "signers:\n"
        "  - agent: dave\n"
        f"    kid: {stub['kid']}\n"
        "    jwk:\n"
        "      kty: OKP\n"
        "      crv: Ed25519\n"
        f"      x: {stub['jwk']['x']}\n"
        "    status: active\n",
        encoding="utf-8",
    )
    return {
        "home": home,
        "receiver_dir": receiver_dir,
        "config_path": config_path,
        "pins_path": pins_path,
        "kid": stub["kid"],
        "key_path": Path(report["key_path"]),
    }


def _sign_both(workspace: dict) -> None:
    signers = load_signers("dave", workspace["home"])
    sign_policy_file(workspace["config_path"], signers, context=CONFIG_CTX)
    sign_policy_file(workspace["pins_path"], signers, context=PINS_CTX)


def _enroll_both(workspace: dict) -> None:
    """Sign + enroll via the CLI path (the production signing flow)."""
    exit_code = trust_cli.main(
        [
            "sign-policy",
            "--project", PROJECT,
            "--agent", "dave",
            "--oacp-dir", str(workspace["home"]),
        ]
    )
    assert exit_code == 0


def _verify(workspace: dict, path: Path) -> dict:
    kind = (
        POLICY_KIND_RECEIVER_CONFIG
        if path.name == "config.yaml"
        else POLICY_KIND_ALLOWED_SIGNERS
    )
    return verify_policy_file(
        path, workspace["home"], receiver="dave", kind=kind
    )


def _write_message(workspace: dict, *, signed: bool) -> Path:
    payload = render_yaml(
        build_message_dict(
            sender="dave",
            recipient="dave",
            msg_type="notification",
            subject="Policy signing round-trip",
            body="content line",
        )
    ).encode("utf-8")
    if signed:
        payload = sign_and_append(payload, load_signers("dave", workspace["home"]))
    path = workspace["receiver_dir"] / "inbox" / "message.yaml"
    path.write_bytes(payload)
    return path


def test_sign_verify_roundtrip_and_resign(workspace: dict) -> None:
    _sign_both(workspace)
    for path in (workspace["config_path"], workspace["pins_path"]):
        auth = _verify(workspace, path)
        assert auth["status"] == POLICY_STATUS_VERIFIED
        assert auth["signer_agent"] == "dave"
        assert auth["signer_kid"] == workspace["kid"]
        # The signed file still parses as YAML, with the trailer as a key.
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "auth" in data

    # Re-signing after an edit is the normal flow and round-trips again.
    config = workspace["config_path"]
    body = config.read_bytes().decode("utf-8")
    config.write_text(
        body.replace("max_estimated_minutes: 45", "max_estimated_minutes: 40"),
        encoding="utf-8",
    )
    report = sign_policy_file(
        config, load_signers("dave", workspace["home"]), context=CONFIG_CTX
    )
    assert report["resigned"] is True
    auth = _verify(workspace, config)
    assert auth["status"] == POLICY_STATUS_VERIFIED


def test_unsigned_policy_is_bootstrap_loadable(workspace: dict) -> None:
    for path in (workspace["config_path"], workspace["pins_path"]):
        auth = _verify(workspace, path)
        assert auth["status"] == POLICY_STATUS_UNSIGNED
        require_policy_authorized(auth, path)  # does not raise


def test_tamper_after_signing_fails_closed(workspace: dict) -> None:
    _sign_both(workspace)
    tampers = {
        workspace["config_path"]: (
            b"max_estimated_minutes: 45",
            b"max_estimated_minutes: 99",
        ),
        workspace["pins_path"]: (b"agent: dave", b"agent: eve"),
    }
    for path, (old, new) in tampers.items():
        tampered = path.read_bytes().replace(old, new, 1)
        assert tampered != path.read_bytes()
        path.write_bytes(tampered)
        auth = _verify(workspace, path)
        assert auth["status"] == POLICY_STATUS_INVALID
        with pytest.raises(PolicyAuthError):
            require_policy_authorized(auth, path)


def test_foreign_local_key_does_not_authorize_receiver_policy(
    workspace: dict,
) -> None:
    generate_keypair("mallory", workspace["home"])
    sign_policy_file(
        workspace["config_path"],
        load_signers("mallory", workspace["home"]),
        context=CONFIG_CTX,
    )
    auth = _verify(workspace, workspace["config_path"])
    assert auth["status"] == POLICY_STATUS_INVALID
    assert "not the receiver" in auth["reason"]


def test_message_signature_never_authorizes_policy(workspace: dict) -> None:
    # Sign the config with the MESSAGE profile: same key, wrong class.
    payload = workspace["config_path"].read_bytes()
    signed = sign_and_append(payload, load_signers("dave", workspace["home"]))
    workspace["config_path"].write_bytes(signed)
    auth = _verify(workspace, workspace["config_path"])
    assert auth["status"] == POLICY_STATUS_INVALID


def test_auth_like_final_line_is_invalid_not_unsigned(workspace: dict) -> None:
    raw = workspace["config_path"].read_bytes() + b'auth: "not*base64url"\n'
    auth = verify_policy_bytes(raw, {}, receiver="dave")
    assert auth["status"] == POLICY_STATUS_INVALID


def _run_gate(workspace: dict, message_path: Path, audit_dir: Path) -> tuple:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = autonomy_gate.main(
            [
                "--config", str(workspace["config_path"]),
                "--message", str(message_path),
                "--receiver", "dave",
                "--audit-dir", str(audit_dir),
                "--oacp-dir", str(workspace["home"]),
            ]
        )
    return exit_code, stdout.getvalue(), stderr.getvalue()


def test_gate_records_authorized_policy_identity(
    workspace: dict, tmp_path: Path
) -> None:
    _sign_both(workspace)
    message_path = _write_message(workspace, signed=True)
    audit_dir = tmp_path / "audit"

    exit_code, stdout, stderr = _run_gate(workspace, message_path, audit_dir)
    assert exit_code == 0, stderr
    decision = json.loads(stdout)
    assert decision["decision"] == "auto_accepted"
    assert decision["policy_auth"]["status"] == "verified"
    assert decision["policy_auth"]["signer_agent"] == "dave"
    assert decision["policy_auth"]["signer_kid"] == workspace["kid"]

    (audit_path,) = audit_dir.glob("*.yaml")
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert audit["policy_auth"]["status"] == "verified"
    assert audit["policy_sha256"] == decision["policy_sha256"]


def test_policy_sha256_is_trailer_independent(workspace: dict, tmp_path: Path) -> None:
    message_path = _write_message(workspace, signed=True)
    # Unsigned run, then signed run: verify_mode enforce holds for both, and
    # the hash must name the same policy content.
    exit_code, stdout, _ = _run_gate(workspace, message_path, tmp_path / "a")
    assert exit_code == 0
    unsigned_hash = json.loads(stdout)["policy_sha256"]
    _sign_both(workspace)
    exit_code, stdout, _ = _run_gate(workspace, message_path, tmp_path / "b")
    assert exit_code == 0
    assert json.loads(stdout)["policy_sha256"] == unsigned_hash


def test_gate_fails_closed_on_tampered_config(
    workspace: dict, tmp_path: Path
) -> None:
    _sign_both(workspace)
    config = workspace["config_path"]
    # Flip enforce to off AFTER signing: the tamper must not get to choose
    # its own verify mode, and the gate must refuse to evaluate.
    config.write_bytes(
        config.read_bytes().replace(b"verify_mode: enforce", b"verify_mode: off")
    )
    message_path = _write_message(workspace, signed=False)
    audit_dir = tmp_path / "audit"

    exit_code, stdout, stderr = _run_gate(workspace, message_path, audit_dir)
    assert exit_code == 0, stderr
    decision = json.loads(stdout)
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["policy_auth_invalid"]
    assert decision["result"]["completion_kind"] == "config_malformed"
    assert decision["policy_auth"]["status"] == "invalid"

    (audit_path,) = audit_dir.glob("*.yaml")
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert audit["reason_codes"] == ["policy_auth_invalid"]


def test_tampered_trust_root_rejects_at_enforce_intake(
    workspace: dict, tmp_path: Path
) -> None:
    _sign_both(workspace)
    message_path = _write_message(workspace, signed=True)

    intake = intake_verify(
        message_path,
        workspace["config_path"],
        receiver="dave",
        oacp_dir=str(workspace["home"]),
    )
    assert intake["action"] == "proceed"
    assert intake["message_auth"]["status"] == "verified"

    pins = workspace["pins_path"]
    pins.write_bytes(pins.read_bytes().replace(b"status: active", b"status: active ", 1))
    intake = intake_verify(
        message_path,
        workspace["config_path"],
        receiver="dave",
        oacp_dir=str(workspace["home"]),
    )
    assert intake["action"] == "reject"
    assert "trust root signature invalid" in intake["message_auth"]["reason"]


# ---------------------------------------------------------------------------
# Single-read snapshot discipline: verified bytes ARE the evaluated bytes
# ---------------------------------------------------------------------------

def test_gate_never_rereads_config_after_verification(
    workspace: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping the config after its verification must not change the mode.

    A deterministic verify-then-swap: the config is verified as enforce,
    then replaced on disk with an unsigned off-mode config before the gate
    proceeds. The gate must keep enforcing from its verified snapshot —
    the unsigned message rejects at intake (exit 3), never auto-accepts.
    """
    import policy_signing as ps

    _sign_both(workspace)
    message_path = _write_message(workspace, signed=False)
    original = ps.verify_policy_data

    def swap_after_verify(raw: bytes, home: Path, **kwargs: object) -> dict:
        result = original(raw, home, **kwargs)
        workspace["config_path"].write_text(
            "signing:\n  verify_mode: off\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(ps, "verify_policy_data", swap_after_verify)
    exit_code, stdout, stderr = _run_gate(
        workspace, message_path, tmp_path / "audit"
    )
    assert exit_code == 3, stderr
    assert json.loads(stdout)["decision"] == "intake_rejected"


def test_gate_evaluates_verified_message_snapshot(
    workspace: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping the message after intake verification must not be evaluated."""
    import hashlib

    import message_verify as mv

    _sign_both(workspace)
    message_path = _write_message(workspace, signed=True)
    original_bytes = message_path.read_bytes()
    orig_intake = mv.intake_verify

    def swap_after_intake(*args: object, **kwargs: object) -> dict:
        result = orig_intake(*args, **kwargs)
        message_path.write_text(
            "id: msg-swapped\n"
            "from: eve\n"
            "to: dave\n"
            "type: notification\n"
            'created_at_utc: "2026-01-01T00:00:00Z"\n'
            "subject: swapped\n"
            "body: swapped\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(mv, "intake_verify", swap_after_intake)
    exit_code, stdout, stderr = _run_gate(
        workspace, message_path, tmp_path / "audit"
    )
    assert exit_code == 0, stderr
    decision = json.loads(stdout)
    assert decision["sender"] == "dave"
    assert (
        decision["message_sha256"]
        == hashlib.sha256(original_bytes).hexdigest()
    )


# ---------------------------------------------------------------------------
# Enrollment: signature stripping / unsupported downgrades fail closed
# ---------------------------------------------------------------------------

def test_signature_stripping_after_enrollment_fails_closed(
    workspace: dict,
) -> None:
    _enroll_both(workspace)
    for path in (workspace["config_path"], workspace["pins_path"]):
        prefix, auth_value = split_signed_message(path.read_bytes())
        assert auth_value is not None
        path.write_bytes(prefix)
        auth = _verify(workspace, path)
        assert auth["status"] == POLICY_STATUS_INVALID
        assert "enrolled" in auth["reason"]


def test_unsupported_crypto_on_enrolled_policy_fails_closed(
    workspace: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    import policy_signing as ps

    _enroll_both(workspace)
    monkeypatch.setattr(ps, "CRYPTO_AVAILABLE", False)
    auth = _verify(workspace, workspace["config_path"])
    assert auth["status"] == POLICY_STATUS_INVALID
    assert "cryptography" in auth["reason"]


def test_gate_fails_closed_on_stripped_enrolled_config(
    workspace: dict, tmp_path: Path
) -> None:
    _enroll_both(workspace)
    config = workspace["config_path"]
    prefix, _auth = split_signed_message(config.read_bytes())
    # Strip the trailer AND flip the mode: the classic downgrade attempt.
    config.write_bytes(prefix.replace(b"verify_mode: enforce", b"verify_mode: off"))
    message_path = _write_message(workspace, signed=False)

    exit_code, stdout, stderr = _run_gate(workspace, message_path, tmp_path / "a")
    assert exit_code == 0, stderr
    decision = json.loads(stdout)
    assert decision["decision"] == "paused"
    assert decision["reason_codes"] == ["policy_auth_invalid"]


# ---------------------------------------------------------------------------
# Context binding: a signature authorizes one project/receiver/kind only
# ---------------------------------------------------------------------------

def test_cross_project_replay_is_rejected(workspace: dict) -> None:
    _sign_both(workspace)
    other_dir = (
        workspace["home"] / "projects" / "other-project" / "agents" / "dave"
    )
    other_dir.mkdir(parents=True)
    replayed = other_dir / "config.yaml"
    replayed.write_bytes(workspace["config_path"].read_bytes())

    auth = verify_policy_file(
        replayed,
        workspace["home"],
        receiver="dave",
        kind=POLICY_KIND_RECEIVER_CONFIG,
    )
    assert auth["status"] == POLICY_STATUS_INVALID
    assert "context mismatch" in auth["reason"]


def test_cross_kind_replay_is_rejected(workspace: dict) -> None:
    # A receiver-config signature must never authorize the trust root.
    _sign_both(workspace)
    workspace["pins_path"].write_bytes(workspace["config_path"].read_bytes())
    auth = _verify(workspace, workspace["pins_path"])
    assert auth["status"] == POLICY_STATUS_INVALID
    assert "context mismatch" in auth["reason"]


def test_signed_policy_outside_workspace_layout_is_invalid(
    workspace: dict, tmp_path: Path
) -> None:
    _sign_both(workspace)
    stray = tmp_path / "stray-config.yaml"
    stray.write_bytes(workspace["config_path"].read_bytes())
    auth = verify_policy_file(
        stray,
        workspace["home"],
        receiver="dave",
        kind=POLICY_KIND_RECEIVER_CONFIG,
    )
    assert auth["status"] == POLICY_STATUS_INVALID
    assert "context unresolved" in auth["reason"]


# ---------------------------------------------------------------------------
# Policy writers: enrolled trust files re-sign atomically or refuse
# ---------------------------------------------------------------------------

def test_revoke_resigns_enrolled_trust_root(workspace: dict) -> None:
    from trust_root import revoke_pin

    _enroll_both(workspace)
    project_dir = workspace["home"] / "projects" / PROJECT
    report = revoke_pin(project_dir, workspace["kid"], receiver="dave")
    assert report["receivers"]["dave"] == "revoked"

    auth = _verify(workspace, workspace["pins_path"])
    assert auth["status"] == POLICY_STATUS_VERIFIED
    pins = load_allowed_signers(workspace["pins_path"])
    assert pins[workspace["kid"]]["status"] == "revoked"


def test_revoke_refuses_when_enrolled_and_key_unavailable(
    workspace: dict,
) -> None:
    from message_verify import TrustRootError
    from trust_root import revoke_pin

    _enroll_both(workspace)
    before = workspace["pins_path"].read_bytes()
    workspace["key_path"].unlink()

    project_dir = workspace["home"] / "projects" / PROJECT
    with pytest.raises(TrustRootError, match="refusing to write"):
        revoke_pin(project_dir, workspace["kid"], receiver="dave")
    # Refused atomically: the trust root is untouched, never de-signed.
    assert workspace["pins_path"].read_bytes() == before


def test_import_resigns_enrolled_trust_root(workspace: dict) -> None:
    from trust_root import import_public_stub

    _enroll_both(workspace)
    peer = generate_keypair("carol", workspace["home"])
    project_dir = workspace["home"] / "projects" / PROJECT
    report = import_public_stub(
        Path(peer["public_stub_path"]), project_dir, receiver="dave"
    )
    assert report["pins"] == "added"

    auth = _verify(workspace, workspace["pins_path"])
    assert auth["status"] == POLICY_STATUS_VERIFIED
    pins = load_allowed_signers(workspace["pins_path"])
    assert peer["kid"] in pins
