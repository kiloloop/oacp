#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""message_verify.py — OACP receiver verify-before-parse (warn mode).

Implements the receiver half of the v0.4.0 message-signing design
("raw-prefix detached-JWS auth trailer v1"):

- **Verify before parse**: the auth trailer is extracted from the raw bytes
  with the strict last-physical-line rule, the bounded auth container and
  protected headers are structurally validated, and EdDSA verification runs
  over the exact raw prefix bytes — all before the payload YAML is parsed.
  Only a cryptographically verified prefix is then parsed for the
  ``from:`` identity cross-check.
- **Receiver-local pins only**: keys resolve via ``kid`` against the
  receiver's own ``allowed_signers.yaml`` pins. No network at verify time,
  ever. The pin-file reader here is the canonical trust-file reader
  (format v1, with reserved ``domain``/``instance`` columns from day one —
  see ``docs/protocol/message_signing.md``) and stays
  the single swap point behind `load_allowed_signers`. Catalog management,
  import, and drift detection live in ``trust_root.py`` and never run at
  verify time.
- **Warn mode records identity and grants no authority**: every outcome —
  unsigned / signed-unknown-kid / signed-verified / signed-INVALID — produces
  an annotation and a ``message_auth`` audit block, never a rejection.
  Enforce/quarantine-as-rejection is v0.4.1; the seams exist, none activate.
- **No-clobber quarantine**: byte-tamper cases (a present signature that
  fails verification) write an evidence copy aside into ``dead_letter/``
  with exclusive-create semantics. The original message file is never
  touched, moved, or overwritten.

The ``message_auth`` block mirrors the schema-v2 ``human_outcome`` pattern:
a recorder (`attach_message_auth`) writes it into an existing autonomy audit
record under a lock, refusing silent overwrites. Audit schema stays v2 — the
block is additive.

Verification requires the optional ``cryptography`` dependency
(``pip install 'oacp-cli[crypto]'``); without it, signed messages annotate
as ``unsupported`` and processing continues (warn mode).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_scripts_dir))

from _oacp_constants import locked_audit, utc_now_iso  # noqa: E402
from message_signing import (  # noqa: E402
    CRYPTO_AVAILABLE,
    JWS_ALG,
    SIG_SCHEME,
    AuthFormatError,
    b64url_decode,
    decode_auth_value,
    jwk_thumbprint,
    signing_input,
    split_signed_message,
    validate_kid,
    validate_protected_header,
    validate_public_ed25519_jwk,
)

if CRYPTO_AVAILABLE:  # pragma: no cover - trivial import guard
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

# Pinned message_auth status enum (v0.4.0 warn subset; replayed/id_collision
# join with the v0.4.1 receipt ledger).
STATUS_UNSIGNED = "unsigned"
STATUS_VERIFIED = "verified"
STATUS_INVALID = "invalid"
STATUS_UNTRUSTED = "untrusted"
STATUS_REVOKED = "revoked"
STATUS_UNSUPPORTED = "unsupported"

# Ruled warn-mode annotations per status.
ANNOTATIONS = {
    STATUS_UNSIGNED: "unsigned",
    STATUS_VERIFIED: "signed-verified",
    STATUS_UNTRUSTED: "signed-unknown-kid",
    STATUS_INVALID: "signed-INVALID",
    STATUS_REVOKED: "signed-REVOKED",
    STATUS_UNSUPPORTED: "signed-unverifiable (crypto unavailable)",
}

VERIFY_MODES = ("off", "warn")
TRUST_FILE_VERSION = 1
PIN_STATUS_ACTIVE = "active"
PIN_STATUS_REVOKED = "revoked"
DEAD_LETTER_DIRNAME = "dead_letter"
ALLOWED_SIGNERS_RELPATH = Path("trust") / "allowed_signers.yaml"

MAX_MESSAGE_BYTES = 1024 * 1024  # bounded read before any processing

# Tri-state trailer framing: a present-but-malformed
# final auth trailer is a byte-tamper signal, distinct from a message that
# never carried authentication.
TRAILER_ABSENT = "absent"
TRAILER_OK = "ok"
TRAILER_MALFORMED = "malformed"


class TrustRootError(ValueError):
    """Raised when the receiver pin file exists but cannot be used."""


def read_message_bounded(path: Path) -> bytes:
    """Read at most MAX_MESSAGE_BYTES + 1 bytes from an untrusted message file.

    The cap is enforced at the file handle — an oversized inbox artifact is
    never fully allocated before being labeled invalid. The +1 byte lets
    verify_message distinguish exactly-at-cap from over-cap.
    """
    with open(path, "rb") as handle:
        return handle.read(MAX_MESSAGE_BYTES + 1)


