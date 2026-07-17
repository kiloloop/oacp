#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""message_signing.py — OACP message signing: sender core + shared wire helpers.

Implements the sender half of the v0.4.0 message-signing design
("raw-prefix detached-JWS auth trailer v1"):

- Transport covenant: byte-different means different message. The signed
  payload is the exact raw UTF-8 bytes of the rendered message file up to the
  trailer. No canonicalization anywhere; nothing is ever re-rendered.
- Wire framing: a signed message carries exactly one final physical line
  ``auth: "<base64url(detached General JWS JSON)>"`` followed by one LF.
  The decoded JSON contains exactly ``signatures``, an array of 1-8
  ``{protected, signature}`` objects (RFC 7515 detached content: no
  ``payload`` member, no unprotected headers). Trailer encodings are
  canonical-only: the auth value and each signature member must be the
  canonical unpadded base64url spelling, and the container JSON the
  signer's compact sorted-key emit — one signed message, one wire
  spelling (encoding aliases are rejected, not re-encoded).
- Each JWS signing input is ``BASE64URL(protected) || '.' ||
  BASE64URL(raw-prefix-bytes)`` per RFC 7515 §5.1.
- Locked JOSE profile: EdDSA (Ed25519) only, ``crit: ["oacp"]``, no
  ``jku``/``jwk``/``x5*``. Messages name keys via ``kid`` (RFC 7638 JWK
  thumbprint) and never supply key material.
- Identity: logical agent URN plus per-machine instance URN in the protected
  header. Keys are resolved receiver-side against local pins only.

Receiver verify-before-parse lives in ``message_verify.py``; it builds on the
shared framing helpers here (`split_signed_message`, `decode_auth_value`,
`validate_protected_header`) but the crypto-verify path lives there.

Signing requires the optional ``cryptography`` dependency
(``pip install 'oacp-cli[crypto]'``). Framing helpers are stdlib-only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from _oacp_constants import AGENT_RE, utc_now_iso

try:  # pragma: no cover - trivial import guard
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover
    CRYPTO_AVAILABLE = False

SIG_SCHEME = "raw-prefix-v1"
SIG_DOMAIN = "urn:oacp:message:v1"
JWS_TYP = "oacp-message+yaml"
JWS_ALG = "EdDSA"
CRIT_PARAMS = ["oacp"]
MAX_SIGNATURES = 8
# Bounded before any decode: 8 signatures of (~1k header + 86-char sig) fit
# comfortably; anything larger is rejected unread.
MAX_AUTH_VALUE_CHARS = 16384
MAX_PROTECTED_CHARS = 4096

AGENT_URN_PREFIX = "urn:oacp:agent:"
INSTANCE_URN_PREFIX = "urn:uuid:"

PROTECTED_HEADER_KEYS = {"alg", "typ", "kid", "crit", "oacp"}
OACP_HEADER_KEYS = {"scheme", "domain", "agent", "instance"}
FORBIDDEN_HEADER_PARAMS = ("jku", "jwk", "x5u", "x5c", "x5t", "x5t#S256")

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# RFC 7638 SHA-256 thumbprint: 32 bytes -> exactly 43 unpadded base64url chars.
_KID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_AUTH_LINE_RE = re.compile(rb'^auth: "([A-Za-z0-9_-]+)"$')

KEYS_DIRNAME = "keys"
TRUST_DOMAIN_FILENAME = "trust_domain"
INSTANCE_ID_FILENAME = "instance_id"
PUBLIC_STUB_SUFFIX = ".pub.json"


class AuthFormatError(ValueError):
    """Raised when signed-message framing or the JOSE profile is violated."""


class SigningUnavailableError(RuntimeError):
    """Raised when signing is requested but keys or crypto are unavailable."""


# ---------------------------------------------------------------------------
# base64url (unpadded) and JSON helpers
# ---------------------------------------------------------------------------

