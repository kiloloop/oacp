#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""policy_signing.py — sign and verify receiver policy files.

The autonomy audit record's ``policy_sha256`` proves WHICH policy ran; this
module proves the policy was AUTHORIZED. ``config.yaml`` and
``trust/allowed_signers.yaml`` carry the same raw-prefix detached-JWS auth
trailer as messages, under a distinct JOSE ``typ``/``domain`` pair
(``oacp-policy+yaml`` / ``urn:oacp:policy:v1``) so a message signature can
never authorize a policy file and a policy signature can never authenticate
a message.

Trust anchor: the machine-local keystore under ``$OACP_HOME/keys/``. A
policy file for receiver X must verify against one of agent X's own public
keys (the ``<kid>.pub.json`` stubs written by ``oacp key gen``). Tampering
with a policy file on disk therefore requires the 0600 private key material,
not just filesystem write access. The receiver-binding check is what stops a
co-located agent's key from authorizing another receiver's policy.

Context binding: every policy signature commits to the target it authorizes
— ``{project, receiver, kind}`` — inside the protected header's ``oacp``
claim. A signature over one project's policy can never authorize a
byte-identical file in another project, and a ``config.yaml`` signature can
never authorize an ``allowed_signers.yaml`` (or vice versa), even under the
same key.

Enrollment (downgrade resistance): the first successful signing of a policy
target records it in the machine-local enrollment registry
(``$OACP_HOME/keys/policy_enrollment.json`` — part of the keystore trust
anchor, deliberately outside every project workspace). Once enrolled, a
policy file that shows up without a verifiable signature — trailer stripped,
or crypto unavailable — is INVALID, not "unsigned": stripping a signature is
tampering, never a downgrade back to bootstrap.

Verification outcomes (the load contract):

- ``unsigned`` — no auth trailer AND the target was never enrolled.
  Loadable; recorded as unsigned. This is the bootstrap state: a fresh
  workspace signs its policy files after the first ``oacp key gen`` via
  ``oacp trust sign-policy``, which enrolls the target.
- ``verified`` — the trailer verifies against a keystore key owned by the
  receiver agent AND the signed context matches the target. The signer
  identity binds the audit record's ``policy_sha256`` to an authorized
  policy identity.
- ``invalid`` — a trailer is present but fails framing, the policy JOSE
  profile, the receiver binding, the context binding, or crypto — or the
  target is enrolled and no verifiable signature is present. FAIL CLOSED:
  loaders refuse the file, and the failure is recorded distinguishably
  from "policy absent".
- ``unsupported`` — the ``cryptography`` extra is unavailable, so a present
  trailer cannot be checked, and the target is NOT enrolled. Loaders
  proceed with the status recorded (mirrors the message-verify posture;
  signing itself always requires the extra, so this state only occurs on a
  crypto-less host reading a workspace signed elsewhere). An enrolled
  target in this situation is ``invalid``.

Unlike messages (append-once, immutable), policy files are long-lived and
edited: signing strips any existing trailer and signs the current content,
so re-signing after a policy edit is the normal flow. Policy WRITERS that
re-render an enrolled file (trust import/revoke) must re-sign atomically or
refuse the mutation — a writer must never strip an enrolled file's
signature as a side effect.

`load_authorized_policy` is the single authorized read path: it reads the
file once into a bounded snapshot, verifies THOSE bytes, and parses the
policy from THE SAME bytes — consumers never re-read the file after
verification, so the bytes evaluated are always the bytes verified.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_scripts_dir))

from _oacp_constants import locked_audit, utc_now_iso  # noqa: E402
from message_signing import (  # noqa: E402
    CRYPTO_AVAILABLE,
    AuthFormatError,
    FileKeySigner,
    b64url_decode,
    decode_auth_value,
    jwk_thumbprint,
    render_auth_line,
    sign_payload,
    signing_input,
    split_signed_message,
    validate_protected_header,
    validate_public_ed25519_jwk,
)

if CRYPTO_AVAILABLE:  # pragma: no cover - trivial import guard
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

POLICY_JWS_TYP = "oacp-policy+yaml"
POLICY_SIG_DOMAIN = "urn:oacp:policy:v1"

POLICY_STATUS_UNSIGNED = "unsigned"
POLICY_STATUS_VERIFIED = "verified"
POLICY_STATUS_INVALID = "invalid"
POLICY_STATUS_UNSUPPORTED = "unsupported"

