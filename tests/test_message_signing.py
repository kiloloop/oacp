# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for message_signing.py — sender core + shared wire framing.

Covers the sender-half design contract: preimage stability
over raw prefix bytes, detached-JWS auth trailer emit, append-once atomicity,
the locked JOSE profile, keygen/kid tooling, and sender sign-on-send.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from message_signing import (  # noqa: E402
    CRYPTO_AVAILABLE,
    JWS_ALG,
    JWS_TYP,
    MAX_SIGNATURES,
    SIG_DOMAIN,
    SIG_SCHEME,
    AuthFormatError,
    FileKeySigner,
    SigningUnavailableError,
    append_auth_trailer,
    auth_structure_errors,
    b64url_decode,
    b64url_encode,
    build_protected_header,
    decode_auth_value,
    ed25519_public_jwk,
    encode_protected_header,
    generate_keypair,
    jwk_thumbprint,
    list_keys,
    load_signers,
    sign_and_append,
    sign_payload,
    signing_input,
    split_signed_message,
    validate_kid,
    validate_protected_header,
    validate_public_ed25519_jwk,
)
from send_inbox_message import (  # noqa: E402
    build_message_dict,
    render_yaml,
    send_message,
)
from validate_message import validate_message_file  # noqa: E402

# RFC 8037 test key (Appendix A.1) and its RFC 7638 thumbprint (Appendix A.3).
RFC8037_X = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
RFC8037_D = "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A"
RFC8037_THUMBPRINT = "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
# RFC 8037 Appendix A.4: Ed25519 signature over the JWS signing input for
# protected {"alg":"EdDSA"} and payload "Example of Ed25519 signing".
RFC8037_A4_SIG = (
    "hgyY0il_MGCjP0JzlnLWG1PPOt7-09PGcvMg3AIbQR6dWbhijcNR4ki4iylGjg5B"
    "hVsPt9g7sVvpAr_MuM0KAg"
)

_B64URL_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def noncanonical_x_alias(x: str) -> str:
    """Alias spelling of a canonical 43-char base64url x value.

    43 characters carry 258 bits for a 256-bit key, so the final
    character's low 2 bits are unused padding and must be zero in the
    canonical encoding. Setting one yields a distinct string that decodes
    to the identical 32 key bytes — the encoding alias the strict public
    JWK boundary must reject, or one key gets two spellings and two kids.
    """
    return x[:-1] + _B64URL_ALPHABET[_B64URL_ALPHABET.index(x[-1]) | 1]


def _make_urn_pair() -> tuple:
    return (
        "urn:oacp:agent:00000000-0000-4000-8000-000000000000:iris",
        "urn:uuid:11111111-1111-4111-8111-111111111111",
    )


def _valid_auth_value(payload: bytes = b"x: y\n") -> str:
    """Structurally valid (but unverifiable) auth value for framing tests."""
    agent, instance = _make_urn_pair()
    header = build_protected_header(RFC8037_THUMBPRINT, agent, instance)
    entry = {
        "protected": encode_protected_header(header),
        "signature": b64url_encode(b"\x00" * 64),
    }
    return b64url_encode(
        json.dumps({"signatures": [entry]}, sort_keys=True, separators=(",", ":")).encode()
    )


class TestB64Url(unittest.TestCase):
    def test_round_trip(self) -> None:
        for blob in (b"", b"f", b"fo", b"foo", b"\x00\xff" * 33):
            if blob:
                self.assertEqual(b64url_decode(b64url_encode(blob)), blob)

    def test_unpadded(self) -> None:
        self.assertNotIn("=", b64url_encode(b"any carnal pleasure"))

    def test_rejects_padding_and_foreign_chars(self) -> None:
        for bad in ("abc=", "a+b", "a/b", "", "a b"):
            with self.assertRaises(AuthFormatError):
                b64url_decode(bad)

    def test_rejects_impossible_length(self) -> None:
        with self.assertRaises(AuthFormatError):
            b64url_decode("abcde")