def b64url_encode(data: bytes) -> str:
    """Unpadded base64url per RFC 7515 §2."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict unpadded base64url decode; rejects padding and foreign chars."""
    if not isinstance(text, str) or not text or not _B64URL_RE.fullmatch(text):
        raise AuthFormatError("invalid base64url value")
    padding = -len(text) % 4
    if padding == 3:
        raise AuthFormatError("invalid base64url length")
    try:
        return base64.urlsafe_b64decode(text + "=" * padding)
    except (ValueError, TypeError) as exc:
        raise AuthFormatError(f"base64url decode failed: {exc}") from exc


def b64url_decode_canonical(text: str, what: str) -> bytes:
    """Strict decode requiring the canonical spelling — the single choke
    point for base64url encoding aliases.

    A value whose final character carries non-zero unused pad bits decodes
    to the same bytes as the canonical spelling; requiring the decode to
    re-encode to the identical string leaves each byte string exactly one
    accepted encoding."""
    raw = b64url_decode(text)
    if b64url_encode(raw) != text:
        raise AuthFormatError(
            f"{what} is not the canonical base64url encoding of its bytes "
            "— non-zero unused pad bits give the same bytes a second "
            "spelling"
        )
    return raw


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthFormatError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _strict_json_loads(raw: bytes, what: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except AuthFormatError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthFormatError(f"{what} is not valid JSON: {exc}") from exc


def _compact_json(data: Dict[str, Any]) -> str:
    """Deterministic emit for signer-produced JSON (sorted keys, no spaces).

    This is emit determinism for our own output, not payload canonicalization
    — verification never re-serializes anything.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# JWK / kid (RFC 7638 thumbprint) and identity URNs
# ---------------------------------------------------------------------------

def ed25519_public_jwk(public_bytes: bytes) -> Dict[str, str]:
    """Ed25519 public JWK per RFC 8037 §2."""
    if len(public_bytes) != 32:
        raise AuthFormatError("Ed25519 public key must be 32 bytes")
    return {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(public_bytes)}


def jwk_thumbprint(jwk: Dict[str, str]) -> str:
    """RFC 7638 JWK thumbprint (SHA-256) for an OKP key: crv, kty, x only."""
    required = {"crv": jwk.get("crv"), "kty": jwk.get("kty"), "x": jwk.get("x")}
    if not all(isinstance(v, str) and v for v in required.values()):
        raise AuthFormatError("JWK must carry non-empty crv, kty, and x")
    digest_input = json.dumps(required, sort_keys=True, separators=(",", ":"))
    return b64url_encode(hashlib.sha256(digest_input.encode("utf-8")).digest())


def validate_public_ed25519_jwk(jwk: Any) -> Dict[str, str]:
    """Strict public-only Ed25519 JWK check for trust-file material.

    The locked profile shape, enforced wherever a JWK enters or leaves a
    trust surface (receiver pins, project catalog, public stubs): exactly
    the ``kty``/``crv``/``x`` members, ``OKP``/``Ed25519``, and ``x`` the
    canonical unpadded base64url encoding of exactly 32 bytes (the decode
    must re-encode to the identical string, so one key has exactly one
    spelling and one RFC 7638 thumbprint). Anything else
    — including any private component such as ``d`` — is rejected. The
    thumbprint alone is not proof of usable key material: it hashes the
    ``x`` string without decoding it. Returns the normalized 3-key JWK.
    """
    if not isinstance(jwk, dict):
        raise AuthFormatError("jwk must be a mapping")
    extra = set(jwk) - {"kty", "crv", "x"}
    if extra:
        raise AuthFormatError(
            f"jwk carries unexpected members {sorted(extra)} — only kty/crv/x "
            "are allowed (a private 'd' component must never enter a trust "
            "file)"
        )
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise AuthFormatError("jwk must be an Ed25519 OKP public key")
    raw = b64url_decode_canonical(jwk.get("x"), "jwk x")
    if len(raw) != 32:
        raise AuthFormatError(
            f"jwk x must decode to exactly 32 bytes (got {len(raw)})"
        )
    return {"kty": "OKP", "crv": "Ed25519", "x": jwk["x"]}


def agent_urn(trust_domain: str, agent: str) -> str:
    return f"{AGENT_URN_PREFIX}{trust_domain}:{agent}"


def instance_urn(instance_id: str) -> str:
    return f"{INSTANCE_URN_PREFIX}{instance_id}"


def validate_agent_urn(value: Any) -> None:
    """Strict shape: urn:oacp:agent:<trust-domain-uuid>:<agent-name>."""
    if not isinstance(value, str) or not value.startswith(AGENT_URN_PREFIX):
        raise AuthFormatError(f"agent identity must start with {AGENT_URN_PREFIX!r}")
    suffix = value[len(AGENT_URN_PREFIX):]
    domain, sep, agent = suffix.rpartition(":")
    if not sep or not domain or not agent:
        raise AuthFormatError(
            "agent identity must be urn:oacp:agent:<trust-domain-uuid>:<agent>"
        )
    try:
        uuid.UUID(domain)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AuthFormatError(
            f"agent identity trust domain must be a UUID, got {domain!r}"
        ) from exc
    if not AGENT_RE.fullmatch(agent):
        raise AuthFormatError(f"agent identity name must match {AGENT_RE.pattern}")


def validate_instance_urn(value: Any) -> None:
    """Strict shape: urn:uuid:<machine-instance-uuid>."""
    if not isinstance(value, str) or not value.startswith(INSTANCE_URN_PREFIX):
        raise AuthFormatError(
            f"instance identity must start with {INSTANCE_URN_PREFIX!r}"
        )
    suffix = value[len(INSTANCE_URN_PREFIX):]
    try:
        uuid.UUID(suffix)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AuthFormatError(
            f"instance identity must be urn:uuid:<uuid>, got suffix {suffix!r}"
        ) from exc


def validate_kid(value: Any) -> None:
    """Strict shape: RFC 7638 SHA-256 thumbprint (43 unpadded base64url chars).

    Canonical spelling required: a 43-char value carries 2 unused pad bits,
    so every thumbprint has alias spellings decoding to the same bytes. Kid
    comparisons everywhere are exact-string (pin lookup, catalog keys), so
    an accepted alias would be a second identity for the same key — one key,
    one spelling, one thumbprint.
    """
    if not isinstance(value, str) or not _KID_RE.fullmatch(value):
        raise AuthFormatError(
            "kid must be an RFC 7638 SHA-256 JWK thumbprint "
            "(43 unpadded base64url characters)"
        )
    b64url_decode_canonical(value, "kid")


# ---------------------------------------------------------------------------
# Protected header + signing input (the PAE preimage)
# ---------------------------------------------------------------------------

def build_protected_header(kid: str, agent: str, instance: str) -> Dict[str, Any]:
    """Strict OACP protected header. `agent`/`instance` are full URNs."""
    return {
        "alg": JWS_ALG,
        "typ": JWS_TYP,
        "kid": kid,
        "crit": list(CRIT_PARAMS),
        "oacp": {
            "scheme": SIG_SCHEME,
            "domain": SIG_DOMAIN,
            "agent": agent,
            "instance": instance,
        },
    }


def encode_protected_header(header: Dict[str, Any]) -> str:
    return b64url_encode(_compact_json(header).encode("utf-8"))


def signing_input(protected_b64: str, payload: bytes) -> bytes:
    """RFC 7515 §5.1 signing input over the exact raw prefix bytes."""
    if not _B64URL_RE.fullmatch(protected_b64 or ""):
        raise AuthFormatError("protected header must be unpadded base64url")
    if not isinstance(payload, bytes) or not payload:
        raise AuthFormatError("payload must be non-empty bytes")
    return (
        protected_b64.encode("ascii")
        + b"."
        + b64url_encode(payload).encode("ascii")
    )


def validate_protected_header(protected_b64: str) -> Dict[str, Any]:
    """Decode + structurally validate one protected header (no crypto).

    Enforces the locked JOSE profile: EdDSA only, exact key set, crit:oacp,
    URN identity, no key-location/certificate parameters.
    """
    if not isinstance(protected_b64, str) or len(protected_b64) > MAX_PROTECTED_CHARS:
        raise AuthFormatError("protected header missing or oversized")
    header = _strict_json_loads(b64url_decode(protected_b64), "protected header")
    if not isinstance(header, dict):
        raise AuthFormatError("protected header must be a JSON object")

    for param in FORBIDDEN_HEADER_PARAMS:
        if param in header:
            raise AuthFormatError(f"forbidden JOSE parameter: {param!r}")
    unknown = sorted(set(header) - PROTECTED_HEADER_KEYS)
    if unknown:
        raise AuthFormatError(f"unknown protected header member(s): {', '.join(unknown)}")
    missing = sorted(PROTECTED_HEADER_KEYS - set(header))
    if missing:
        raise AuthFormatError(f"missing protected header member(s): {', '.join(missing)}")

    if header["alg"] != JWS_ALG:
        raise AuthFormatError(f"alg must be {JWS_ALG!r}")
    if header["typ"] != JWS_TYP:
        raise AuthFormatError(f"typ must be {JWS_TYP!r}")
    validate_kid(header["kid"])
    if header["crit"] != CRIT_PARAMS:
        raise AuthFormatError(f"crit must be exactly {CRIT_PARAMS!r}")

    oacp = header["oacp"]
    if not isinstance(oacp, dict):
        raise AuthFormatError("oacp header member must be a JSON object")
    unknown = sorted(set(oacp) - OACP_HEADER_KEYS)
    if unknown:
        raise AuthFormatError(f"unknown oacp header member(s): {', '.join(unknown)}")
    missing = sorted(OACP_HEADER_KEYS - set(oacp))
    if missing:
        raise AuthFormatError(f"missing oacp header member(s): {', '.join(missing)}")
    if oacp["scheme"] != SIG_SCHEME:
        raise AuthFormatError(f"unknown signing scheme: {oacp['scheme']!r}")
    if oacp["domain"] != SIG_DOMAIN:
        raise AuthFormatError(f"unknown signing domain: {oacp['domain']!r}")
    validate_agent_urn(oacp["agent"])
    validate_instance_urn(oacp["instance"])
    return header


# ---------------------------------------------------------------------------
# Trailer framing: emit, append-once, split
# ---------------------------------------------------------------------------

def render_auth_line(auth_value: str) -> str:
    if not _B64URL_RE.fullmatch(auth_value or ""):
        raise AuthFormatError("auth value must be unpadded base64url")
    if len(auth_value) > MAX_AUTH_VALUE_CHARS:
        raise AuthFormatError("auth value exceeds size bound")
    return f'auth: "{auth_value}"\n'


def append_auth_trailer(payload: bytes, auth_value: str) -> bytes:
    """Append the auth trailer exactly once to a rendered message.

    The payload must be the complete rendered message bytes ending in LF.
    Appending to an already-signed message is refused — the sender flow is
    render-unsigned once, append once, atomic write (never re-render).
    """
    if not isinstance(payload, bytes) or not payload:
        raise AuthFormatError("payload must be non-empty bytes")
    if not payload.endswith(b"\n"):
        raise AuthFormatError("payload must end with a newline before signing")
    if split_signed_message(payload)[1] is not None:
        raise AuthFormatError("payload already carries an auth trailer (append-once)")
    return payload + render_auth_line(auth_value).encode("ascii")


def split_signed_message(raw: bytes) -> Tuple[bytes, Optional[str]]:
    """Split raw message bytes at the strict last-physical-line auth trailer.

    Returns ``(prefix_bytes, auth_value)`` when the final physical line is a
    well-formed ``auth: "<base64url>"`` line followed by exactly one LF, else
    ``(raw, None)``. Only the last physical line is ever considered — an
    auth-lookalike anywhere earlier is payload content.
    """
    if not isinstance(raw, bytes):
        raise AuthFormatError("raw message must be bytes")
    if not raw.endswith(b"\n"):
        return raw, None
    body = raw[:-1]
    cut = body.rfind(b"\n")
    last_line = body[cut + 1:] if cut != -1 else body
    match = _AUTH_LINE_RE.fullmatch(last_line)
    if match is None:
        return raw, None
    auth_value = match.group(1).decode("ascii")
    if len(auth_value) > MAX_AUTH_VALUE_CHARS:
        return raw, None
    prefix = body[: cut + 1] if cut != -1 else b""
    return prefix, auth_value


def decode_auth_value(auth_value: str) -> List[Dict[str, str]]:
    """Decode + structurally validate the auth container (no crypto).

    Canonical-encoding boundary: the outer auth value and each
    ``signatures[].signature`` member must be the canonical unpadded
    base64url spelling of their bytes, and the container JSON must be the
    signer's compact sorted-key emit — one signed message has exactly one
    wire spelling. (``protected`` needs no such check: its spelling is part
    of the RFC 7515 signing input, so an alias there fails crypto.)

    Returns the raw ``{protected, signature}`` entries. Callers wanting the
    header contents validated run `validate_protected_header` per entry.
    """
    if not isinstance(auth_value, str) or len(auth_value) > MAX_AUTH_VALUE_CHARS:
        raise AuthFormatError("auth value missing or oversized")
    container_bytes = b64url_decode_canonical(auth_value, "auth value")
    container = _strict_json_loads(container_bytes, "auth container")
    if not isinstance(container, dict):
        raise AuthFormatError("auth container must be a JSON object")
    unknown = sorted(set(container) - {"signatures"})
    if unknown:
        raise AuthFormatError(
            f"auth container has unexpected member(s): {', '.join(unknown)}"
        )
    signatures = container.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        raise AuthFormatError("auth container must carry a non-empty signatures array")
    if len(signatures) > MAX_SIGNATURES:
        raise AuthFormatError(f"signatures array exceeds maximum of {MAX_SIGNATURES}")
    for index, entry in enumerate(signatures):
        if not isinstance(entry, dict) or sorted(entry) != ["protected", "signature"]:
            raise AuthFormatError(
                f"signatures[{index}] must be exactly {{protected, signature}}"
            )
        for member in ("protected", "signature"):
            value = entry[member]
            if not isinstance(value, str) or not _B64URL_RE.fullmatch(value):
                raise AuthFormatError(
                    f"signatures[{index}].{member} must be unpadded base64url"
                )
        b64url_decode_canonical(
            entry["signature"], f"signatures[{index}].signature"
        )
    if _compact_json(container).encode("utf-8") != container_bytes:
        raise AuthFormatError(
            "auth container is not the signer's canonical compact JSON emit "
            "— formatting variants give one signed message a second wire "
            "spelling"
        )
    return signatures


def auth_structure_errors(auth_value: Any) -> List[str]:
    """Validator-style structural check: full framing + header profile."""
    try:
        entries = decode_auth_value(auth_value)
        for entry in entries:
            validate_protected_header(entry["protected"])
    except AuthFormatError as exc:
        return [f"field 'auth': {exc}"]
    return []


# ---------------------------------------------------------------------------
# Signing (sender half)
# ---------------------------------------------------------------------------

def sign_payload(payload: bytes, signers: Sequence["FileKeySigner"]) -> str:
    """Sign the raw prefix bytes with 1-8 signers; return the auth value."""
    if not signers:
        raise SigningUnavailableError("no signing keys provided")
    if len(signers) > MAX_SIGNATURES:
        raise AuthFormatError(f"at most {MAX_SIGNATURES} signatures per message")
    entries = []
    for signer in signers:
        header = build_protected_header(
            kid=signer.kid, agent=signer.agent_urn, instance=signer.instance_urn
        )
        protected_b64 = encode_protected_header(header)
        signature = signer.sign(signing_input(protected_b64, payload))
        entries.append(
            {"protected": protected_b64, "signature": b64url_encode(signature)}
        )
    return b64url_encode(_compact_json({"signatures": entries}).encode("utf-8"))


def sign_and_append(payload: bytes, signers: Sequence["FileKeySigner"]) -> bytes:
    """Convenience: sign the payload and return the full signed message bytes."""
    return append_auth_trailer(payload, sign_payload(payload, signers))


# ---------------------------------------------------------------------------
# File key backend (pluggable interface; file-only in v0.4.0)
# ---------------------------------------------------------------------------

def _require_crypto() -> None:
    if not CRYPTO_AVAILABLE:
        raise SigningUnavailableError(
            "message signing requires the 'cryptography' package — "
            "install with: pip install 'oacp-cli[crypto]'"
        )


def _check_private_modes(key_path: Path) -> None:
    """Enforce the key-file permission boundary: no group/world access on the private
    key file or its directory (POSIX; the file backend targets POSIX hosts)."""
    if os.name != "posix":  # pragma: no cover - non-POSIX hosts
        return
    file_mode = stat.S_IMODE(os.stat(key_path).st_mode)
    if file_mode & 0o077:
        raise SigningUnavailableError(
            f"private key {key_path} is group/world-accessible "
            f"(mode {file_mode:o}); require 0600"
        )
    dir_mode = stat.S_IMODE(os.stat(key_path.parent).st_mode)
    if dir_mode & 0o077:
        raise SigningUnavailableError(
            f"private key directory {key_path.parent} is group/world-accessible "
            f"(mode {dir_mode:o}); require 0700"
        )


class FileKeySigner:
    """Signer backed by a private-key JSON file under $OACP_HOME/keys/.

    The key file is a private Ed25519 JWK (RFC 8037: kty/crv/x/d) plus OACP
    identity metadata (agent, trust_domain, instance_id, kid, created_at_utc).
    Loading enforces the 0600/0700 boundary and cross-checks that the private
    seed actually derives the stored public key and its RFC 7638 kid.
    """

    def __init__(self, key_path: Path):
        _require_crypto()
        self.key_path = Path(key_path)
        _check_private_modes(self.key_path)
        try:
            record = json.loads(self.key_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SigningUnavailableError(
                f"cannot read key file {self.key_path}: {exc}"
            ) from exc
        for field in ("kty", "crv", "x", "d", "kid", "agent", "trust_domain",
                      "instance_id"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise SigningUnavailableError(
                    f"key file {self.key_path} missing field {field!r}"
                )
        if record["kty"] != "OKP" or record["crv"] != "Ed25519":
            raise SigningUnavailableError(
                f"key file {self.key_path} is not an Ed25519 OKP key"
            )
        self._record = record
        try:
            seed = b64url_decode(record["d"])
            derived_public = (
                ed25519.Ed25519PrivateKey.from_private_bytes(seed)
                .public_key()
                .public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            )
        except (AuthFormatError, ValueError) as exc:
            raise SigningUnavailableError(
                f"key file {self.key_path} has an invalid private seed: {exc}"
            ) from exc
        derived_jwk = ed25519_public_jwk(derived_public)
        if derived_jwk["x"] != record["x"]:
            raise SigningUnavailableError(
                f"key file {self.key_path} private seed does not derive its "
                "stored public key"
            )
        if record["kid"] != jwk_thumbprint(derived_jwk):
            raise SigningUnavailableError(
                f"key file {self.key_path} kid does not match its public key thumbprint"
            )

    @property
    def kid(self) -> str:
        return self._record["kid"]

    @property
    def agent(self) -> str:
        return self._record["agent"]

    @property
    def agent_urn(self) -> str:
        return agent_urn(self._record["trust_domain"], self._record["agent"])

    @property
    def instance_urn(self) -> str:
        return instance_urn(self._record["instance_id"])

    @property
    def public_jwk(self) -> Dict[str, str]:
        return ed25519_public_jwk(b64url_decode(self._record["x"]))

    @property
    def created_at_utc(self) -> str:
        return str(self._record.get("created_at_utc", ""))

    def sign(self, data: bytes) -> bytes:
        _require_crypto()
        seed = b64url_decode(self._record["d"])
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        return private_key.sign(data)


def _keys_root(oacp_home: Path) -> Path:
    return Path(oacp_home).expanduser() / KEYS_DIRNAME


def _read_or_create_id(path: Path) -> str:
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = str(uuid.uuid4())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return value


def _write_private_file(path: Path, content: str) -> None:
    """Create the private key file 0600, refusing to overwrite (kid collision
    means the same key already exists)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def generate_keypair(agent: str, oacp_home: Path) -> Dict[str, Any]:
    """Generate an Ed25519 keypair for *agent* under $OACP_HOME/keys/.

    Layout: keys/<trust_domain>/<agent>/<instance_id>/<kid>.json (0600, dirs
    0700) plus a public catalog stub <kid>.pub.json for the
    `oacp trust import` flow. The trust-domain and machine-instance UUIDs are
    minted once per keys root and reused for every agent on this machine.

    Returns a report dict (paths, kid, URNs) — never the private material.
    """
    _require_crypto()
    if not AGENT_RE.fullmatch(agent or ""):
        raise ValueError(f"agent name must match {AGENT_RE.pattern}")

    keys_root = _keys_root(oacp_home)
    keys_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    trust_domain = _read_or_create_id(keys_root / TRUST_DOMAIN_FILENAME)
    instance_id = _read_or_create_id(keys_root / INSTANCE_ID_FILENAME)

    private_key = ed25519.Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    public_jwk = ed25519_public_jwk(public_bytes)
    kid = jwk_thumbprint(public_jwk)
    created_at = utc_now_iso()

    key_dir = keys_root / trust_domain / agent / instance_id
    current = keys_root
    for part in (trust_domain, agent, instance_id):
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)

    private_record = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": public_jwk["x"],
        "d": b64url_encode(seed),
        "kid": kid,
        "agent": agent,
        "trust_domain": trust_domain,
        "instance_id": instance_id,
        "created_at_utc": created_at,
    }
    key_path = key_dir / f"{kid}.json"
    try:
        _write_private_file(key_path, json.dumps(private_record, indent=2) + "\n")
    except FileExistsError:
        raise SigningUnavailableError(
            f"key {kid} already exists at {key_path} — refusing to overwrite"
        ) from None

    public_stub = {
        "kid": kid,
        "jwk": public_jwk,
        "agent": agent,
        "agent_urn": agent_urn(trust_domain, agent),
        "instance_urn": instance_urn(instance_id),
        "created_at_utc": created_at,
    }
    stub_path = key_dir / f"{kid}{PUBLIC_STUB_SUFFIX}"
    stub_path.write_text(json.dumps(public_stub, indent=2) + "\n", encoding="utf-8")

    return {
        "agent": agent,
        "kid": kid,
        "agent_urn": public_stub["agent_urn"],
        "instance_urn": public_stub["instance_urn"],
        "key_path": str(key_path),
        "public_stub_path": str(stub_path),
        "created_at_utc": created_at,
    }