POLICY_KIND_RECEIVER_CONFIG = "receiver_config"
POLICY_KIND_ALLOWED_SIGNERS = "allowed_signers"
_POLICY_KIND_RELPATHS = {
    POLICY_KIND_RECEIVER_CONFIG: ("config.yaml",),
    POLICY_KIND_ALLOWED_SIGNERS: ("trust", "allowed_signers.yaml"),
}
POLICY_CONTEXT_KEYS = ("project", "receiver", "kind")
POLICY_OACP_CLAIM = "policy"

# Policy files are small YAML documents; anything near this bound is not a
# policy. Bounding the read keeps an oversized artifact from being fully
# allocated before it is refused.
MAX_POLICY_BYTES = 1_048_576

KEYS_DIRNAME = "keys"
PUBLIC_STUB_GLOB = "*/*/*/*.pub.json"
ENROLLMENT_FILENAME = "policy_enrollment.json"
ENROLLMENT_VERSION = 1


class PolicyAuthError(ValueError):
    """Raised when a policy file must be authorized but is not."""


def _agent_name_from_urn(agent_urn_value: str) -> str:
    return agent_urn_value.rsplit(":", 1)[-1]


def policy_context(project: str, receiver: str, kind: str) -> Dict[str, str]:
    """Validated constructor for the signed policy-target context."""
    if kind not in _POLICY_KIND_RELPATHS:
        raise ValueError(f"unknown policy kind: {kind!r}")
    for label, value in (("project", project), ("receiver", receiver)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"policy context {label} must be a non-empty string")
    return {"project": project, "receiver": receiver, "kind": kind}


def derive_policy_context(
    path: Path, oacp_home: Path, *, receiver: str, kind: str
) -> Optional[Dict[str, str]]:
    """Resolve a policy file's context from its canonical workspace location.

    Returns None when the path does not sit at
    ``$OACP_HOME/projects/<project>/agents/<receiver>/<kind relpath>`` —
    a signed policy at an underivable location cannot have its binding
    confirmed and therefore never verifies.
    """
    expected_tail = _POLICY_KIND_RELPATHS.get(kind)
    if expected_tail is None:
        raise ValueError(f"unknown policy kind: {kind!r}")
    try:
        rel = Path(path).resolve().relative_to(
            (Path(oacp_home).expanduser() / "projects").resolve()
        )
    except ValueError:
        return None
    parts = rel.parts
    if (
        len(parts) == 3 + len(expected_tail)
        and parts[1] == "agents"
        and parts[2] == receiver
        and parts[3:] == expected_tail
    ):
        return policy_context(parts[0], receiver, kind)
    return None


# ---------------------------------------------------------------------------
# Enrollment registry (machine-local, inside the keystore trust anchor)
# ---------------------------------------------------------------------------

def _enrollment_path(oacp_home: Path) -> Path:
    return Path(oacp_home).expanduser() / KEYS_DIRNAME / ENROLLMENT_FILENAME


def _enrollment_key(context: Dict[str, str]) -> str:
    return "/".join(context[key] for key in POLICY_CONTEXT_KEYS)


def _load_enrollment(oacp_home: Path) -> Dict[str, Any]:
    path = _enrollment_path(oacp_home)
    if not path.is_file():
        return {"version": ENROLLMENT_VERSION, "enrolled": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # An unreadable registry must not silently un-enroll everything —
        # that would be exactly the downgrade the registry exists to stop.
        raise PolicyAuthError(
            f"policy enrollment registry {path} is unreadable ({exc}) — "
            "refusing to treat enrolled policies as unsigned"
        ) from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("enrolled"), dict):
        raise PolicyAuthError(
            f"policy enrollment registry {path} is malformed — refusing to "
            "treat enrolled policies as unsigned"
        )
    return loaded


def policy_enrolled(oacp_home: Path, context: Optional[Dict[str, str]]) -> bool:
    """True when this policy target has been enrolled for signing."""
    if context is None:
        return False
    registry = _load_enrollment(oacp_home)
    return _enrollment_key(context) in registry["enrolled"]


