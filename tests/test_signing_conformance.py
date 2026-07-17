# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Signing conformance runner — dual-parser goldens + tamper-detection suite.

Every fixture under tests/conformance/signing/expected/ is executed against
the committed message bytes:

- both parse paths — ``message_signing.split_signed_message`` (the shared
  framing helper the validator uses) and ``message_verify.classify_auth_trailer``
  (the receiver's tri-state classifier) — must agree byte-for-byte on the
  trailer boundary;
- the extracted prefix must equal the committed ``*.prefix.yaml`` bytes and
  the pinned digests (canonicalization creep anywhere shows up as a byte
  diff);
- the JWS preimages (RFC 7515 signing input over the raw prefix) must match
  the pinned digests;
- ``verify_message`` against the fixture pins must reproduce the pinned
  ``message_auth`` block and warn annotation exactly;
- re-signing each golden prefix with the committed fixture keys must
  reproduce the committed signed bytes byte-for-byte (Ed25519 and the
  signer's JSON emit are deterministic);
- regenerating the whole corpus must be a no-op (`regen_fixtures.py --check`
  as a test) — behavior drift against the committed corpus fails CI.

A failing conformance test after a code change means the wire format moved:
that needs a ruling, not a fixture regen. See
tests/conformance/signing/README.md.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import yaml  # noqa: E402

from message_signing import (  # noqa: E402
    CRYPTO_AVAILABLE,
    sign_and_append,
    split_signed_message,
)
from message_verify import (  # noqa: E402
    annotate,
    classify_auth_trailer,
    load_allowed_signers,
    verify_message,
)
from validate_message import validate_message_file  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "conformance" / "signing"
sys.path.insert(0, str(FIXTURE_DIR))

import regen_fixtures  # noqa: E402

# Pinned enums: any fixture using a value outside these sets fails the
# runner (mirrors the autonomy conformance reason-code pinning).
TRAILER_STATES = {"absent", "ok", "malformed"}
AUTH_STATUSES = {
    "unsigned", "verified", "invalid", "untrusted", "revoked", "unsupported",
}
SIGNATURE_OUTCOMES = {
    "verified", "unknown_kid", "revoked", "pin_agent_mismatch",
    "bad_signature", "identity_mismatch", "unchecked",
}


def load_cases() -> List[Dict[str, Any]]:
    cases = []
    for path in sorted((FIXTURE_DIR / "expected").glob("*.yaml")):
        with open(path, encoding="utf-8") as handle:
            cases.append(yaml.safe_load(handle))
    return cases


CASES = load_cases()
PINS = load_allowed_signers(FIXTURE_DIR / "pins" / "allowed_signers.yaml")


class TestCorpusShape(unittest.TestCase):
    def test_corpus_is_present_and_complete(self) -> None:
        self.assertGreaterEqual(len(CASES), 18)
        spec_cases = {spec["case"] for spec in regen_fixtures.MESSAGE_SPECS}
        tamper_cases = {case for case, _, _ in regen_fixtures.TAMPER_SPECS}
        self.assertEqual(
            {case["case"] for case in CASES}, spec_cases | tamper_cases
        )

    def test_expected_values_use_pinned_enums_only(self) -> None:
        for case in CASES:
            expected = case["expected"]
            self.assertIn(expected["trailer_state"], TRAILER_STATES, case["case"])
            auth = expected["message_auth"]
            self.assertIn(auth["status"], AUTH_STATUSES, case["case"])
            for checked in auth["signatures_checked"]:
                self.assertIn(
                    checked["outcome"], SIGNATURE_OUTCOMES, case["case"]
                )


class TestDualParserBoundary(unittest.TestCase):
    """Both parse paths agree byte-for-byte on every fixture."""

    def _raw(self, case: Dict[str, Any]) -> bytes:
        return (FIXTURE_DIR / case["message"]).read_bytes()

    def test_committed_bytes_match_pinned_digests(self) -> None:
        # First line of defense against EOL/encoding munging of the corpus
        # (checkout filters, editors): the exact on-disk bytes are pinned.
        for case in CASES:
            raw = self._raw(case)
            self.assertEqual(
                hashlib.sha256(raw).hexdigest(),
                case["expected"]["raw_sha256"],
                f"{case['case']}: committed message bytes changed",
            )

    def test_both_parsers_agree_on_the_trailer_boundary(self) -> None:
        for case in CASES:
            raw = self._raw(case)
            expected = case["expected"]
            state, class_prefix, class_auth = classify_auth_trailer(raw)
            split_prefix, split_auth = split_signed_message(raw)
            self.assertEqual(state, expected["trailer_state"], case["case"])
            if state == "ok":
                self.assertEqual(split_prefix, class_prefix, case["case"])
                self.assertEqual(split_auth, class_auth, case["case"])
            else:
                # No defined signed prefix: both parsers return the artifact
                # whole, and the strict splitter reports no trailer.
                self.assertIsNone(split_auth, case["case"])
                self.assertEqual(split_prefix, raw, case["case"])
                self.assertEqual(class_prefix, raw, case["case"])
            self.assertEqual(
                hashlib.sha256(class_prefix).hexdigest(),
                expected["prefix_sha256"],
                case["case"],
            )

    def test_extracted_prefix_equals_committed_prefix_bytes(self) -> None:
        # The boundary is pinned by data, not by the code under test: the
        # signed prefix ships as its own committed artifact.
        for case in CASES:
            if "prefix" not in case:
                continue
            raw = self._raw(case)
            prefix_bytes = (FIXTURE_DIR / case["prefix"]).read_bytes()
            split_prefix, _ = split_signed_message(raw)
            self.assertEqual(split_prefix, prefix_bytes, case["case"])
            self.assertEqual(
                hashlib.sha256(prefix_bytes).hexdigest(),
                case["expected"]["prefix_sha256"],
                case["case"],
            )

    def test_preimages_match_pinned_digests(self) -> None:
        # RFC 7515 signing input over the exact raw prefix — byte-stable.
        # Preimages are pinned by forensic EXTRACTION, not the acceptance
        # gate: the signing input is a pure function of each protected value
        # and the signed prefix, so canonicality rejection of the trailer
        # spelling does not undefine it (see
        # regen_fixtures.extract_preimage_sha256s).
        for case in CASES:
            raw = self._raw(case)
            self.assertEqual(
                regen_fixtures.extract_preimage_sha256s(raw),
                case["expected"]["signing_input_sha256"],
                case["case"],
            )

    def test_alias_vectors_retain_signed_basic_preimages(self) -> None:
        # The encoding-alias/formatting vectors change the trailer SPELLING,
        # never the signing input: their pinned preimages must equal
        # signed_basic's, and must not be empty.
        by_case = {case["case"]: case for case in CASES}
        basic = by_case["signed_basic"]["expected"]["signing_input_sha256"]
        self.assertTrue(basic)
        for name in (
            "tamper_sig_alias",
            "tamper_auth_alias",
            "tamper_container_reformat",
        ):
            self.assertEqual(
                by_case[name]["expected"]["signing_input_sha256"], basic, name
            )

    def test_validator_path_agrees(self) -> None:
        # The third consumer: validate_message_file cross-checks the
        # YAML-parsed auth value against the raw trailer bytes.
        for case in CASES:
            with tempfile.TemporaryDirectory() as tmp:
                scratch = Path(tmp) / "message.yaml"
                scratch.write_bytes(self._raw(case))
                self.assertEqual(
                    validate_message_file(scratch),
                    case["expected"]["validate_errors"],
                    case["case"],
                )

    def test_renderer_reproduces_committed_prefix_bytes(self) -> None:
        # Render-stability golden: the pinned message specs must render to
        # the committed prefix bytes. A diff here is renderer output change
        # — a wire-format event needing a ruling, not a regen.
        spec_by_case = {s["case"]: s for s in regen_fixtures.MESSAGE_SPECS}
        for case in CASES:
            spec = spec_by_case.get(case["case"])
            if spec is None:
                continue
            committed = (
                FIXTURE_DIR / case.get("prefix", case["message"])
            ).read_bytes()
            self.assertEqual(
                regen_fixtures.render_prefix(spec), committed, case["case"]
            )


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography not installed")
class TestVerifyOutcomes(unittest.TestCase):
    """verify_message reproduces every pinned message_auth block exactly."""

    def test_message_auth_blocks_match(self) -> None:
        for case in CASES:
            raw = (FIXTURE_DIR / case["message"]).read_bytes()
            auth = verify_message(raw, PINS)
            observed_at = auth.pop("verified_at_utc")
            self.assertTrue(observed_at, case["case"])
            self.assertEqual(
                auth, case["expected"]["message_auth"], case["case"]
            )

    def test_warn_annotations_match(self) -> None:
        for case in CASES:
            raw = (FIXTURE_DIR / case["message"]).read_bytes()
            auth = verify_message(raw, PINS)
            auth.pop("verified_at_utc")
            self.assertEqual(
                annotate(auth), case["expected"]["annotation"], case["case"]
            )

    def test_every_tamper_vector_is_flagged(self) -> None:
        # The tamper contract in one assertion: no tampered artifact may
        # annotate as clean. (tamper_indented_trailer is the deliberate
        # exception — an indented auth-lookalike is payload content by the
        # column-0 rule, so the artifact reads as unsigned.)
        for case in CASES:
            name = case["case"]
            if not name.startswith("tamper_"):
                continue
            status = case["expected"]["message_auth"]["status"]
            if name == "tamper_indented_trailer":
                self.assertEqual(status, "unsigned", name)
                continue
            self.assertIn(status, {"invalid", "untrusted", "revoked"}, name)


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography not installed")
class TestSignerDeterminism(unittest.TestCase):
    """Re-signing each golden prefix reproduces the committed bytes."""

    def test_resign_is_byte_identical(self) -> None:
        records = regen_fixtures.load_key_records()
        for case in CASES:
            if "signers" not in case:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                signers = [
                    regen_fixtures.signer_for(records[name], Path(tmp))
                    for name in case["signers"]
                ]
                prefix = (FIXTURE_DIR / case["prefix"]).read_bytes()
                committed = (FIXTURE_DIR / case["message"]).read_bytes()
                self.assertEqual(
                    sign_and_append(prefix, signers), committed, case["case"]
                )

    def test_regen_check_is_clean(self) -> None:
        # The full corpus regenerates byte-identically: fixture drift or
        # behavior drift against committed goldens fails CI here.
        files = regen_fixtures.build_corpus()
        drift = [
            str(path.relative_to(FIXTURE_DIR))
            for path, content in sorted(files.items())
            if not path.is_file() or path.read_bytes() != content
        ]
        self.assertEqual(
            drift, [],
            "regenerated corpus differs from committed fixtures — a golden "
            "change is a wire-format change and needs a ruling (see "
            "tests/conformance/signing/README.md)",
        )


class TestRegenGuards(unittest.TestCase):
    """The regeneration tool's guard rails, exercised through the real CLI
    against an isolated corpus copy (never the committed one)."""

    def _snapshot(self, root: Path) -> Dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def _run_regen(self, corpus: Path, *args: str) -> "subprocess.CompletedProcess[str]":
        env = dict(os.environ)
        env["OACP_SIGNING_FIXTURE_DIR"] = str(corpus)
        return subprocess.run(
            [sys.executable, str(FIXTURE_DIR / "regen_fixtures.py"), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_mint_keys_refuses_established_corpus(self) -> None:
        # A populated corpus with one key deleted must NOT be silently
        # reseeded: --write --mint-keys has to fail before writing anything,
        # byte-for-byte. (A reseeded corpus passes its own --check, so the
        # refusal is the only thing standing between a lost key and a
        # self-blessed rewrite of the normative goldens.)
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            for name in ("keys", "pins", "messages", "expected"):
                shutil.copytree(FIXTURE_DIR / name, corpus / name)
            (corpus / "keys" / "alice.json").unlink()
            before = self._snapshot(corpus)

            result = self._run_regen(corpus, "--write", "--mint-keys")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("REFUSING --mint-keys", result.stdout)
            self.assertEqual(self._snapshot(corpus), before)

            # Plain --write must refuse too: the corpus cannot re-sign
            # itself without every committed key.
            result = self._run_regen(corpus, "--write")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self._snapshot(corpus), before)

    def test_mint_keys_refuses_even_a_single_leftover_artifact(self) -> None:
        # All-or-nothing means ANY artifact blocks bootstrap, not just keys.
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            (corpus / "pins").mkdir(parents=True)
            shutil.copy(
                FIXTURE_DIR / "pins" / "allowed_signers.yaml",
                corpus / "pins" / "allowed_signers.yaml",
            )
            before = self._snapshot(corpus)
            result = self._run_regen(corpus, "--write", "--mint-keys")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("REFUSING --mint-keys", result.stdout)
            self.assertEqual(self._snapshot(corpus), before)


if __name__ == "__main__":
    unittest.main()
