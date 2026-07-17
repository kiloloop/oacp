#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the signing conformance fixtures — a deliberate act.

A golden change is a wire-format change: the committed messages pin the
trailer boundary, the signed prefix bytes, and the JWS preimages that every
implementation must reproduce. Do not regenerate to "fix" a failing test —
a diff here means the wire format moved and needs a ruling first (see
README.md).

Everything is deterministic: Ed25519 signatures (RFC 8032), the signer's
compact sorted-key JSON emit, and the pinned message ids/timestamps below.
Re-running against unchanged inputs is byte-for-byte idempotent.

Modes:
  --check                 regenerate in memory, diff against committed files,
                          exit 1 on any drift (writes nothing)
  --write                 write regenerated fixtures (refuses to change an
                          existing rendered prefix unless
                          --accept-render-change is also given)
  --accept-render-change  allow --write to replace prefix payload bytes when
                          the message renderer's output changed — this IS a
                          wire-format event; get a ruling before using it
  --mint-keys             bootstrap a fully EMPTY corpus: refuses to run if
                          any key, pin, message, or expected artifact exists
                          (minting near an established corpus would reseed
                          it, orphan every committed golden, and let --check
                          bless the reseed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The corpus normally lives beside this script; the env override exists so
# guard-rail regressions can exercise the CLI against an isolated corpus
# copy without ever touching the normative committed one.
FIXTURE_DIR = Path(
    os.environ.get("OACP_SIGNING_FIXTURE_DIR")
    or Path(__file__).resolve().parent
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import yaml  # noqa: E402

from message_signing import (  # noqa: E402
    AuthFormatError,
    FileKeySigner,
    agent_urn,
    b64url_decode,
    b64url_encode,
    decode_auth_value,
    ed25519_public_jwk,
    instance_urn,
    jwk_thumbprint,
    sign_and_append,
    signing_input,
    split_signed_message,
)
from message_verify import (  # noqa: E402
    classify_auth_trailer,
    load_allowed_signers,
    verify_message,
)
from send_inbox_message import build_message_dict, render_yaml  # noqa: E402
from validate_message import validate_message_file  # noqa: E402

KEYS_DIR = FIXTURE_DIR / "keys"
PINS_PATH = FIXTURE_DIR / "pins" / "allowed_signers.yaml"
MESSAGES_DIR = FIXTURE_DIR / "messages"
EXPECTED_DIR = FIXTURE_DIR / "expected"

# ---------------------------------------------------------------------------
# Pinned fixture identity — test vectors only, never real trust material.
# One trust domain, one machine instance per agent. Frozen forever: changing
# any of these orphans every committed golden.
# ---------------------------------------------------------------------------

TRUST_DOMAIN = "3f54e34d-9d31-4c8a-a5a7-64766f636170"
INSTANCES = {
    "alice": "aa11e9c2-0000-4000-8000-666978747572",
    "bob": "bb22e9c2-0000-4000-8000-666978747572",
    "carol": "cc33e9c2-0000-4000-8000-666978747572",
}
CREATED_AT = "2026-07-01T00:00:00Z"

# 43 valid base64url chars pinned to no receiver key — the unknown-kid vector.
# Canonical spelling required (final char carries zero unused pad bits): a
# non-canonical kid is its own vector, tamper_kid_alias.
UNKNOWN_KID = "UnknownKidUnknownKidUnknownKidUnknownKid040"

# Pinned message specs. `body` lines are part of the signed bytes — do not
# reword. `signers` is the auth-container order (multisig order matters for
# the order-independence golden).
MESSAGE_SPECS: List[Dict[str, Any]] = [
    {
        "case": "unsigned_notification",
        "sender": "alice",
        "signers": [],
        "msg_id": "msg-20260701000000-alice-a001",
        "type": "notification",
        "subject": "Unsigned conformance golden",
        "body": "No auth trailer anywhere.\nTwo lines, one final LF.",
    },
    {
        "case": "signed_basic",
        "sender": "alice",
        "signers": ["alice"],
        "msg_id": "msg-20260701000000-alice-a002",
        "type": "task_request",
        "subject": "Signed conformance golden",
        "body": (
            "Single-signature golden for the conformance corpus.\n"
            "Byte covenant: byte-different means different message — ü.\n"
            "The word tamperable appears once for the byte-flip vector."
        ),
    },
    {
        "case": "signed_multisig",
        "sender": "alice",
        "signers": ["bob", "alice"],
        "msg_id": "msg-20260701000000-alice-a003",
        "type": "notification",
        "subject": "Multisig conformance golden",
        "body": (
            "Two signatures, sender's own signature second: OR acceptance\n"
            "must be order-independent."
        ),
    },
    {
        "case": "signed_revoked",
        "sender": "carol",
        "signers": ["carol"],
        "msg_id": "msg-20260701000000-carol-a004",
        "type": "notification",
        "subject": "Revoked-pin conformance golden",
        "body": "Signed by a key the receiver has revoked.",
    },
    {
        "case": "signed_lookalike",
        "sender": "alice",
        "signers": ["alice"],
        "msg_id": "msg-20260701000000-alice-a005",
        "type": "notification",
        "subject": "Trailer lookalike golden",
        "body": (
            "The next line is payload content, not a trailer:\n"
            'auth: "QUJDRA"\n'
            "Only the last physical line of the file is ever the trailer."
        ),
    },
    {
        "case": "tamper_identity_mismatch",
        "sender": "bob",
        "signers": ["alice"],
        "msg_id": "msg-20260701000000-bob-a006",
        "type": "notification",
        "subject": "Identity mismatch vector",
        "body": "Payload claims bob; the only signature is alice's.",
    },
]

# Byte-tamper derivations from signed_basic (except where noted). Each entry:
# (case, source_case, mutation_fn_name). The functions below are the normative
# definition of each vector — committed bytes are their output.
TAMPER_SPECS: List[Tuple[str, str, str]] = [
    ("tamper_body_flip", "signed_basic", "mutate_body_flip"),
    ("tamper_trailer_transplant", "signed_basic", "mutate_trailer_transplant"),
    ("tamper_sig_corrupt", "signed_basic", "mutate_sig_corrupt"),
    ("tamper_kid_unknown", "signed_basic", "mutate_kid_unknown"),
    ("tamper_kid_cross_agent", "signed_basic", "mutate_kid_cross_agent"),
    ("tamper_header_reencode", "signed_basic", "mutate_header_reencode"),
    ("tamper_crlf_trailer", "signed_basic", "mutate_crlf_trailer"),
    ("tamper_trailing_blank_line", "signed_basic", "mutate_trailing_blank_line"),
    ("tamper_missing_final_lf", "signed_basic", "mutate_missing_final_lf"),
    ("tamper_trailing_space", "signed_basic", "mutate_trailing_space"),
    ("tamper_padded_b64", "signed_basic", "mutate_padded_b64"),
    ("tamper_indented_trailer", "signed_basic", "mutate_indented_trailer"),
    ("tamper_sig_alias", "signed_basic", "mutate_sig_alias"),
    ("tamper_auth_alias", "signed_basic", "mutate_auth_alias"),
    ("tamper_container_reformat", "signed_basic", "mutate_container_reformat"),
    ("tamper_kid_alias", "signed_basic", "mutate_kid_alias"),
]


def _compact_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Fixture keys
# ---------------------------------------------------------------------------

def corpus_artifacts_present() -> List[str]:
    """Every committed corpus artifact that already exists. Non-empty means
    the corpus is established and bootstrap minting must refuse."""
    present = [
        path
        for pattern_dir, glob in (
            (KEYS_DIR, "*.json"),
            (MESSAGES_DIR, "*"),
            (EXPECTED_DIR, "*"),
        )
        for path in sorted(pattern_dir.glob(glob))
    ]
    if PINS_PATH.is_file():
        present.append(PINS_PATH)
    return [str(path.relative_to(FIXTURE_DIR)) for path in present]


def mint_keys() -> None:
    """Bootstrap an EMPTY corpus only (main() enforces emptiness first): a
    fixture key is frozen identity, and minting near any established
    artifact would reseed the corpus and let --check bless the reseed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    for agent, instance_id in INSTANCES.items():
        key_path = KEYS_DIR / f"{agent}.json"
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
        record = {
            "_comment": (
                "CONFORMANCE TEST VECTOR — fixture-only Ed25519 key, "
                "published deliberately (like RFC 8037 A.3). Grants no "
                "authority anywhere; never pin outside this corpus."
            ),
            "kty": "OKP",
            "crv": "Ed25519",
            "x": public_jwk["x"],
            "d": b64url_encode(seed),
            "kid": jwk_thumbprint(public_jwk),
            "agent": agent,
            "trust_domain": TRUST_DOMAIN,
            "instance_id": instance_id,
            "created_at_utc": CREATED_AT,
        }
        key_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"minted fixture key: {key_path}")


def load_key_records() -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for agent in INSTANCES:
        path = KEYS_DIR / f"{agent}.json"
        if not path.is_file():
            raise SystemExit(
                f"missing fixture key {path} — an established corpus cannot "
                "re-sign itself without every committed key; restore it from "
                "version control (--mint-keys bootstraps a fully empty "
                "corpus only)"
            )
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("_comment", None)
        records[agent] = record
    return records


def signer_for(record: Dict[str, Any], workdir: Path) -> FileKeySigner:
    """FileKeySigner from a committed record. Git checkouts are 0644, and the
    signer enforces the private 0600/0700 boundary, so the record is staged
    into a tight-mode scratch copy first."""
    key_dir = workdir / "keys" / record["agent"]
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = key_dir / f"{record['kid']}.json"
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    return FileKeySigner(key_path)


def build_pins(records: Dict[str, Dict[str, Any]]) -> str:
    """Receiver pins for the corpus: alice + bob active, carol revoked
    (jwk retained so the corpus also pins revoked-with-material handling)."""
    lines = [
        "# Receiver pins for the signing conformance corpus (fixture keys",
        "# only — see README.md). Format v1 per message_signing.md.",
        "version: 1",
        "signers:",
    ]
    status = {"alice": "active", "bob": "active", "carol": "revoked"}
    for agent in ("alice", "bob", "carol"):
        record = records[agent]
        lines += [
            f"  - agent: {agent}",
            f"    domain: {TRUST_DOMAIN}",
            f"    instance: {record['instance_id']}",
            f"    kid: {record['kid']}",
            "    jwk:",
            "      kty: OKP",
            "      crv: Ed25519",
            f"      x: {record['x']}",
            f"    status: {status[agent]}",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Golden rendering + signing
# ---------------------------------------------------------------------------

def render_prefix(spec: Dict[str, Any]) -> bytes:
    msg = build_message_dict(
        sender=spec["sender"],
        recipient="dave",
        msg_type=spec["type"],
        subject=spec["subject"],
        body=spec["body"],
    )
    msg["id"] = spec["msg_id"]
    msg["created_at_utc"] = CREATED_AT
    return render_yaml(msg).encode("utf-8")


def build_goldens(
    records: Dict[str, Dict[str, Any]], workdir: Path
) -> Dict[str, Dict[str, bytes]]:
    """case -> {prefix: bytes, message: bytes} (message == prefix when unsigned)."""
    signers = {
        agent: signer_for(record, workdir) for agent, record in records.items()
    }
    goldens: Dict[str, Dict[str, bytes]] = {}
    for spec in MESSAGE_SPECS:
        prefix = render_prefix(spec)
        if spec["signers"]:
            message = sign_and_append(
                prefix, [signers[name] for name in spec["signers"]]
            )
        else:
            message = prefix
        goldens[spec["case"]] = {"prefix": prefix, "message": message}
    return goldens


# ---------------------------------------------------------------------------
# Tamper mutations — the normative vector definitions
# ---------------------------------------------------------------------------

def _split_or_die(raw: bytes) -> Tuple[bytes, str]:
    prefix, auth_value = split_signed_message(raw)
    assert auth_value is not None, "tamper source must be a signed golden"
    return prefix, auth_value


def _reencode_first_header(
    raw: bytes, edit: Any
) -> bytes:
    """Decode the auth container, apply *edit* to the first protected header,
    re-encode both canonically, and re-frame the trailer."""
    prefix, auth_value = _split_or_die(raw)
    entries = decode_auth_value(auth_value)
    header = json.loads(b64url_decode(entries[0]["protected"]))
    edit(header)
    entries[0] = {
        "protected": b64url_encode(_compact_json(header).encode("utf-8")),
        "signature": entries[0]["signature"],
    }
    new_auth = b64url_encode(
        _compact_json({"signatures": entries}).encode("utf-8")
    )
    return prefix + f'auth: "{new_auth}"\n'.encode("ascii")


def mutate_body_flip(raw: bytes, goldens: Dict[str, Dict[str, bytes]]) -> bytes:
    """Flip one bit of one payload byte inside the signed prefix."""
    index = raw.index(b"tamperable")
    flipped = bytes([raw[index] ^ 0x01])
    return raw[:index] + flipped + raw[index + 1:]


def mutate_trailer_transplant(
    raw: bytes, goldens: Dict[str, Dict[str, bytes]]
) -> bytes:
    """Splice signed_multisig's trailer onto signed_basic's body."""
    prefix, _ = _split_or_die(raw)
    donor_prefix, donor_auth = _split_or_die(goldens["signed_multisig"]["message"])
    del donor_prefix
    return prefix + f'auth: "{donor_auth}"\n'.encode("ascii")


def mutate_sig_corrupt(raw: bytes, goldens: Dict[str, Dict[str, bytes]]) -> bytes:
    """Flip one bit of the first decoded signature byte, re-encoded
    canonically — framing stays perfect, crypto must fail. (Mutating the
    encoding's final character is NOT a byte corruption: its unused pad bits
    alias to the same 64 signature bytes — that spelling alias is its own
    vector, tamper_sig_alias.)"""
    prefix, auth_value = _split_or_die(raw)
    entries = decode_auth_value(auth_value)
    sig_bytes = bytearray(b64url_decode(entries[0]["signature"]))
    sig_bytes[0] ^= 0x01
    entries[0] = {
        "protected": entries[0]["protected"],
        "signature": b64url_encode(bytes(sig_bytes)),
    }
    new_auth = b64url_encode(
        _compact_json({"signatures": entries}).encode("utf-8")
    )
    return prefix + f'auth: "{new_auth}"\n'.encode("ascii")


def mutate_kid_unknown(raw: bytes, goldens: Dict[str, Dict[str, bytes]]) -> bytes:
    """Point the header at a kid no receiver pin knows."""
    return _reencode_first_header(
        raw, lambda header: header.__setitem__("kid", UNKNOWN_KID)
    )


def mutate_kid_cross_agent(
    raw: bytes, goldens: Dict[str, Dict[str, bytes]]
) -> bytes:
    """Point alice's signature at bob's pinned kid (agent URN untouched)."""
    bob_kid = json.loads((KEYS_DIR / "bob.json").read_text(encoding="utf-8"))["kid"]
    return _reencode_first_header(
        raw, lambda header: header.__setitem__("kid", bob_kid)
    )


_B64URL_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def mutate_kid_alias(raw: bytes, goldens: Dict[str, Dict[str, bytes]]) -> bytes:
    """Respell alice's kid with a non-zero unused pad bit — the same 32
    thumbprint bytes under a second spelling. Kid comparisons are
    exact-string, so an accepted alias would dodge the pin (and any
    revocation) for the very key it names; the header validator must
    reject the spelling outright."""

    def edit(header: Dict[str, Any]) -> None:
        kid = header["kid"]
        alias = kid[:-1] + _B64URL_ALPHABET[_B64URL_ALPHABET.index(kid[-1]) | 1]
        assert alias != kid, "canonical final char must have pad bits clear"
        header["kid"] = alias

    return _reencode_first_header(raw, edit)


def mutate_header_reencode(
    raw: bytes, goldens: Dict[str, Dict[str, bytes]]
) -> bytes:
    """Rewrite the header as a fully self-consistent bob identity — the
    signature bytes are still alice's, so crypto must fail."""
    bob = json.loads((KEYS_DIR / "bob.json").read_text(encoding="utf-8"))

    def edit(header: Dict[str, Any]) -> None:
        header["kid"] = bob["kid"]
        header["oacp"]["agent"] = agent_urn(TRUST_DOMAIN, "bob")
        header["oacp"]["instance"] = instance_urn(bob["instance_id"])

    return _reencode_first_header(raw, edit)


def mutate_crlf_trailer(raw: bytes, goldens: Dict[str, Dict[str, bytes]]) -> bytes:
    """CRLF on the trailer line — a byte-different signed artifact."""
    assert raw.endswith(b"\n") and not raw.endswith(b"\r\n")
    return raw[:-1] + b"\r\n"


def mutate_trailing_blank_line(
    raw: bytes, goldens: Dict[str, Dict[str, bytes]]
) -> bytes:
    return raw + b"\n"


def mutate_missing_final_lf(
    raw: bytes, goldens: Dict[str, Dict[str, bytes]]
) -> bytes:
    return raw[:-1]


def mutate_trailing_space(
    raw: bytes, goldens: Dict[str, Dict[str, bytes]]
) -> bytes:
    return raw[:-1] + b" \n"


def mutate_padded_b64(raw: bytes, goldens: Dict[str, Dict[str, bytes]]) -> bytes:
    """Padded base64url in the trailer value violates exact framing."""
    prefix, auth_value = _split_or_die(raw)
    return prefix + f'auth: "{auth_value}=="\n'.encode("ascii")


def mutate_indented_trailer(
    raw: bytes, goldens: Dict[str, Dict[str, bytes]]
) -> bytes:
    """An indented auth line is payload content, not a trailer (column-0
    rule) — the artifact reads as unsigned."""
    prefix, auth_value = _split_or_die(raw)
    return prefix + f'  auth: "{auth_value}"\n'.encode("ascii")


_B64URL_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _alias_final_char(text: str) -> str:
    """Set the lowest unused pad bit of a base64url value's final character:
    identical decoded bytes, byte-different spelling. Requires unused pad
    bits (length not a multiple of 4) and a canonical input."""
    assert len(text) % 4 != 0, "no unused pad bits to alias"
    aliased = text[:-1] + _B64URL_ALPHABET[
        _B64URL_ALPHABET.index(text[-1]) | 1
    ]
    assert aliased != text and b64url_decode(aliased) == b64url_decode(text)
    return aliased


def mutate_sig_alias(raw: bytes, goldens: Dict[str, Dict[str, bytes]]) -> bytes:
    """Alias the signature member's encoding: set an unused pad bit in its
    final character — same 64 signature bytes, byte-different spelling. The
    canonical round-trip check must reject it (before the canonical-encoding
    ruling this artifact verified)."""
    prefix, auth_value = _split_or_die(raw)
    entries = decode_auth_value(auth_value)
    entries[0] = {
        "protected": entries[0]["protected"],
        "signature": _alias_final_char(entries[0]["signature"]),
    }
    new_auth = b64url_encode(
        _compact_json({"signatures": entries}).encode("utf-8")
    )
    return prefix + f'auth: "{new_auth}"\n'.encode("ascii")


def mutate_auth_alias(raw: bytes, goldens: Dict[str, Dict[str, bytes]]) -> bytes:
    """Alias the OUTER trailer value's encoding: same container bytes,
    byte-different auth value — rejected by the same canonical round-trip
    check, before the container JSON is even parsed."""
    prefix, auth_value = _split_or_die(raw)
    return prefix + f'auth: "{_alias_final_char(auth_value)}"\n'.encode("ascii")


def mutate_container_reformat(
    raw: bytes, goldens: Dict[str, Dict[str, bytes]]
) -> bytes:
    """Re-emit the container JSON with non-compact separators: identical
    parsed structure and signature entries, byte-different container bytes
    (spelled canonically) — the canonical compact-emit check must reject."""
    prefix, auth_value = _split_or_die(raw)
    entries = decode_auth_value(auth_value)
    reformatted = json.dumps(
        {"signatures": entries}, sort_keys=True, separators=(", ", ": ")
    )
    new_auth = b64url_encode(reformatted.encode("utf-8"))
    return prefix + f'auth: "{new_auth}"\n'.encode("ascii")


def build_tampers(
    goldens: Dict[str, Dict[str, bytes]]
) -> Dict[str, bytes]:
    tampers: Dict[str, bytes] = {}
    for case, source, fn_name in TAMPER_SPECS:
        raw = goldens[source]["message"]
        tampers[case] = globals()[fn_name](raw, goldens)
    return tampers


# ---------------------------------------------------------------------------
# Expected files — produced by executing the implementation under a corpus
# that already has a ruling. A diff on regen means observed behavior moved.
# ---------------------------------------------------------------------------

def extract_preimage_sha256s(raw: bytes) -> List[str]:
    """RFC 7515 signing-input digests for a framed trailer — forensic
    EXTRACTION, deliberately not the acceptance gate.

    An encoding-alias or formatting-variant container still carries
    well-defined signing inputs: the signing input is a pure function of
    each ``protected`` value and the signed prefix bytes, neither of which
    a trailer-spelling change touches — so the corpus pins those digests
    even for vectors the strict `decode_auth_value` rejects. ``[]`` is
    reserved for artifacts from which no protected value and signed prefix
    can actually be extracted (no ok trailer, undecodable/unparsable
    container, no structurally usable entries)."""
    prefix, auth_value = split_signed_message(raw)
    if auth_value is None:
        return []
    try:
        container = json.loads(b64url_decode(auth_value).decode("utf-8"))
    except (AuthFormatError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(container, dict):
        return []
    signatures = container.get("signatures")
    if not isinstance(signatures, list):
        return []
    digests: List[str] = []
    for entry in signatures:
        if not isinstance(entry, dict) or not isinstance(
            entry.get("protected"), str
        ):
            continue
        try:
            digests.append(
                hashlib.sha256(
                    signing_input(entry["protected"], prefix)
                ).hexdigest()
            )
        except AuthFormatError:
            continue
    return digests


def build_expected(
    case: str,
    raw: bytes,
    pins: Dict[str, Dict[str, Any]],
    *,
    prefix_file: Optional[str],
    signers: Optional[List[str]],
    workdir: Path,
) -> Dict[str, Any]:
    from message_verify import annotate

    state, class_prefix, _ = classify_auth_trailer(raw)
    message_auth = verify_message(raw, pins)
    message_auth.pop("verified_at_utc", None)

    # Validator observations against the exact on-disk bytes.
    scratch = workdir / f"{case}.yaml"
    scratch.write_bytes(raw)
    validate_errors = validate_message_file(scratch)

    expected: Dict[str, Any] = {
        "case": case,
        "message": f"messages/{case}.yaml",
        "expected": {
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "trailer_state": state,
            "prefix_sha256": hashlib.sha256(class_prefix).hexdigest(),
            "signing_input_sha256": extract_preimage_sha256s(raw),
            "message_auth": message_auth,
            "annotation": annotate(message_auth),
            "validate_errors": validate_errors,
        },
    }
    if prefix_file:
        expected["prefix"] = prefix_file
    if signers:
        expected["signers"] = signers
    return expected


def dump_expected(expected: Dict[str, Any]) -> str:
    return yaml.safe_dump(
        expected, sort_keys=True, allow_unicode=True, default_flow_style=False
    )


# ---------------------------------------------------------------------------
# Assembly / check / write
# ---------------------------------------------------------------------------

def build_corpus() -> Dict[Path, bytes]:
    records = load_key_records()
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        goldens = build_goldens(records, workdir)
        tampers = build_tampers(goldens)

        files: Dict[Path, bytes] = {}
        files[PINS_PATH] = build_pins(records).encode("utf-8")
        pins = load_allowed_signers_from_bytes(files[PINS_PATH], workdir)

        spec_by_case = {spec["case"]: spec for spec in MESSAGE_SPECS}
        for case, parts in goldens.items():
            files[MESSAGES_DIR / f"{case}.yaml"] = parts["message"]
            prefix_file: Optional[str] = None
            if spec_by_case[case]["signers"]:
                prefix_file = f"messages/{case}.prefix.yaml"
                files[MESSAGES_DIR / f"{case}.prefix.yaml"] = parts["prefix"]
            expected = build_expected(
                case,
                parts["message"],
                pins,
                prefix_file=prefix_file,
                signers=spec_by_case[case]["signers"] or None,
                workdir=workdir,
            )
            files[EXPECTED_DIR / f"{case}.yaml"] = dump_expected(expected).encode(
                "utf-8"
            )
        for case, raw in tampers.items():
            files[MESSAGES_DIR / f"{case}.yaml"] = raw
            expected = build_expected(
                case, raw, pins, prefix_file=None, signers=None, workdir=workdir
            )
            files[EXPECTED_DIR / f"{case}.yaml"] = dump_expected(expected).encode(
                "utf-8"
            )
        return files


def load_allowed_signers_from_bytes(
    content: bytes, workdir: Path
) -> Dict[str, Dict[str, Any]]:
    pins_scratch = workdir / "allowed_signers.yaml"
    pins_scratch.write_bytes(content)
    return load_allowed_signers(pins_scratch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--accept-render-change", action="store_true")
    parser.add_argument("--mint-keys", action="store_true")
    args = parser.parse_args()

    if args.mint_keys:
        if not args.write:
            parser.error("--mint-keys requires --write")
        existing = corpus_artifacts_present()
        if existing:
            print(
                "REFUSING --mint-keys: the corpus is not empty "
                f"({len(existing)} artifact(s) exist, e.g. {existing[0]})."
            )
            print(
                "Minting keys near an established corpus would silently "
                "reseed it — and a reseeded corpus passes its own --check. "
                "Bootstrap only ever applies to a fully empty corpus; an "
                "established one changes solely via a ruling (README.md)."
            )
            return 1
        mint_keys()

    files = build_corpus()

    drift: List[str] = []
    render_drift: List[str] = []
    for path, content in sorted(files.items()):
        existing = path.read_bytes() if path.is_file() else None
        if existing == content:
            continue
        rel = str(path.relative_to(FIXTURE_DIR))
        drift.append(rel)
        if path.name.endswith(".prefix.yaml") and existing is not None:
            render_drift.append(rel)

    if args.check:
        if drift:
            print("DRIFT — regenerated corpus differs from committed fixtures:")
            for rel in drift:
                print(f"  {rel}")
            print(
                "A golden change is a wire-format change and needs a ruling — "
                "see README.md."
            )
            return 1
        print(f"OK — {len(files)} fixture files match the committed corpus.")
        return 0

    if render_drift and not args.accept_render_change:
        print("REFUSING to overwrite rendered prefix bytes:")
        for rel in render_drift:
            print(f"  {rel}")
        print(
            "The message renderer's output changed for a pinned spec — that is "
            "a wire-format event. Get a ruling, then rerun with "
            "--accept-render-change."
        )
        return 1

    for path, content in sorted(files.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print(f"wrote {len(files)} fixture files ({len(drift)} changed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