class TestThumbprintAndHeader(unittest.TestCase):
    def test_rfc7638_ed25519_golden_vector(self) -> None:
        jwk = {"kty": "OKP", "crv": "Ed25519", "x": RFC8037_X}
        self.assertEqual(jwk_thumbprint(jwk), RFC8037_THUMBPRINT)

    def test_public_jwk_shape(self) -> None:
        jwk = ed25519_public_jwk(b"\x01" * 32)
        self.assertEqual(jwk["kty"], "OKP")
        self.assertEqual(jwk["crv"], "Ed25519")
        with self.assertRaises(AuthFormatError):
            ed25519_public_jwk(b"\x01" * 31)

    def test_non_canonical_x_alias_rejected(self) -> None:
        alias = noncanonical_x_alias(RFC8037_X)
        # prove it is a true alias: distinct string, identical key bytes,
        # distinct thumbprint — accepting it would give a revoked key a
        # fresh active kid.
        self.assertNotEqual(alias, RFC8037_X)
        self.assertEqual(b64url_decode(alias), b64url_decode(RFC8037_X))
        self.assertNotEqual(
            jwk_thumbprint({"kty": "OKP", "crv": "Ed25519", "x": alias}),
            jwk_thumbprint({"kty": "OKP", "crv": "Ed25519", "x": RFC8037_X}),
        )
        canonical = validate_public_ed25519_jwk(
            {"kty": "OKP", "crv": "Ed25519", "x": RFC8037_X}
        )
        self.assertEqual(canonical["x"], RFC8037_X)
        with self.assertRaises(AuthFormatError):
            validate_public_ed25519_jwk(
                {"kty": "OKP", "crv": "Ed25519", "x": alias}
            )

    def test_validate_kid_requires_canonical_spelling(self) -> None:
        validate_kid(RFC8037_THUMBPRINT)
        alias = noncanonical_x_alias(RFC8037_THUMBPRINT)
        # same 32 thumbprint bytes, second spelling — kid comparisons are
        # exact-string everywhere (pin lookup, catalog keys), so accepting
        # the alias would let it dodge the pin (and any revocation) for
        # the very key it names.
        self.assertEqual(b64url_decode(alias), b64url_decode(RFC8037_THUMBPRINT))
        with self.assertRaises(AuthFormatError):
            validate_kid(alias)

    def test_header_round_trip(self) -> None:
        agent, instance = _make_urn_pair()
        header = build_protected_header(RFC8037_THUMBPRINT, agent, instance)
        parsed = validate_protected_header(encode_protected_header(header))
        self.assertEqual(parsed, header)
        self.assertEqual(parsed["alg"], JWS_ALG)
        self.assertEqual(parsed["typ"], JWS_TYP)
        self.assertEqual(parsed["oacp"]["scheme"], SIG_SCHEME)
        self.assertEqual(parsed["oacp"]["domain"], SIG_DOMAIN)

    def _mutated_header(self, **overrides) -> str:
        agent, instance = _make_urn_pair()
        header = build_protected_header(RFC8037_THUMBPRINT, agent, instance)
        for key, value in overrides.items():
            if value is None:
                header.pop(key, None)
            else:
                header[key] = value
        return encode_protected_header(header)

    def test_profile_rejections(self) -> None:
        agent, instance = _make_urn_pair()
        cases = [
            self._mutated_header(alg="ES256"),
            self._mutated_header(typ="JWT"),
            self._mutated_header(kid=None),
            self._mutated_header(crit=["oacp", "b64"]),
            self._mutated_header(crit=[]),
            self._mutated_header(jku="https://example.com/keys"),
            self._mutated_header(jwk={"kty": "OKP"}),
            self._mutated_header(x5c=["MIIB..."]),
            self._mutated_header(extra="member"),
            self._mutated_header(
                oacp={"scheme": "raw-prefix-v2", "domain": SIG_DOMAIN,
                      "agent": agent, "instance": instance}
            ),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": "urn:other:v1",
                      "agent": agent, "instance": instance}
            ),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": SIG_DOMAIN,
                      "agent": "iris", "instance": instance}
            ),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": SIG_DOMAIN,
                      "agent": agent, "instance": "laptop-1"}
            ),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": SIG_DOMAIN,
                      "agent": agent, "instance": instance, "extra": 1}
            ),
        ]
        for encoded in cases:
            with self.assertRaises(AuthFormatError):
                validate_protected_header(encoded)

    def test_duplicate_json_keys_rejected(self) -> None:
        raw = b'{"alg":"EdDSA","alg":"EdDSA"}'
        with self.assertRaises(AuthFormatError):
            validate_protected_header(b64url_encode(raw))

    def test_identity_shape_rejections(self) -> None:
        # kid must be a real RFC 7638 thumbprint shape;
        # URNs must carry a UUID trust domain / UUID instance.
        agent, instance = _make_urn_pair()
        cases = [
            self._mutated_header(kid="not-a-thumbprint"),
            self._mutated_header(kid=RFC8037_THUMBPRINT + "A"),
            self._mutated_header(kid=RFC8037_THUMBPRINT[:-1] + "="),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": SIG_DOMAIN,
                      "agent": "urn:oacp:agent:", "instance": instance}
            ),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": SIG_DOMAIN,
                      "agent": "urn:oacp:agent:not-a-uuid:iris",
                      "instance": instance}
            ),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": SIG_DOMAIN,
                      "agent": agent.rsplit(":", 1)[0] + ":bad/name",
                      "instance": instance}
            ),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": SIG_DOMAIN,
                      "agent": agent, "instance": "urn:uuid:not-a-uuid"}
            ),
            self._mutated_header(
                oacp={"scheme": SIG_SCHEME, "domain": SIG_DOMAIN,
                      "agent": agent, "instance": "urn:uuid:"}
            ),
        ]
        for encoded in cases:
            with self.assertRaises(AuthFormatError):
                validate_protected_header(encoded)


