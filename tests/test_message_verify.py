# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for message_verify.py — receiver verify-before-parse (warn mode).

Covers the receiver half: extraction boundary cases, verify outcomes against
receiver-local pins, warn annotations, no-clobber quarantine, the
verify_mode knob, and message_auth audit recording. Test keys are ephemeral
per-test fixtures — no key material outside temp dirs.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import message_verify  # noqa: E402
from message_signing import (  # noqa: E402
    CRYPTO_AVAILABLE,
    MAX_AUTH_VALUE_CHARS,
    generate_keypair,
    jwk_thumbprint,
    load_signers,
    sign_and_append,
    split_signed_message,
)
from message_verify import (  # noqa: E402
    STATUS_INVALID,
    STATUS_REVOKED,
    STATUS_UNSIGNED,
    STATUS_UNSUPPORTED,
    STATUS_UNTRUSTED,
    STATUS_VERIFIED,
    TrustRootError,
    annotate,
    attach_message_auth,
    load_allowed_signers,
    load_verify_mode,
    quarantine_write_aside,
    verify_message,
)
from send_inbox_message import build_message_dict, render_yaml  # noqa: E402


def _render_message(sender: str = "iris") -> bytes:
    msg = build_message_dict(
        sender=sender, recipient="claude", msg_type="notification",
        subject="Verify me", body="line1\nline2",
    )
    return render_yaml(msg).encode("utf-8")