def classify_auth_trailer(raw: bytes) -> Tuple[str, bytes, Optional[str]]:
    """Classify the message tail: (state, prefix_bytes, auth_value).

    ``ok`` — strict last-physical-line trailer; prefix is the signed bytes.
    ``malformed`` — the file ends in an auth-like column-0 line that fails
    exact framing (missing final LF, trailing whitespace, CRLF, oversized
    value, trailing blank lines): a byte-different signed artifact, never
    reported as unsigned.
    ``absent`` — no trailer and nothing auth-like at the end of the file.
    """
    prefix, value = split_signed_message(raw)
    if value is not None:
        return TRAILER_OK, prefix, value
    lines = raw.split(b"\n")
    while lines and lines[-1].strip(b" \t\r") == b"":
        lines.pop()
    if lines and lines[-1].startswith(b"auth:"):
        return TRAILER_MALFORMED, raw, None
    return TRAILER_ABSENT, raw, None


# ---------------------------------------------------------------------------
# Receiver config knob + pin-file reader (swappable)
# ---------------------------------------------------------------------------

def load_verify_mode(config_path: Path) -> str:
    """Read `signing.verify_mode` from a receiver config; default ``off``.

    Receivers without the knob (or without the file) behave exactly as
    today. ``enforce`` is not activated in v0.4.0 — a receiver opting in
    early degrades to ``warn`` (identity recorded, nothing rejected);
    any other value degrades to ``off``.
    """
    config_path = Path(config_path)
    if not config_path.is_file():
        return "off"
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return "off"
    if not isinstance(loaded, dict):
        return "off"
    signing = loaded.get("signing")
    if not isinstance(signing, dict):
        return "off"
    mode = str(signing.get("verify_mode", "off")).strip().lower()
    if mode in VERIFY_MODES:
        return mode
    if mode == "enforce":
        return "warn"
    return "off"