class TestSigningInput(unittest.TestCase):
    def test_golden_vector(self) -> None:
        # Pinned preimage: any independent JWS tool reproduces this by
        # base64url-encoding protected header and payload around a dot.
        protected_b64 = b64url_encode(b'{"alg":"EdDSA"}')
        payload = b"Example of Ed25519 signing"
        expected = (
            b"eyJhbGciOiJFZERTQSJ9.RXhhbXBsZSBvZiBFZDI1NTE5IHNpZ25pbmc"
        )
        self.assertEqual(signing_input(protected_b64, payload), expected)

    def test_preimage_stability_and_byte_sensitivity(self) -> None:
        protected_b64 = b64url_encode(b'{"alg":"EdDSA"}')
        payload = "body: café\n".encode("utf-8")
        first = signing_input(protected_b64, payload)
        second = signing_input(protected_b64, bytes(payload))
        self.assertEqual(first, second)
        tampered = payload[:-2] + b"E\n"
        self.assertNotEqual(signing_input(protected_b64, tampered), first)

    def test_rejects_empty_payload(self) -> None:
        with self.assertRaises(AuthFormatError):
            signing_input(b64url_encode(b"{}"), b"")


class TestTrailerFraming(unittest.TestCase):
    def test_append_and_split_round_trip(self) -> None:
        payload = b"id: msg-1\nbody: |\n  hello\n"
        auth_value = _valid_auth_value()
        signed = append_auth_trailer(payload, auth_value)
        prefix, parsed_value = split_signed_message(signed)
        self.assertEqual(prefix, payload)
        self.assertEqual(parsed_value, auth_value)

    def test_append_once(self) -> None:
        payload = b"id: msg-1\n"
        signed = append_auth_trailer(payload, _valid_auth_value())
        with self.assertRaises(AuthFormatError):
            append_auth_trailer(signed, _valid_auth_value())

    def test_append_requires_trailing_newline(self) -> None:
        with self.assertRaises(AuthFormatError):
            append_auth_trailer(b"id: msg-1", _valid_auth_value())

    def test_split_unsigned(self) -> None:
        raw = b"id: msg-1\nbody: hello\n"
        self.assertEqual(split_signed_message(raw), (raw, None))

    def test_split_ignores_mid_body_lookalikes(self) -> None:
        # Auth-lookalike lines that are NOT the final physical line are
        # payload content — only the strict last line is ever the trailer.
        raw = b'auth: "QUJD"\nbody: |\n  auth: "QUJD"\nsubject: x\n'
        self.assertEqual(split_signed_message(raw), (raw, None))

    def test_split_rejects_trailing_whitespace_variants(self) -> None:
        base = b"id: msg-1\n"
        candidates = [
            base + b'auth: "QUJD" \n',      # trailing space inside the line
            base + b'auth: "QUJD"\n\n',     # blank line after the trailer
            base + b'auth: "QUJD"',         # no final LF
            base + b'auth: QUJD\n',         # unquoted
            base + b'auth: "QUJ=D"\n',      # padding char
        ]
        for raw in candidates:
            prefix, value = split_signed_message(raw)
            self.assertIsNone(value, raw)
            self.assertEqual(prefix, raw)

    def test_split_signed_then_content_after_is_unsigned(self) -> None:
        raw = b'id: msg-1\nauth: "QUJD"\ntrailing: content\n'
        self.assertEqual(split_signed_message(raw), (raw, None))