class TestVerifyModeKnob(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_config(self, content: str) -> Path:
        path = self.dir / "config.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_default_off(self) -> None:
        self.assertEqual(load_verify_mode(self.dir / "missing.yaml"), "off")
        self.assertEqual(load_verify_mode(self._write_config("autonomy: {}\n")), "off")

    def test_modes(self) -> None:
        self.assertEqual(
            load_verify_mode(self._write_config("signing:\n  verify_mode: warn\n")),
            "warn",
        )
        self.assertEqual(
            load_verify_mode(self._write_config("signing:\n  verify_mode: off\n")),
            "off",
        )

    def test_enforce_degrades_to_warn_never_rejecting(self) -> None:
        self.assertEqual(
            load_verify_mode(self._write_config("signing:\n  verify_mode: enforce\n")),
            "warn",
        )

    def test_garbage_and_malformed_degrade_to_off(self) -> None:
        self.assertEqual(
            load_verify_mode(self._write_config("signing:\n  verify_mode: loud\n")),
            "off",
        )
        self.assertEqual(
            load_verify_mode(self._write_config(":\n  - not yaml: [\n")), "off"
        )


class TestAllowedSignersStub(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "allowed_signers.yaml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_means_no_pins(self) -> None:
        self.assertEqual(load_allowed_signers(self.path), {})

    # RFC 8037 A.3 Ed25519 public key — published test vector, safe fixture.
    GOLDEN_X = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
    GOLDEN_JWK = f"{{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}"
    GOLDEN_KID = jwk_thumbprint({"kty": "OKP", "crv": "Ed25519", "x": GOLDEN_X})
    # canonical placeholder (final char carries zero unused pad bits)
    KID_B = "b" * 42 + "w"
    # non-canonical alias: same 32 key bytes, distinct spelling and kid —
    # built by setting an unused pad bit in the canonical final character.
    GOLDEN_X_ALIAS = GOLDEN_X[:-1] + "p"
    GOLDEN_KID_ALIAS = jwk_thumbprint(
        {"kty": "OKP", "crv": "Ed25519", "x": GOLDEN_X_ALIAS}
    )

    def test_valid_pins(self) -> None:
        self.path.write_text(
            "version: 1\n"
            "signers:\n"
            "  - agent: iris\n"
            "    domain: d-1\n"
            "    instance: i-1\n"
            f"    kid: {self.GOLDEN_KID}\n"
            f"    jwk: {self.GOLDEN_JWK}\n"
            "    status: active\n"
            "  - agent: codex\n"
            f"    kid: {self.KID_B}\n"
            "    status: revoked\n"
        )
        pins = load_allowed_signers(self.path)
        self.assertEqual(set(pins), {self.GOLDEN_KID, self.KID_B})
        self.assertEqual(pins[self.GOLDEN_KID]["agent"], "iris")
        self.assertEqual(pins[self.KID_B]["status"], "revoked")

    def test_rejections(self) -> None:
        kid = self.GOLDEN_KID
        jwk = self.GOLDEN_JWK
        cases = [
            "version: 99\nsigners: []\n",
            "version: 1\nsigners:\n  - agent: iris\n    status: active\n",
            f"version: 1\nsigners:\n  - kid: {kid}\n    status: active\n",
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {kid}, status: odd, jwk: {jwk}}}\n"
            ),
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {kid}, status: active, jwk: {{kty: RSA}}}}\n"
            ),
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {kid}, status: revoked}}\n"
                f"  - {{agent: iris, kid: {kid}, status: revoked}}\n"
            ),
            "just a string\n",
            # kid must be a well-formed RFC 7638 thumbprint
            "version: 1\nsigners:\n  - {agent: iris, kid: kid-1, status: revoked}\n",
            f"version: 1\nsigners:\n  - {{agent: iris, kid: '{'x' * 44}', status: revoked}}\n",
            # the reader is the verify boundary: a shape-valid kid that is
            # NOT the thumbprint of the pinned jwk must make the file
            # unusable — verification would otherwise trust the mapping.
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {'a' * 43}, status: active, jwk: {jwk}}}\n"
            ),
            # private key material makes the trust file unusable, active or
            # revoked
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {kid}, status: active, "
                f"jwk: {{kty: OKP, crv: Ed25519, x: {self.GOLDEN_X}, d: QUJDQQ}}}}\n"
            ),
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {kid}, status: revoked, "
                f"jwk: {{kty: OKP, crv: Ed25519, x: {self.GOLDEN_X}, d: QUJDQQ}}}}\n"
            ),
            # x must decode to exactly 32 bytes
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {kid}, status: active, "
                "jwk: {kty: OKP, crv: Ed25519, x: QUJD}}\n"
            ),
            # reserved columns are shape-checked (string or null only)
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {kid}, status: revoked, domain: 7}}\n"
            ),
            (
                "version: 1\nsigners:\n"
                f"  - {{agent: iris, kid: {kid}, status: revoked, instance: [u]}}\n"
            ),
        ]
        for content in cases:
            self.path.write_text(content)
            with self.assertRaises(TrustRootError, msg=content):
                load_allowed_signers(self.path)

    def test_non_canonical_x_alias_makes_pins_unusable(self) -> None:
        # same key bytes under a second spelling would give a revoked key
        # a fresh active kid; the reader is the verify boundary and must
        # reject the alias even when its kid is self-consistent.
        from message_signing import b64url_decode

        self.assertNotEqual(self.GOLDEN_X_ALIAS, self.GOLDEN_X)
        self.assertEqual(
            b64url_decode(self.GOLDEN_X_ALIAS), b64url_decode(self.GOLDEN_X)
        )
        self.path.write_text(
            "version: 1\nsigners:\n"
            f"  - {{agent: iris, kid: {self.GOLDEN_KID_ALIAS}, status: active, "
            f"jwk: {{kty: OKP, crv: Ed25519, x: {self.GOLDEN_X_ALIAS}}}}}\n"
        )
        with self.assertRaises(TrustRootError):
            load_allowed_signers(self.path)

    def test_alias_kid_in_revoked_pin_makes_pins_unusable(self) -> None:
        # Regression: a revoked entry may drop its jwk, which
        # used to leave the kid regex-only validated. An alias spelling
        # then loaded fine but could never match the canonical kid at pin
        # lookup — the revocation silently missed (signed-unknown-kid
        # instead of signed-REVOKED). The load must fail loudly instead.
        # build a true pad-bit alias of the canonical kid
        from message_signing import b64url_decode, b64url_encode

        raw = b64url_decode(self.GOLDEN_KID)
        alphabet = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        alias = self.GOLDEN_KID[:-1] + alphabet[
            alphabet.index(self.GOLDEN_KID[-1]) | 1
        ]
        self.assertNotEqual(alias, self.GOLDEN_KID)
        self.assertEqual(b64url_decode(alias), raw)
        self.assertEqual(b64url_encode(raw), self.GOLDEN_KID)
        self.path.write_text(
            "version: 1\n"
            "signers:\n"
            "  - agent: iris\n"
            f"    kid: {alias}\n"
            "    status: revoked\n"
        )
        with self.assertRaises(TrustRootError):
            load_allowed_signers(self.path)

    def test_revoked_pin_may_keep_valid_jwk(self) -> None:
        self.path.write_text(
            "version: 1\nsigners:\n"
            f"  - {{agent: iris, kid: {self.GOLDEN_KID}, status: revoked, "
            f"jwk: {self.GOLDEN_JWK}}}\n"
        )
        pins = load_allowed_signers(self.path)
        self.assertEqual(pins[self.GOLDEN_KID]["status"], "revoked")

    def test_reserved_columns_accept_null_and_absent(self) -> None:
        self.path.write_text(
            "version: 1\nsigners:\n"
            f"  - {{agent: iris, kid: {self.KID_B}, status: revoked, domain: null}}\n"
        )
        pins = load_allowed_signers(self.path)
        self.assertIsNone(pins[self.KID_B]["domain"])
        self.assertIsNone(pins[self.KID_B]["instance"])