def list_keys(oacp_home: Path, agent: Optional[str] = None) -> List[Dict[str, str]]:
    """List private key files under $OACP_HOME/keys/ (metadata only)."""
    keys_root = _keys_root(oacp_home)
    if not keys_root.is_dir():
        return []
    results: List[Dict[str, str]] = []
    for key_path in sorted(keys_root.glob("*/*/*/*.json")):
        if key_path.name.endswith(PUBLIC_STUB_SUFFIX):
            continue
        try:
            record = json.loads(key_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        record_agent = str(record.get("agent", ""))
        if agent and record_agent != agent:
            continue
        results.append(
            {
                "agent": record_agent,
                "kid": str(record.get("kid", "")),
                "created_at_utc": str(record.get("created_at_utc", "")),
                "key_path": str(key_path),
            }
        )
    return results


def load_signers(
    agent: str, oacp_home: Path, kid: Optional[str] = None
) -> List[FileKeySigner]:
    """Load the agent's local signing keys (newest first, capped at 8).

    Signing with every local key is the designed rotation-overlap behavior: during
    a rotation the message verifies against either pin. Remove retired key
    files to stop including them. With *kid* set, only that key is loaded.
    """
    entries = list_keys(oacp_home, agent=agent)
    if kid:
        entries = [e for e in entries if e["kid"] == kid]
    if not entries:
        where = _keys_root(oacp_home)
        raise SigningUnavailableError(
            f"no signing key for agent {agent!r} under {where} — "
            "run: oacp key gen --agent " + agent
        )
    entries.sort(key=lambda e: e["created_at_utc"], reverse=True)
    return [FileKeySigner(Path(e["key_path"])) for e in entries[:MAX_SIGNATURES]]