class TestAuthContainer(unittest.TestCase):
    def test_valid_container(self) -> None:
        entries = decode_auth_value(_valid_auth_value())
        self.assertEqual(len(entries), 1)
        validate_protected_header(entries[0]["protected"])
        self.assertEqual(auth_structure_errors(_valid_auth_value()), [])

    def _container(self, obj) -> str:
        # Canonical compact emit: rejection tests must fail on their own
        # intended defect, not on the compact-emit formatting check.
        return b64url_encode(
            json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
        )

    def test_rejections(self) -> None:
        entry = {
            "protected": b64url_encode(b"{}"),
            "signature": b64url_encode(b"\x00" * 64),
        }
        cases = [
            "not-base64url!",
            self._container([]),
            self._container({}),
            self._container({"signatures": []}),
            self._container({"signatures": [entry], "payload": "abc"}),
            self._container({"signatures": [entry] * (MAX_SIGNATURES + 1)}),
            self._container({"signatures": [{"protected": entry["protected"]}]}),
            self._container(
                {"signatures": [dict(entry, header={"kid": "x"})]}
            ),
            self._container(
                {"signatures": [dict(entry, signature="has=padding")]}
            ),
        ]
        for value in cases:
            with self.assertRaises(AuthFormatError):
                decode_auth_value(value)

    def test_structure_errors_cover_header_profile(self) -> None:
        entry = {
            "protected": b64url_encode(b'{"alg":"ES256"}'),
            "signature": b64url_encode(b"\x00" * 64),
        }
        errors = auth_structure_errors(self._container({"signatures": [entry]}))
        self.assertEqual(len(errors), 1)
        self.assertIn("auth", errors[0])

    def test_non_canonical_outer_value_rejected(self) -> None:
        # "e30" is the canonical spelling of b"{}"; "e31" sets an unused pad
        # bit and decodes to the identical bytes. The alias must be rejected
        # at the canonical-encoding boundary BEFORE the container is parsed;
        # the canonical spelling proceeds to (and fails) the structure check.
        self.assertEqual(b64url_encode(b"{}"), "e30")
        self.assertEqual(b64url_decode("e31"), b"{}")
        with self.assertRaises(AuthFormatError) as ctx:
            decode_auth_value("e31")
        self.assertIn("canonical base64url", str(ctx.exception))
        with self.assertRaises(AuthFormatError) as ctx:
            decode_auth_value("e30")
        self.assertNotIn("canonical", str(ctx.exception))

    def test_non_canonical_signature_member_rejected(self) -> None:
        # 64 zero bytes encode canonically to 86 "A"s; setting an unused pad
        # bit in the final character ("B") decodes to the same 64 bytes.
        canonical = b64url_encode(b"\x00" * 64)
        aliased = canonical[:-1] + "B"
        self.assertEqual(b64url_decode(aliased), b"\x00" * 64)
        entry = {"protected": b64url_encode(b"{}"), "signature": aliased}
        with self.assertRaises(AuthFormatError) as ctx:
            decode_auth_value(self._container({"signatures": [entry]}))
        self.assertIn("signatures[0].signature", str(ctx.exception))
        self.assertIn("canonical base64url", str(ctx.exception))

    def test_non_compact_container_rejected(self) -> None:
        # Identical parsed structure, byte-different container JSON
        # (whitespace after separators) — formatting variants must not give
        # one signed message a second wire spelling.
        entry = {
            "protected": b64url_encode(b"{}"),
            "signature": b64url_encode(b"\x00" * 64),
        }
        reformatted = b64url_encode(
            json.dumps(
                {"signatures": [entry]}, sort_keys=True, separators=(", ", ": ")
            ).encode()
        )
        with self.assertRaises(AuthFormatError) as ctx:
            decode_auth_value(reformatted)
        self.assertIn("compact JSON emit", str(ctx.exception))