class TestUnsignedAndFraming(unittest.TestCase):
    def test_unsigned_message(self) -> None:
        result = verify_message(_render_message(), {})
        self.assertEqual(result["status"], STATUS_UNSIGNED)
        self.assertIsNotNone(result["payload_sha256"])

    def test_extraction_boundary_cases_stay_unsigned(self) -> None:
        base = _render_message()
        candidates = [
            base[:-1],                                   # no final LF, no trailer
            b'auth: "QUJD"\n' + base,                    # lookalike first line
        ]
        for raw in candidates:
            self.assertEqual(verify_message(raw, {})["status"], STATUS_UNSIGNED)

    def test_malformed_trailer_bytes_are_invalid_not_unsigned(self) -> None:
        # a present-but-malformed final auth trailer is a
        # byte-tamper signal — it must classify signed-INVALID, never blend
        # into unsigned telemetry.
        base = _render_message()
        good_line = b'auth: "QUJD"\n'
        candidates = [
            (base + good_line)[:-1],                 # final LF removed
            base + b'auth: "QUJD" \n',               # space before the LF
            base + b'auth: "QUJD"\r\n',              # CRLF trailer
            base + good_line + b"\n",                # trailing blank line
            base
            + b'auth: "'
            + b"A" * (MAX_AUTH_VALUE_CHARS + 1)
            + b'"\n',                                # oversized auth value
        ]
        for raw in candidates:
            result = verify_message(raw, {})
            self.assertEqual(result["status"], STATUS_INVALID, raw[-60:])
            self.assertIn("auth framing", result["reason"])

    def test_mid_body_lookalike_is_payload(self) -> None:
        msg = build_message_dict(
            sender="iris", recipient="claude", msg_type="notification",
            subject="s", body='auth: "QUJD"\nmore',
        )
        raw = render_yaml(msg).encode("utf-8")
        self.assertEqual(verify_message(raw, {})["status"], STATUS_UNSIGNED)

    def test_malformed_trailer_is_invalid(self) -> None:
        raw = _render_message() + b'auth: "QUJD"\n'
        result = verify_message(raw, {})
        self.assertEqual(result["status"], STATUS_INVALID)
        self.assertIn("auth framing", result["reason"])

    def test_oversized_message_is_invalid(self) -> None:
        raw = b"a" * (message_verify.MAX_MESSAGE_BYTES + 1)
        self.assertEqual(verify_message(raw, {})["status"], STATUS_INVALID)


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography not installed")
class TestVerifyOutcomes(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.report = generate_keypair("iris", self.home)
        self.signers = load_signers("iris", self.home)
        self.raw = sign_and_append(_render_message(), self.signers)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _pins(self, status: str = "active", agent: str = "iris") -> dict:
        stub = json.loads(Path(self.report["public_stub_path"]).read_text())
        return {
            stub["kid"]: {
                "agent": agent,
                "domain": None,
                "instance": None,
                "jwk": stub["jwk"],
                "status": status,
            }
        }

    def test_signed_verified(self) -> None:
        result = verify_message(self.raw, self._pins(), trust_source="pins.yaml")
        self.assertEqual(result["status"], STATUS_VERIFIED)
        self.assertEqual(result["claimed_sender"], "iris")
        self.assertEqual(result["kid"], self.report["kid"])
        self.assertEqual(result["verified_signer"], self.report["agent_urn"])
        self.assertIsNone(result["reason"])
        self.assertEqual(
            [c["outcome"] for c in result["signatures_checked"]], ["verified"]
        )

    def test_signed_unknown_kid(self) -> None:
        result = verify_message(self.raw, {})
        self.assertEqual(result["status"], STATUS_UNTRUSTED)
        self.assertEqual(
            [c["outcome"] for c in result["signatures_checked"]], ["unknown_kid"]
        )

    def test_trust_error_stays_visible_on_signed_traffic(self) -> None:
        # A pins file that fails to load leaves the caller with zero pins,
        # so every signed message lands in the unknown-kid branch — the
        # load failure is the actual finding and must not be masked by the
        # generic no-matching-pin reason.
        result = verify_message(
            self.raw, {}, trust_error="pin file pins.yaml: signers[0]: bad kid"
        )
        self.assertEqual(result["status"], STATUS_UNTRUSTED)
        self.assertIn("trust root unusable", result["reason"])
        self.assertIn("signers[0]: bad kid", result["reason"])

    def test_signed_invalid_on_byte_tamper(self) -> None:
        tampered = self.raw.replace(b"Verify me", b"Verify ME", 1)
        self.assertNotEqual(tampered, self.raw)
        result = verify_message(tampered, self._pins())
        self.assertEqual(result["status"], STATUS_INVALID)
        self.assertEqual(
            [c["outcome"] for c in result["signatures_checked"]], ["bad_signature"]
        )

    def test_revoked_pin(self) -> None:
        result = verify_message(self.raw, self._pins(status="revoked"))
        self.assertEqual(result["status"], STATUS_REVOKED)
        self.assertEqual(
            [c["outcome"] for c in result["signatures_checked"]], ["revoked"]
        )

    def test_pin_agent_mismatch_is_invalid(self) -> None:
        result = verify_message(self.raw, self._pins(agent="codex"))
        self.assertEqual(result["status"], STATUS_INVALID)
        self.assertEqual(
            [c["outcome"] for c in result["signatures_checked"]],
            ["pin_agent_mismatch"],
        )

    def test_identity_mismatch_between_signer_and_from(self) -> None:
        forged = sign_and_append(_render_message(sender="mallory"), self.signers)
        result = verify_message(forged, self._pins())
        self.assertEqual(result["status"], STATUS_INVALID)
        self.assertIn("identity mismatch", result["reason"])
        self.assertEqual(result["claimed_sender"], "mallory")
        self.assertEqual(
            [c["outcome"] for c in result["signatures_checked"]],
            ["identity_mismatch"],
        )

    def test_signed_message_byte_mutations_on_trailer_are_invalid(self) -> None:
        # malformed-trailer boundary shapes, on a REAL signed message.
        candidates = [
            self.raw[:-1],                          # final LF removed
            self.raw[:-1] + b" \n",                 # space injected before LF
            self.raw[:-1] + b"\r\n",                # trailer LF -> CRLF
        ]
        for raw in candidates:
            result = verify_message(raw, self._pins())
            self.assertEqual(result["status"], STATUS_INVALID)
            self.assertIn("auth framing", result["reason"])

    def test_non_canonical_signature_spelling_is_invalid(self) -> None:
        # Encoding alias on a REAL signed message: an unused pad bit set in
        # the signature encoding's final character decodes to the identical
        # 64 signature bytes, so crypto alone would accept it — the
        # canonical-encoding boundary must reject it first.
        from message_signing import b64url_decode, b64url_encode, decode_auth_value

        alphabet = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        prefix, auth_value = split_signed_message(self.raw)
        entries = decode_auth_value(auth_value)
        sig = entries[0]["signature"]
        aliased = sig[:-1] + alphabet[alphabet.index(sig[-1]) | 1]
        self.assertNotEqual(aliased, sig)
        self.assertEqual(b64url_decode(aliased), b64url_decode(sig))
        entries[0] = {"protected": entries[0]["protected"], "signature": aliased}
        container = json.dumps(
            {"signatures": entries}, sort_keys=True, separators=(",", ":")
        )
        raw = prefix + (
            f'auth: "{b64url_encode(container.encode("utf-8"))}"\n'.encode("ascii")
        )
        result = verify_message(raw, self._pins())
        self.assertEqual(result["status"], STATUS_INVALID)
        self.assertIn("auth framing", result["reason"])
        self.assertIn("canonical", result["reason"])

    def test_multi_agent_or_acceptance_is_order_independent(self) -> None:
        # a pinned co-signer's valid signature must not
        # mask the sender's own valid signature by array position.
        mallory = generate_keypair("mallory", self.home)
        mallory_signer = load_signers("mallory", self.home)[0]
        stub = json.loads(Path(mallory["public_stub_path"]).read_text())
        pins = self._pins()
        pins[stub["kid"]] = {
            "agent": "mallory",
            "domain": None,
            "instance": None,
            "jwk": stub["jwk"],
            "status": "active",
        }
        payload = _render_message()  # from: iris
        for order in (
            [mallory_signer, self.signers[0]],
            [self.signers[0], mallory_signer],
        ):
            raw = sign_and_append(payload, order)
            result = verify_message(raw, pins)
            self.assertEqual(result["status"], STATUS_VERIFIED, order)
            self.assertEqual(result["verified_signer"], self.report["agent_urn"])
            self.assertEqual(result["kid"], self.report["kid"])
            outcomes = sorted(c["outcome"] for c in result["signatures_checked"])
            self.assertEqual(outcomes, ["identity_mismatch", "verified"])

    def test_all_crypto_verified_but_none_match_from_is_invalid(self) -> None:
        forged = sign_and_append(_render_message(sender="codex"), self.signers)
        result = verify_message(forged, self._pins())
        self.assertEqual(result["status"], STATUS_INVALID)
        self.assertIn("identity mismatch", result["reason"])
        self.assertEqual(
            [c["outcome"] for c in result["signatures_checked"]],
            ["identity_mismatch"],
        )

    def test_multi_signature_or_acceptance(self) -> None:
        generate_keypair("iris", self.home)
        signers = load_signers("iris", self.home)
        self.assertEqual(len(signers), 2)
        raw = sign_and_append(_render_message(), signers)
        # Pin only the ORIGINAL key: one unknown_kid + one verified → verified.
        result = verify_message(raw, self._pins())
        self.assertEqual(result["status"], STATUS_VERIFIED)
        outcomes = sorted(c["outcome"] for c in result["signatures_checked"])
        self.assertEqual(outcomes, ["unknown_kid", "verified"])

    def test_unsupported_without_crypto(self) -> None:
        with mock.patch.object(message_verify, "CRYPTO_AVAILABLE", False):
            result = verify_message(self.raw, self._pins())
        self.assertEqual(result["status"], STATUS_UNSUPPORTED)
        self.assertEqual(
            [c["outcome"] for c in result["signatures_checked"]], ["unchecked"]
        )

    def test_annotations(self) -> None:
        self.assertIn(
            "signed-verified", annotate(verify_message(self.raw, self._pins()))
        )
        self.assertIn("signed-unknown-kid", annotate(verify_message(self.raw, {})))
        tampered = self.raw.replace(b"Verify me", b"Verify ME", 1)
        self.assertIn(
            "signed-INVALID", annotate(verify_message(tampered, self._pins()))
        )
        self.assertIn("unsigned", annotate(verify_message(_render_message(), {})))


class TestQuarantine(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.inbox = self.dir / "inbox"
        self.inbox.mkdir()
        self.message_path = self.inbox / "20260712_msg.yaml"
        self.message_path.write_bytes(b"original bytes\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_aside_never_touches_original(self) -> None:
        raw = self.message_path.read_bytes()
        dead_letter = self.dir / "dead_letter"
        copy_path = quarantine_write_aside(raw, self.message_path, dead_letter)
        self.assertTrue(copy_path.is_file())
        self.assertEqual(copy_path.read_bytes(), raw)
        self.assertEqual(self.message_path.read_bytes(), b"original bytes\n")
        self.assertEqual(copy_path.parent, dead_letter)

    def test_no_clobber_on_repeat(self) -> None:
        raw = self.message_path.read_bytes()
        dead_letter = self.dir / "dead_letter"
        first = quarantine_write_aside(raw, self.message_path, dead_letter)
        second = quarantine_write_aside(raw, self.message_path, dead_letter)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), raw)
        self.assertEqual(second.read_bytes(), raw)


class TestAttachMessageAuth(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.audit_path = Path(self._tmp.name) / "audit.yaml"
        self.audit_path.write_text(
            "schema_version: 2\n"
            "decision: auto_accepted\n"
            "result:\n"
            "  final_state: pending\n"
        )
        self.block = {
            "status": STATUS_VERIFIED,
            "kid": "kid-1",
            "signatures_checked": [],
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_attach_and_refuse_overwrite(self) -> None:
        import yaml

        attach_message_auth(self.audit_path, self.block)
        loaded = yaml.safe_load(self.audit_path.read_text())
        self.assertEqual(loaded["result"]["message_auth"]["status"], STATUS_VERIFIED)
        self.assertEqual(loaded["schema_version"], 2)

        with self.assertRaises(ValueError):
            attach_message_auth(self.audit_path, dict(self.block, kid="kid-2"))

        attach_message_auth(
            self.audit_path, dict(self.block, kid="kid-2"), replace=True
        )
        loaded = yaml.safe_load(self.audit_path.read_text())
        self.assertEqual(loaded["result"]["message_auth"]["kid"], "kid-2")

    def test_attach_requires_result_mapping(self) -> None:
        self.audit_path.write_text("schema_version: 2\nresult: nope\n")
        with self.assertRaises(ValueError):
            attach_message_auth(self.audit_path, self.block)

    def test_eight_process_attach_yields_exactly_one_winner(self) -> None:
        # the lock must live on a stable sibling lockfile;
        # concurrent non-replace attaches must produce one success and seven
        # refuse-overwrite errors, never silent mutual overwrites.
        import subprocess

        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        script = (
            "import sys\n"
            f"sys.path.insert(0, {scripts_dir!r})\n"
            "from message_verify import attach_message_auth\n"
            "try:\n"
            f"    attach_message_auth({str(self.audit_path)!r},\n"
            "        {'status': 'verified', 'kid': sys.argv[1]})\n"
            "    print('OK')\n"
            "except ValueError:\n"
            "    print('REFUSED')\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, f"kid-{i}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for i in range(8)
        ]
        outcomes = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())
            outcomes.append(out.strip().decode())
        self.assertEqual(outcomes.count("OK"), 1, outcomes)
        self.assertEqual(outcomes.count("REFUSED"), 7, outcomes)

        import yaml

        final = yaml.safe_load(self.audit_path.read_text())
        self.assertEqual(final["result"]["message_auth"]["status"], "verified")
        self.assertTrue(final["result"]["message_auth"]["kid"].startswith("kid-"))


class TestCrossWriterAuditLock(unittest.TestCase):
    """message_auth and human_outcome writers must
    serialize on the ONE shared stable audit lock — concurrent recording
    must preserve both additive result blocks."""

    PAUSED_AUDIT = (
        "schema_version: 2\n"
        'spec_version: "0.3.5"\n'
        'created_at_utc: "2026-07-12T22:00:00Z"\n'
        "receiver: claude\n"
        "sender: iris\n"
        "message_id: msg-crosswriter-test\n"
        "message_type: task_request\n"
        "decision: paused\n"
        "mode: auto_review\n"
        "reason_codes:\n"
        "- estimated_minutes_exceeds_threshold\n"
        "result:\n"
        "  final_state: pending\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.scripts_dir = Path(__file__).resolve().parent.parent / "scripts"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_concurrent_writers_preserve_both_blocks(self) -> None:
        import subprocess

        import yaml

        attach_template = (
            "import sys\n"
            f"sys.path.insert(0, {str(self.scripts_dir)!r})\n"
            "from message_verify import attach_message_auth\n"
            "attach_message_auth(sys.argv[1], {'status': 'verified', 'kid': 'k'})\n"
        )
        for trial in range(3):
            audit_path = self.dir / f"audit-{trial}.yaml"
            audit_path.write_text(self.PAUSED_AUDIT, encoding="utf-8")
            attach_proc = subprocess.Popen(
                [sys.executable, "-c", attach_template, str(audit_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            outcome_proc = subprocess.Popen(
                [
                    sys.executable,
                    str(self.scripts_dir / "record_autonomy_outcome.py"),
                    str(audit_path),
                    "--decision",
                    "approved",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for proc in (attach_proc, outcome_proc):
                _, err = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, err.decode())
            final = yaml.safe_load(audit_path.read_text())
            self.assertEqual(
                final["result"]["message_auth"]["status"], "verified",
                f"trial {trial}: message_auth block lost",
            )
            self.assertTrue(
                final["result"]["human_outcome"]["recorded"],
                f"trial {trial}: human_outcome block lost",
            )


class TestBoundedRead(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_message_bounded_caps_allocation(self) -> None:
        # the 1 MiB bound caps bytes READ, not just what
        # gets processed after an unbounded allocation.
        big_path = self.dir / "big.yaml"
        big_path.write_bytes(b"a" * (message_verify.MAX_MESSAGE_BYTES + 4096))
        raw = message_verify.read_message_bounded(big_path)
        self.assertEqual(len(raw), message_verify.MAX_MESSAGE_BYTES + 1)
        result = verify_message(raw, {})
        self.assertEqual(result["status"], STATUS_INVALID)
        self.assertIn("size bound", result["reason"])

    def test_cli_oversized_file_is_invalid_via_bounded_read(self) -> None:
        # CLI wiring regression: main() reads through read_message_bounded,
        # so an oversized inbox artifact reports invalid at exit 3 (the
        # helper test above proves the read itself is capped).
        import io
        import json as json_mod
        from contextlib import redirect_stdout

        big_path = self.dir / "huge.yaml"
        big_path.write_bytes(b"a" * (message_verify.MAX_MESSAGE_BYTES + 4096))
        buffer = io.StringIO()
        with mock.patch.object(
            sys, "argv", ["message_verify.py", str(big_path), "--json"]
        ):
            with redirect_stdout(buffer):
                code = message_verify.main()
        self.assertEqual(code, 3)
        payload = json_mod.loads(buffer.getvalue())
        self.assertEqual(payload["status"], STATUS_INVALID)
        self.assertIn("size bound", payload["reason"])


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography not installed")
class TestVerifyCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.report = generate_keypair("iris", self.home)
        signers = load_signers("iris", self.home)
        self.inbox = self.home / "inbox"
        self.inbox.mkdir()
        self.message_path = self.inbox / "msg.yaml"
        self.message_path.write_bytes(sign_and_append(_render_message(), signers))

        stub = json.loads(Path(self.report["public_stub_path"]).read_text())
        self.pins_path = self.home / "allowed_signers.yaml"
        self.pins_path.write_text(
            "version: 1\n"
            "signers:\n"
            f"  - agent: iris\n"
            f"    kid: {stub['kid']}\n"
            f"    jwk: {{kty: OKP, crv: Ed25519, x: {stub['jwk']['x']}}}\n"
            "    status: active\n"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, argv) -> tuple:
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with mock.patch.object(sys, "argv", ["message_verify.py", *argv]):
            with redirect_stdout(buffer):
                code = message_verify.main()
        return code, buffer.getvalue()

    def test_cli_verified_json(self) -> None:
        code, out = self._run(
            [str(self.message_path), "--pins", str(self.pins_path), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], STATUS_VERIFIED)

    def test_cli_invalid_quarantines_and_exits_3(self) -> None:
        tampered_path = self.inbox / "tampered.yaml"
        tampered_path.write_bytes(
            self.message_path.read_bytes().replace(b"Verify me", b"Verify ME", 1)
        )
        code, out = self._run(
            [
                str(tampered_path),
                "--pins", str(self.pins_path),
                "--quarantine", "--json",
            ]
        )
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["status"], STATUS_INVALID)
        quarantine_copy = Path(payload["quarantine_copy"])
        self.assertTrue(quarantine_copy.is_file())
        # Original stays in place, untouched (warn mode never rejects).
        self.assertTrue(tampered_path.is_file())
        self.assertEqual(quarantine_copy.read_bytes(), tampered_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