def record_policy_enrollment(
    oacp_home: Path, context: Dict[str, str], kid: str
) -> None:
    """Record (idempotently) that a policy target is signed from now on."""
    path = _enrollment_path(oacp_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_audit(path):
        registry = (
            _load_enrollment(oacp_home)
            if path.is_file()
            else {"version": ENROLLMENT_VERSION, "enrolled": {}}
        )
        entry = registry["enrolled"].get(_enrollment_key(context))
        if entry is None:
            registry["enrolled"][_enrollment_key(context)] = {
                "enrolled_at_utc": utc_now_iso(),
                "kid": kid,
            }
        else:
            entry["kid"] = kid
        content = json.dumps(registry, indent=2) + "\n"
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


# ---------------------------------------------------------------------------
# Keystore pins + bounded read
# ---------------------------------------------------------------------------

def load_keystore_public_pins(oacp_home: Path) -> Dict[str, Dict[str, Any]]:
    """Load the local keystore's public stubs as a ``kid -> entry`` map.

    Only structurally valid stubs whose kid matches their jwk thumbprint are
    usable as policy trust anchors; anything else is skipped (an unusable
    stub simply anchors nothing — it can never make a bad signature pass).
    """
    pins: Dict[str, Dict[str, Any]] = {}
    keys_root = Path(oacp_home).expanduser() / KEYS_DIRNAME
    if not keys_root.is_dir():
        return pins
    for stub_path in sorted(keys_root.glob(PUBLIC_STUB_GLOB)):
        try:
            stub = json.loads(stub_path.read_text(encoding="utf-8"))
            kid = stub.get("kid")
            agent = stub.get("agent")
            jwk = validate_public_ed25519_jwk(stub.get("jwk"))
            if not isinstance(kid, str) or jwk_thumbprint(jwk) != kid:
                continue
            if not isinstance(agent, str) or not agent:
                continue
        except (OSError, ValueError, AuthFormatError):
            continue
        pins[kid] = {"agent": agent, "jwk": jwk}
    return pins


def read_policy_bounded(path: Path) -> bytes:
    """Read at most MAX_POLICY_BYTES + 1 bytes of a policy file.

    The +1 byte lets the verifier distinguish exactly-at-cap from over-cap.
    This is THE read: every downstream consumer of the file — verifier,
    parser, hasher — must operate on the returned snapshot, never on a
    fresh read of the path.
    """
    with open(path, "rb") as handle:
        return handle.read(MAX_POLICY_BYTES + 1)


def _classify_policy_trailer(raw: bytes) -> Any:
    """(state, prefix, auth_value): ``ok`` / ``malformed`` / ``absent``.

    Same tri-state as message intake: an auth-like final line that fails
    exact framing is a byte-tamper signal, never "unsigned".
    """
    prefix, value = split_signed_message(raw)
    if value is not None:
        return "ok", prefix, value
    lines = raw.split(b"\n")
    while lines and lines[-1].strip(b" \t\r") == b"":
        lines.pop()
    if lines and lines[-1].startswith(b"auth:"):
        return "malformed", raw, None
    return "absent", raw, None


def _check_policy_claim(
    oacp_claim: Dict[str, Any], context: Optional[Dict[str, str]]
) -> Optional[str]:
    """Validate the signed policy context against the expected target.

    Returns a rejection reason, or None when the binding holds.
    """
    claim = oacp_claim.get(POLICY_OACP_CLAIM)
    if not isinstance(claim, dict):
        return "policy context claim must be a JSON object"
    if sorted(claim) != sorted(POLICY_CONTEXT_KEYS):
        return "policy context claim must carry exactly project/receiver/kind"
    for key in POLICY_CONTEXT_KEYS:
        if not isinstance(claim[key], str) or not claim[key]:
            return f"policy context {key} must be a non-empty string"
    if context is None:
        return (
            "policy target context unresolved — signed policies verify only "
            "at their canonical workspace location"
        )
    mismatched = sorted(
        key for key in POLICY_CONTEXT_KEYS if claim[key] != context[key]
    )
    if mismatched:
        return (
            "policy context mismatch on "
            + ", ".join(f"{key} (signed {claim[key]!r})" for key in mismatched)
        )
    return None


def verify_policy_bytes(
    raw: bytes,
    keystore_pins: Dict[str, Dict[str, Any]],
    *,
    receiver: Optional[str] = None,
    context: Optional[Dict[str, str]] = None,
    enrolled: bool = False,
) -> Dict[str, Any]:
    """Verify a policy file's raw bytes against local keystore pins.

    Returns the ``policy_auth`` block recorded into audit records:
    ``{status, signer_agent, signer_kid, reason, verified_at_utc}``. With
    *receiver* set, only a signature by that agent's own key verifies — a
    valid signature by a different local agent is ``invalid``
    (receiver-binding failure), not authorization. *context* is the
    expected ``{project, receiver, kind}`` target; the signed context must
    match it exactly. *enrolled* engages downgrade resistance: an enrolled
    target without a verifiable signature is ``invalid``, never
    ``unsigned``/``unsupported``.
    """
    result: Dict[str, Any] = {
        "status": POLICY_STATUS_UNSIGNED,
        "signer_agent": None,
        "signer_kid": None,
        "reason": None,
        "verified_at_utc": utc_now_iso(),
    }
    if len(raw) > MAX_POLICY_BYTES:
        result["status"] = POLICY_STATUS_INVALID
        result["reason"] = f"policy file exceeds size bound ({MAX_POLICY_BYTES} bytes)"
        return result
    state, prefix, auth_value = _classify_policy_trailer(raw)
    if state == "absent":
        if enrolled:
            result["status"] = POLICY_STATUS_INVALID
            result["reason"] = (
                "policy target is enrolled for signing but carries no auth "
                "trailer — signature stripping fails closed; re-sign with "
                "`oacp trust sign-policy` if the change was intended"
            )
        return result
    if state == "malformed":
        result["status"] = POLICY_STATUS_INVALID
        result["reason"] = (
            "auth framing: final line is auth-like but violates exact framing"
        )
        return result

    try:
        entries = decode_auth_value(auth_value)
        headers = [
            validate_protected_header(
                entry["protected"],
                expected_typ=POLICY_JWS_TYP,
                expected_domain=POLICY_SIG_DOMAIN,
                extra_oacp_keys=frozenset((POLICY_OACP_CLAIM,)),
            )
            for entry in entries
        ]
    except AuthFormatError as exc:
        result["status"] = POLICY_STATUS_INVALID
        result["reason"] = f"auth framing: {exc}"
        return result

    if not CRYPTO_AVAILABLE:
        if enrolled:
            result["status"] = POLICY_STATUS_INVALID
            result["reason"] = (
                "policy target is enrolled for signing but cryptography is "
                "unavailable to verify it — install 'oacp-cli[crypto]'"
            )
        else:
            result["status"] = POLICY_STATUS_UNSUPPORTED
            result["reason"] = (
                "cryptography unavailable — install 'oacp-cli[crypto]' to verify"
            )
        return result

    reasons: List[str] = []
    for entry, header in zip(entries, headers):
        kid = header["kid"]
        signer_name = _agent_name_from_urn(header["oacp"]["agent"])
        context_reason = _check_policy_claim(header["oacp"], context)
        if context_reason is not None:
            reasons.append(f"kid {kid[:12]}… {context_reason}")
            continue
        pin = keystore_pins.get(kid)
        if pin is None:
            reasons.append(f"kid {kid[:12]}… not in local keystore")
            continue
        if pin["agent"] != signer_name:
            reasons.append(f"kid {kid[:12]}… keystore agent mismatch")
            continue
        if receiver is not None and signer_name != receiver:
            reasons.append(
                f"signer {signer_name!r} is not the receiver {receiver!r}"
            )
            continue
        try:
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(
                b64url_decode(pin["jwk"]["x"])
            )
            public_key.verify(
                b64url_decode(entry["signature"]),
                signing_input(entry["protected"], prefix),
            )
        except (InvalidSignature, AuthFormatError, ValueError):
            reasons.append(f"kid {kid[:12]}… signature failed verification")
            continue
        result["status"] = POLICY_STATUS_VERIFIED
        result["signer_agent"] = signer_name
        result["signer_kid"] = kid
        result["reason"] = None
        return result

    result["status"] = POLICY_STATUS_INVALID
    result["reason"] = "; ".join(reasons) or "no usable signature"
    return result


def verify_policy_data(
    raw: bytes,
    oacp_home: Path,
    *,
    receiver: Optional[str] = None,
    context: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Verify an already-read policy snapshot; see `verify_policy_bytes`.

    Resolves enrollment for the context and consults the keystore only when
    the snapshot actually carries a trailer — an unsigned, unenrolled
    policy never touches key material.
    """
    enrolled = policy_enrolled(oacp_home, context)
    state, _prefix, _auth = _classify_policy_trailer(raw)
    if state == "absent":
        return verify_policy_bytes(
            raw, {}, receiver=receiver, context=context, enrolled=enrolled
        )
    pins = load_keystore_public_pins(oacp_home)
    return verify_policy_bytes(
        raw, pins, receiver=receiver, context=context, enrolled=enrolled
    )


def verify_policy_file(
    path: Path,
    oacp_home: Path,
    *,
    receiver: Optional[str] = None,
    context: Optional[Dict[str, str]] = None,
    kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify one policy file on disk; see `verify_policy_data`.

    When *context* is not supplied but *kind* and *receiver* are, the
    context is derived from the file's canonical workspace location.
    NOTE: callers that go on to parse the policy must not use this + a
    separate read — use `load_authorized_policy`, which verifies and
    parses one snapshot.
    """
    if context is None and kind is not None and receiver is not None:
        context = derive_policy_context(
            Path(path), oacp_home, receiver=receiver, kind=kind
        )
    raw = read_policy_bounded(Path(path))
    return verify_policy_data(raw, oacp_home, receiver=receiver, context=context)


def require_policy_authorized(policy_auth: Dict[str, Any], path: Path) -> None:
    """Fail-closed guard: raise on ``invalid`` (tamper), pass otherwise."""
    if policy_auth.get("status") == POLICY_STATUS_INVALID:
        raise PolicyAuthError(
            f"policy file {path} failed signature verification "
            f"({policy_auth.get('reason')}) — refusing to load; re-sign it "
            "with `oacp trust sign-policy` if the change was intended"
        )


def load_authorized_policy(
    path: Path,
    oacp_home: Path,
    *,
    receiver: str,
    kind: str,
    project: Optional[str] = None,
    on_invalid: str = "raise",
) -> Tuple[Dict[str, Any], Dict[str, Any], bytes]:
    """THE authorized policy read path: one snapshot, verified then parsed.

    Reads the file once (bounded), verifies those bytes (receiver binding,
    context binding, enrollment downgrade resistance), and parses the
    policy mapping from the SAME bytes — the ``auth`` trailer key is
    stripped from the parsed mapping (it is authorization metadata, not
    policy content). Returns ``(policy, policy_auth, raw)``.

    ``on_invalid="raise"`` (default) fails closed with `PolicyAuthError`;
    ``on_invalid="return"`` hands the caller the invalid ``policy_auth``
    to record (the autonomy gate pauses-with-record rather than erroring).
    """
    if on_invalid not in ("raise", "return"):
        raise ValueError(f"unknown on_invalid mode: {on_invalid!r}")
    path = Path(path)
    context = (
        policy_context(project, receiver, kind)
        if project is not None
        else derive_policy_context(path, oacp_home, receiver=receiver, kind=kind)
    )
    raw = read_policy_bounded(path)
    policy_auth = verify_policy_data(
        raw, oacp_home, receiver=receiver, context=context
    )
    if policy_auth["status"] == POLICY_STATUS_INVALID and on_invalid == "raise":
        require_policy_authorized(policy_auth, path)

    import yaml  # type: ignore

    try:
        loaded = yaml.safe_load(raw.decode("utf-8"))
    except Exception as exc:
        raise PolicyAuthError(f"cannot parse policy file {path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise PolicyAuthError(f"policy file {path} must be a YAML mapping")
    loaded.pop("auth", None)
    return loaded, policy_auth, raw


def sign_policy_file(
    path: Path,
    signers: Sequence[FileKeySigner],
    *,
    context: Dict[str, str],
) -> Dict[str, Any]:
    """Sign (or re-sign) one policy file in place, atomically.

    An existing trailer is stripped and the current content signed — policy
    files are edited over their lifetime, so re-signing is the normal flow.
    A missing final newline is normalized before signing (the trailer needs
    a complete final line to attach to). *context* is the signed
    ``{project, receiver, kind}`` target binding; enrollment recording is
    the caller's step (`record_policy_enrollment`) so that multi-file
    signing can enroll only after every file round-trips.
    """
    context = policy_context(
        context["project"], context["receiver"], context["kind"]
    )
    path = Path(path)
    raw = path.read_bytes()
    prefix, existing = split_signed_message(raw)
    payload = prefix if existing is not None else raw
    if not payload.endswith(b"\n"):
        payload += b"\n"
    auth_value = sign_payload(
        payload,
        signers,
        typ=POLICY_JWS_TYP,
        domain=POLICY_SIG_DOMAIN,
        extra_oacp={POLICY_OACP_CLAIM: context},
    )
    signed = payload + render_auth_line(auth_value).encode("ascii")

    mode = path.stat().st_mode
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".sign.tmp",
            delete=False,
        ) as handle:
            handle.write(signed)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    return {
        "path": str(path),
        "resigned": existing is not None,
        "context": context,
        "signer_kids": [signer.kid for signer in signers],
    }