def load_allowed_signers(pins_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load receiver-local pins as a ``kid -> entry`` map.

    Canonical trust-file reader (format v1 — keep this function the
    single swap point):

    ```yaml
    version: 1
    signers:
      - agent: iris
        domain: <trust-domain uuid>      # reserved, unused in v0.4.0
        instance: <instance uuid>        # reserved, unused in v0.4.0
        kid: <RFC 7638 thumbprint>
        jwk: {kty: OKP, crv: Ed25519, x: <base64url>}
        status: active                   # active | revoked
    ```

    This reader is the verify boundary, so it enforces the locked profile
    itself (doctor's drift check is advisory, never the enforcement):
    ``kid`` must be exactly the RFC 7638 thumbprint of the entry's strict
    public-only Ed25519 ``jwk`` (exactly kty/crv/x, 32-byte x). Any
    private component makes the whole file unusable. The reserved
    ``domain``/``instance`` columns are shape-checked (string or null) but
    carry no semantics in v0.4.0. A missing file returns no pins. A
    present-but-unreadable file raises `TrustRootError` — warn-mode
    callers record the reason and continue.
    """
    pins_path = Path(pins_path)
    if not pins_path.is_file():
        return {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(pins_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TrustRootError(f"cannot read pin file {pins_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TrustRootError(f"pin file {pins_path} must be a YAML mapping")
    version = loaded.get("version")
    if version not in (TRUST_FILE_VERSION, str(TRUST_FILE_VERSION)):
        raise TrustRootError(
            f"pin file {pins_path} has unsupported version {version!r}"
        )
    signers = loaded.get("signers")
    if signers is None:
        return {}
    if not isinstance(signers, list):
        raise TrustRootError(f"pin file {pins_path}: signers must be a list")

    pins: Dict[str, Dict[str, Any]] = {}
    for index, entry in enumerate(signers):
        if not isinstance(entry, dict):
            raise TrustRootError(f"pin file {pins_path}: signers[{index}] must be a mapping")
        kid = entry.get("kid")
        agent = entry.get("agent")
        jwk = entry.get("jwk")
        status = str(entry.get("status", PIN_STATUS_ACTIVE)).strip().lower()
        if not isinstance(kid, str) or not kid:
            raise TrustRootError(f"pin file {pins_path}: signers[{index}] missing kid")
        try:
            validate_kid(kid)
        except AuthFormatError as exc:
            raise TrustRootError(
                f"pin file {pins_path}: signers[{index}]: {exc}"
            ) from exc
        if not isinstance(agent, str) or not agent:
            raise TrustRootError(f"pin file {pins_path}: signers[{index}] missing agent")
        if status not in (PIN_STATUS_ACTIVE, PIN_STATUS_REVOKED):
            raise TrustRootError(
                f"pin file {pins_path}: signers[{index}] has unknown status {status!r}"
            )
        # Active pins must carry usable key material; a revoked pin may
        # drop its jwk entirely, but if one is still present it must
        # satisfy the same locked profile so a later manual reactivation
        # cannot smuggle in bad material.
        if jwk is not None or status == PIN_STATUS_ACTIVE:
            try:
                jwk = validate_public_ed25519_jwk(jwk)
            except AuthFormatError as exc:
                raise TrustRootError(
                    f"pin file {pins_path}: signers[{index}]: {exc}"
                ) from exc
            if jwk_thumbprint(jwk) != kid:
                raise TrustRootError(
                    f"pin file {pins_path}: signers[{index}]: kid is not the "
                    "RFC 7638 thumbprint of its jwk"
                )
        if kid in pins:
            raise TrustRootError(f"pin file {pins_path}: duplicate kid {kid!r}")
        for column in ("domain", "instance"):
            value = entry.get(column)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise TrustRootError(
                    f"pin file {pins_path}: signers[{index}]: reserved column "
                    f"{column!r} must be a non-empty string or null"
                )
        pins[kid] = {
            "agent": agent,
            "domain": entry.get("domain"),
            "instance": entry.get("instance"),
            "jwk": jwk,
            "status": status,
        }
    return pins


# ---------------------------------------------------------------------------
# Verify-before-parse
# ---------------------------------------------------------------------------

def _parse_prefix_from(prefix: bytes) -> Optional[str]:
    """Parse the verified prefix and return its `from` value.

    Called only after cryptographic verification succeeded — the parsed
    bytes are exactly the bytes that were verified.
    """
    try:
        from validate_message import _load_message

        data = _load_message(prefix.decode("utf-8"))
    except Exception:
        return None
    value = data.get("from")
    return str(value).strip() if isinstance(value, str) else None


def _agent_name_from_urn(agent_urn_value: str) -> str:
    return agent_urn_value.rsplit(":", 1)[-1]


def verify_message(
    raw: bytes,
    pins: Dict[str, Dict[str, Any]],
    *,
    trust_source: Optional[str] = None,
    trust_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify raw message bytes against receiver-local pins (warn mode).

    Returns the ``message_auth`` block. Never raises for message-shaped
    problems and never rejects — the caller annotates and continues.
    """
    result: Dict[str, Any] = {
        "status": STATUS_UNSIGNED,
        "alg": None,
        "scheme": None,
        "claimed_sender": None,
        "verified_signer": None,
        "verified_instance": None,
        "kid": None,
        "payload_sha256": None,
        "trust_source": trust_source,
        "signatures_checked": [],
        "reason": trust_error,
        "verified_at_utc": utc_now_iso(),
    }
    if len(raw) > MAX_MESSAGE_BYTES:
        result["status"] = STATUS_INVALID
        result["reason"] = "message exceeds size bound"
        return result

    state, prefix, auth_value = classify_auth_trailer(raw)
    # For absent/malformed trailers the digest covers the whole raw artifact
    # (there is no defined signed prefix) — still forensically useful.
    result["payload_sha256"] = hashlib.sha256(prefix).hexdigest()
    if state == TRAILER_ABSENT:
        return result
    if state == TRAILER_MALFORMED:
        # A byte-tampered trailer must never be indistinguishable from a
        # never-signed message — and stays eligible for
        # evidence quarantine.
        result["status"] = STATUS_INVALID
        result["reason"] = (
            "auth framing: final line is auth-like but violates exact "
            'framing (auth: "<base64url>" + exactly one LF, bounded size)'
        )
        return result

    result["alg"] = JWS_ALG
    result["scheme"] = SIG_SCHEME
    try:
        entries = decode_auth_value(auth_value)
        headers = [validate_protected_header(e["protected"]) for e in entries]
    except AuthFormatError as exc:
        result["status"] = STATUS_INVALID
        result["reason"] = f"auth framing: {exc}"
        return result

    if not CRYPTO_AVAILABLE:
        result["status"] = STATUS_UNSUPPORTED
        result["reason"] = (
            "cryptography unavailable — install 'oacp-cli[crypto]' to verify"
        )
        result["signatures_checked"] = [
            {"kid": h["kid"], "agent": h["oacp"]["agent"], "outcome": "unchecked"}
            for h in headers
        ]
        return result

    saw_bad_signature = False
    saw_revoked = False
    # Every cryptographically verified entry is retained: OR acceptance must
    # be order-independent — a co-signature by another
    # agent must never mask the sender's own valid signature by position.
    verified: List[Tuple[Dict[str, str], Dict[str, Any]]] = []
    for entry, header in zip(entries, headers):
        kid = header["kid"]
        checked = {"kid": kid, "agent": header["oacp"]["agent"], "outcome": ""}
        result["signatures_checked"].append(checked)
        pin = pins.get(kid)
        if pin is None:
            checked["outcome"] = "unknown_kid"
            continue
        if pin["status"] == PIN_STATUS_REVOKED:
            # Revoked keys reject unseen messages regardless of any
            # self-declared date (backdating defense) — no crypto attempted.
            checked["outcome"] = "revoked"
            saw_revoked = True
            continue
        if pin["agent"] != _agent_name_from_urn(header["oacp"]["agent"]):
            checked["outcome"] = "pin_agent_mismatch"
            saw_bad_signature = True
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
            checked["outcome"] = "bad_signature"
            saw_bad_signature = True
            continue
        checked["outcome"] = "verified"
        verified.append((checked, header))

    if verified:
        # Parse the exact verified prefix ONCE; accept any verified header
        # whose agent matches the claimed sender, regardless of position.
        claimed = _parse_prefix_from(prefix)
        result["claimed_sender"] = claimed
        selected_header: Optional[Dict[str, Any]] = None
        for checked, header in verified:
            name = _agent_name_from_urn(header["oacp"]["agent"])
            if claimed is not None and name == claimed:
                if selected_header is None:
                    selected_header = header
            else:
                checked["outcome"] = "identity_mismatch"
        if selected_header is None:
            names = sorted(
                {_agent_name_from_urn(h["oacp"]["agent"]) for _, h in verified}
            )
            result["status"] = STATUS_INVALID
            result["reason"] = (
                f"signer identity mismatch: verified {', '.join(names)}, "
                f"message claims {claimed!r}"
            )
            return result
        result["status"] = STATUS_VERIFIED
        result["verified_signer"] = selected_header["oacp"]["agent"]
        result["verified_instance"] = selected_header["oacp"]["instance"]
        result["kid"] = selected_header["kid"]
        result["reason"] = None
        return result

    if saw_bad_signature:
        result["status"] = STATUS_INVALID
        result["reason"] = "signature present but failed verification"
    elif saw_revoked:
        result["status"] = STATUS_REVOKED
        result["reason"] = "all matching pins are revoked"
    else:
        result["status"] = STATUS_UNTRUSTED
        reason = "no signature matches a receiver-pinned kid"
        if trust_error:
            # With no usable pins every signed message lands here; the
            # load failure is the actual finding and must stay visible.
            reason = f"{reason} (trust root unusable: {trust_error})"
        result["reason"] = reason
    return result


def annotate(message_auth: Dict[str, Any]) -> str:
    """One-line warn annotation for logs / inbox surfaces."""
    status = message_auth.get("status", STATUS_UNSIGNED)
    label = ANNOTATIONS.get(status, status)
    parts = [f"[oacp-auth] {label}"]
    if message_auth.get("verified_signer"):
        parts.append(f"signer={message_auth['verified_signer']}")
    if message_auth.get("kid"):
        parts.append(f"kid={message_auth['kid'][:12]}…")
    if message_auth.get("reason"):
        parts.append(f"reason: {message_auth['reason']}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# No-clobber quarantine (write-aside; original never touched)
# ---------------------------------------------------------------------------

def quarantine_write_aside(
    raw: bytes, message_path: Path, dead_letter_dir: Path
) -> Path:
    """Write an evidence copy of *raw* into the dead-letter directory.

    Exclusive-create with a digest+timestamp+counter name — an existing
    quarantine file is never overwritten, and the original message file is
    never moved or modified (warn mode continues processing it).
    """
    dead_letter_dir = Path(dead_letter_dir)
    dead_letter_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()[:8]
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    base = f"{Path(message_path).name}.{digest}.{stamp}"
    for counter in range(1000):
        suffix = "" if counter == 0 else f".{counter}"
        candidate = dead_letter_dir / f"{base}{suffix}"
        try:
            fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        return candidate
    raise OSError(f"could not allocate a unique quarantine name for {base}")


# ---------------------------------------------------------------------------
# message_auth audit recording (mirrors the schema-v2 outcome-block pattern)
# ---------------------------------------------------------------------------

def attach_message_auth(
    audit_path: Path, message_auth: Dict[str, Any], *, replace: bool = False
) -> Dict[str, Any]:
    """Record a message_auth block into an existing autonomy audit record.

    Additive to schema v2 (like ``human_outcome``): locked read-modify-write,
    atomic replace, refuses to overwrite a recorded block unless *replace*.
    Warn mode: the block records identity and grants no authority — gates
    consume it as telemetry only.

    Concurrency: serialization uses the ONE
    shared stable audit lock (`_oacp_constants.locked_audit`) that every
    audit read-modify-write path holds — including the human-outcome
    recorder — so concurrent writers cannot drop each other's blocks.
    Locking the audit file itself is unsound because os.replace swaps the
    inode under waiters. The audit content is read only after the lock is
    held. The small ``.lock`` sibling persists; it carries no data.
    """
    import yaml  # type: ignore

    audit_path = Path(audit_path)
    with locked_audit(audit_path):
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        if not isinstance(audit, dict):
            raise ValueError(f"{audit_path} is not a YAML mapping")
        result = audit.get("result")
        if not isinstance(result, dict):
            raise ValueError("audit.result must be a mapping")
        existing = result.get("message_auth")
        if not replace and isinstance(existing, dict) and existing.get("status"):
            raise ValueError(
                "audit already has a recorded message_auth block; use replace"
            )
        updated = copy.deepcopy(audit)
        updated["result"]["message_auth"] = copy.deepcopy(message_auth)

        content = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True)
        mode = audit_path.stat().st_mode
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(audit_path.parent),
                prefix=f".{audit_path.name}.",
                suffix=".ma.tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.chmod(temp_path, mode)
            os.replace(temp_path, audit_path)
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        return updated


# ---------------------------------------------------------------------------
# CLI: manual verify (the warn annotation path's entry point)
# ---------------------------------------------------------------------------

def _resolve_pins_path(args: argparse.Namespace) -> Optional[Path]:
    if args.pins:
        return Path(args.pins)
    if args.project and args.receiver:
        if args.oacp_dir:
            from _oacp_env import resolve_oacp_home

            home = resolve_oacp_home(args.oacp_dir)
        else:
            from _oacp_env import resolve_oacp_home

            home = resolve_oacp_home()
        return (
            home / "projects" / args.project / "agents" / args.receiver
            / ALLOWED_SIGNERS_RELPATH
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a message file's auth trailer against receiver-local pins "
            "(warn mode: reports, never rejects)."
        )
    )
    parser.add_argument("message_file", help="Path to the message YAML file")
    parser.add_argument("--pins", default=None, help="Path to allowed_signers.yaml")
    parser.add_argument("--project", default=None, help="Project name (with --receiver)")
    parser.add_argument("--receiver", default=None, help="Receiver agent name")
    parser.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    parser.add_argument(
        "--quarantine",
        action="store_true",
        help="Write-aside an evidence copy to dead_letter/ when INVALID",
    )
    parser.add_argument(
        "--attach-audit",
        default=None,
        help="Record the message_auth block into this autonomy audit YAML",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    message_path = Path(args.message_file)
    if not message_path.is_file():
        print(f"ERROR: message file does not exist: {message_path}", file=sys.stderr)
        return 2
    raw = read_message_bounded(message_path)

    pins_path = _resolve_pins_path(args)
    pins: Dict[str, Dict[str, Any]] = {}
    trust_error: Optional[str] = None
    if pins_path is not None:
        try:
            pins = load_allowed_signers(pins_path)
        except TrustRootError as exc:
            trust_error = str(exc)

    message_auth = verify_message(
        raw,
        pins,
        trust_source=str(pins_path) if pins_path else None,
        trust_error=trust_error,
    )

    if args.quarantine and message_auth["status"] == STATUS_INVALID:
        dead_letter_dir = message_path.parent.parent / DEAD_LETTER_DIRNAME
        quarantined = quarantine_write_aside(raw, message_path, dead_letter_dir)
        message_auth["quarantine_copy"] = str(quarantined)

    if args.attach_audit:
        try:
            attach_message_auth(Path(args.attach_audit), message_auth)
        except (OSError, ValueError) as exc:
            print(f"ERROR: attach-audit failed: {exc}", file=sys.stderr)
            return 2

    if args.json_output:
        print(json.dumps(message_auth, indent=2))
    else:
        print(annotate(message_auth))

    if message_auth["status"] in (STATUS_UNSIGNED, STATUS_VERIFIED):
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