class TestRenderYamlLoudReject(unittest.TestCase):
    def test_rejects_auth_and_sig_fields(self) -> None:
        msg = build_message_dict(
            sender="iris", recipient="claude", msg_type="notification",
            subject="s", body="b",
        )
        for key in ("auth", "sig_alg", "sig_by"):
            data = dict(msg)
            data[key] = "value"
            with self.assertRaises(ValueError):
                render_yaml(data)


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography not installed")
class TestEd25519GoldenVector(unittest.TestCase):
    def test_rfc8037_a4_signature(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        key = ed25519.Ed25519PrivateKey.from_private_bytes(
            b64url_decode(RFC8037_D)
        )
        data = signing_input(
            b64url_encode(b'{"alg":"EdDSA"}'), b"Example of Ed25519 signing"
        )
        self.assertEqual(b64url_encode(key.sign(data)), RFC8037_A4_SIG)


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography not installed")
class TestKeygenAndSigning(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generate_keypair_layout_and_permissions(self) -> None:
        report = generate_keypair("claude", self.home)
        key_path = Path(report["key_path"])
        self.assertTrue(key_path.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(key_path).st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(os.stat(key_path.parent).st_mode), 0o700
        )
        stub = json.loads(Path(report["public_stub_path"]).read_text())
        self.assertEqual(jwk_thumbprint(stub["jwk"]), report["kid"])
        self.assertNotIn("d", stub.get("jwk", {}))
        self.assertTrue(report["agent_urn"].startswith("urn:oacp:agent:"))
        self.assertTrue(report["instance_urn"].startswith("urn:uuid:"))

    def test_trust_domain_and_instance_stable(self) -> None:
        first = generate_keypair("claude", self.home)
        second = generate_keypair("codex", self.home)
        self.assertEqual(
            first["agent_urn"].rsplit(":", 1)[0],
            second["agent_urn"].rsplit(":", 1)[0],
        )
        self.assertEqual(first["instance_urn"], second["instance_urn"])

    def test_list_and_load_signers(self) -> None:
        generate_keypair("claude", self.home)
        generate_keypair("claude", self.home)
        self.assertEqual(len(list_keys(self.home, agent="claude")), 2)
        signers = load_signers("claude", self.home)
        self.assertEqual(len(signers), 2)
        with self.assertRaises(SigningUnavailableError):
            load_signers("nokey", self.home)

    def test_signer_rejects_kid_mismatch(self) -> None:
        report = generate_keypair("claude", self.home)
        key_path = Path(report["key_path"])
        record = json.loads(key_path.read_text())
        record["kid"] = "tampered"
        key_path.write_text(json.dumps(record))
        with self.assertRaises(SigningUnavailableError):
            FileKeySigner(key_path)

    def test_signer_rejects_seed_public_key_mismatch(self) -> None:
        # a private seed that does not derive the stored
        # public key must refuse to load, not sign unverifiable messages.
        first = generate_keypair("claude", self.home)
        second = generate_keypair("claude", self.home)
        first_path = Path(first["key_path"])
        record = json.loads(first_path.read_text())
        record["d"] = json.loads(Path(second["key_path"]).read_text())["d"]
        first_path.write_text(json.dumps(record))
        with self.assertRaises(SigningUnavailableError) as ctx:
            FileKeySigner(first_path)
        self.assertIn("does not derive", str(ctx.exception))

    def test_signer_rejects_widened_file_mode(self) -> None:
        # the 0600/0700 permission boundary is enforced on load.
        report = generate_keypair("claude", self.home)
        key_path = Path(report["key_path"])
        os.chmod(key_path, 0o644)
        with self.assertRaises(SigningUnavailableError) as ctx:
            FileKeySigner(key_path)
        self.assertIn("group/world-accessible", str(ctx.exception))

    def test_signer_rejects_widened_directory_mode(self) -> None:
        report = generate_keypair("claude", self.home)
        key_path = Path(report["key_path"])
        os.chmod(key_path.parent, 0o755)
        try:
            with self.assertRaises(SigningUnavailableError) as ctx:
                FileKeySigner(key_path)
            self.assertIn("group/world-accessible", str(ctx.exception))
        finally:
            os.chmod(key_path.parent, 0o700)

    def test_sign_and_verify_round_trip(self) -> None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric import ed25519

        generate_keypair("iris", self.home)
        signers = load_signers("iris", self.home)
        msg = build_message_dict(
            sender="iris", recipient="claude", msg_type="notification",
            subject="Unicode café — bytes as-is",
            body="line1\nline2 with auth: lookalike\n",
        )
        payload = render_yaml(msg).encode("utf-8")
        signed = sign_and_append(payload, signers)

        prefix, auth_value = split_signed_message(signed)
        self.assertEqual(prefix, payload)
        entries = decode_auth_value(auth_value)
        self.assertEqual(len(entries), 1)
        header = validate_protected_header(entries[0]["protected"])
        self.assertEqual(header["kid"], signers[0].kid)

        public_key = ed25519.Ed25519PublicKey.from_public_bytes(
            b64url_decode(signers[0].public_jwk["x"])
        )
        data = signing_input(entries[0]["protected"], prefix)
        public_key.verify(b64url_decode(entries[0]["signature"]), data)

        # One-byte payload tamper must fail verification (byte covenant).
        tampered = prefix[:-2] + b"X\n"
        with self.assertRaises(InvalidSignature):
            public_key.verify(
                b64url_decode(entries[0]["signature"]),
                signing_input(entries[0]["protected"], tampered),
            )

    def test_multi_signature_rotation_overlap(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        generate_keypair("iris", self.home)
        generate_keypair("iris", self.home)
        signers = load_signers("iris", self.home)
        payload = b"id: msg-1\nbody: rotation\n"
        auth_value = sign_payload(payload, signers)
        entries = decode_auth_value(auth_value)
        self.assertEqual(len(entries), 2)
        for signer, entry in zip(signers, entries):
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(
                b64url_decode(signer.public_jwk["x"])
            )
            public_key.verify(
                b64url_decode(entry["signature"]),
                signing_input(entry["protected"], payload),
            )

    def test_sign_payload_bounds(self) -> None:
        with self.assertRaises(SigningUnavailableError):
            sign_payload(b"x\n", [])


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography not installed")
class TestSenderIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.project_dir = self.home / "projects" / "proj"
        (self.project_dir / "agents" / "iris").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _send(self, **kwargs):
        return send_message(
            project="proj",
            sender="iris",
            recipient="claude",
            msg_type="notification",
            subject="Signed hello",
            body="body line 1\nbody line 2",
            oacp_dir=self.home,
            **kwargs,
        )

    def test_default_is_unsigned(self) -> None:
        report = self._send()
        raw = Path(report["inbox_path"]).read_bytes()
        self.assertEqual(split_signed_message(raw), (raw, None))
        self.assertNotIn("signed", report)

    def test_config_knob_signs_and_files_are_byte_identical(self) -> None:
        generate_keypair("iris", self.home)
        config = self.project_dir / "agents" / "iris" / "config.yaml"
        config.write_text("signing:\n  sign_messages: true\n")

        report = self._send()
        self.assertTrue(report.get("signed"))
        inbox_raw = Path(report["inbox_path"]).read_bytes()
        outbox_raw = Path(report["outbox_path"]).read_bytes()
        self.assertEqual(inbox_raw, outbox_raw)

        prefix, auth_value = split_signed_message(inbox_raw)
        self.assertIsNotNone(auth_value)
        self.assertTrue(prefix.endswith(b"\n"))
        self.assertEqual(auth_structure_errors(auth_value), [])
        # The signed file passes full message validation incl. position rule.
        self.assertEqual(validate_message_file(Path(report["inbox_path"])), [])

    def test_on_disk_bytes_equal_signer_input_exactly(self) -> None:
        # the bytes the signature covers must be exactly the
        # bytes on disk — no text-mode translation anywhere after signing.
        from cryptography.hazmat.primitives.asymmetric import ed25519

        from message_signing import (
            b64url_decode,
            decode_auth_value,
            signing_input,
        )

        generate_keypair("iris", self.home)
        signers = load_signers("iris", self.home)
        report = self._send(sign=True)
        on_disk = Path(report["inbox_path"]).read_bytes()
        prefix, auth_value = split_signed_message(on_disk)
        entry = decode_auth_value(auth_value)[0]
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(
            b64url_decode(signers[0].public_jwk["x"])
        )
        # Verifying against the on-disk prefix bytes proves signer input ==
        # written bytes; any translation would fail here.
        public_key.verify(
            b64url_decode(entry["signature"]),
            signing_input(entry["protected"], prefix),
        )

    def test_malformed_config_with_signing_intent_fails_loudly(self) -> None:
        # explicit signing intent + unparsable config must
        # never silently send unsigned.
        config = self.project_dir / "agents" / "iris" / "config.yaml"
        config.write_text(
            "signing:\n  sign_messages: true\n  broken: [unclosed\n"
        )
        with self.assertRaises(ValueError):
            self._send()

    def test_quoted_false_is_rejected_not_truthy(self) -> None:
        # sign_messages: "false" must be a type error, not
        # Python-truthy signing.
        config = self.project_dir / "agents" / "iris" / "config.yaml"
        config.write_text('signing:\n  sign_messages: "false"\n')
        with self.assertRaises(ValueError) as ctx:
            self._send()
        self.assertIn("must be a boolean", str(ctx.exception))

    def test_non_mapping_signing_block_rejected(self) -> None:
        config = self.project_dir / "agents" / "iris" / "config.yaml"
        config.write_text("signing: [sign_messages]\n")
        with self.assertRaises(ValueError):
            self._send()

    def test_non_string_kid_rejected(self) -> None:
        config = self.project_dir / "agents" / "iris" / "config.yaml"
        config.write_text("signing:\n  sign_messages: false\n  kid: 123\n")
        with self.assertRaises(ValueError):
            self._send()

    def test_config_without_signing_block_still_sends(self) -> None:
        config = self.project_dir / "agents" / "iris" / "config.yaml"
        config.write_text("autonomy:\n  default_mode: always_pause\n")
        report = self._send()
        self.assertNotIn("signed", report)

    def test_sign_flag_without_key_fails_loudly(self) -> None:
        with self.assertRaises(SigningUnavailableError):
            self._send(sign=True)

    def test_sign_flag_forces_signing(self) -> None:
        generate_keypair("iris", self.home)
        report = self._send(sign=True)
        self.assertTrue(report.get("signed"))
        self.assertEqual(validate_message_file(Path(report["inbox_path"])), [])

    def test_dry_run_signs_in_memory(self) -> None:
        generate_keypair("iris", self.home)
        report = self._send(sign=True, dry_run=True)
        raw = report["yaml"].encode("utf-8")
        _, auth_value = split_signed_message(raw)
        self.assertIsNotNone(auth_value)

    def test_broadcast_signs_one_blob_for_all_inboxes(self) -> None:
        generate_keypair("iris", self.home)
        report = send_message(
            project="proj",
            sender="iris",
            recipient="claude,codex",
            msg_type="notification",
            subject="Broadcast",
            body="same bytes everywhere",
            oacp_dir=self.home,
            sign=True,
        )
        blobs = {Path(p).read_bytes() for p in report["inbox_paths"]}
        blobs.add(Path(report["outbox_path"]).read_bytes())
        self.assertEqual(len(blobs), 1)


class TestValidatorFraming(unittest.TestCase):
    def _write(self, content: bytes) -> Path:
        handle = tempfile.NamedTemporaryFile(
            suffix=".yaml", delete=False, dir=tempfile.gettempdir()
        )
        handle.write(content)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def _base_message(self) -> bytes:
        msg = build_message_dict(
            sender="iris", recipient="claude", msg_type="notification",
            subject="s", body="b",
        )
        return render_yaml(msg).encode("utf-8")

    def test_unsigned_message_valid(self) -> None:
        path = self._write(self._base_message())
        self.assertEqual(validate_message_file(path), [])

    def test_signed_message_valid(self) -> None:
        signed = append_auth_trailer(self._base_message(), _valid_auth_value())
        path = self._write(signed)
        self.assertEqual(validate_message_file(path), [])

    def test_auth_not_final_line_rejected(self) -> None:
        signed = append_auth_trailer(self._base_message(), _valid_auth_value())
        path = self._write(signed + b"trailing: content\n")
        errors = validate_message_file(path)
        self.assertTrue(any("final physical line" in e for e in errors))

    def test_structurally_bad_auth_rejected(self) -> None:
        bad = self._base_message() + b'auth: "QUJD"\n'
        path = self._write(bad)
        errors = validate_message_file(path)
        self.assertTrue(any("auth" in e for e in errors))

    def test_crlf_signed_message_rejected(self) -> None:
        # byte-different means different message — CRLF
        # line endings must fail framing, not be normalized away by a
        # text-mode read.
        signed = append_auth_trailer(self._base_message(), _valid_auth_value())
        crlf = signed.replace(b"\n", b"\r\n")
        self.assertNotEqual(crlf, signed)
        path = self._write(crlf)
        errors = validate_message_file(path)
        self.assertTrue(
            any("final physical line" in e for e in errors), errors
        )


if __name__ == "__main__":
    unittest.main()
