# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for trust_root.py — project catalog, trust import, drift.

Covers catalog reader format-version handling, public-stub validation,
import round-trip (catalog + pins, idempotency, conflict/revocation refusal,
private-component refusal), catalog-vs-pins drift detection, and the
end-to-end key gen → import → verify path when crypto is available. Test
keys are ephemeral per-test fixtures — no key material outside temp dirs.
The stub fixtures use the RFC 8037 A.3 public key (a published test vector,
not a real identity).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from message_signing import (  # noqa: E402
    CRYPTO_AVAILABLE,
    AuthFormatError,
    generate_keypair,
    jwk_thumbprint,
    load_signers,
    sign_and_append,
)
from message_verify import (  # noqa: E402
    STATUS_VERIFIED,
    TrustRootError,
    load_allowed_signers,
    verify_message,
)
from trust_root import (  # noqa: E402
    CATALOG_RELPATH,
    DRIFT_ERROR,
    DRIFT_WARN,
    TrustImportError,
    TrustRevokeError,
    drift_report,
    has_trust_root,
    import_public_stub,
    load_catalog,
    load_public_stub,
    revoke_pin,
    write_catalog,
)
from send_inbox_message import build_message_dict, render_yaml  # noqa: E402

# RFC 8037 A.3 Ed25519 public key — published test vector, safe fixture.
GOLDEN_X = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
GOLDEN_JWK = {"kty": "OKP", "crv": "Ed25519", "x": GOLDEN_X}
GOLDEN_KID = jwk_thumbprint(GOLDEN_JWK)
# Non-canonical alias of GOLDEN_X: a canonical 43-char encoding has zero
# unused pad bits in its final character, so setting one yields a distinct
# string (and a distinct kid) that decodes to the identical 32 key bytes.
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
GOLDEN_X_ALIAS = GOLDEN_X[:-1] + _B64[_B64.index(GOLDEN_X[-1]) | 1]
GOLDEN_JWK_ALIAS = {"kty": "OKP", "crv": "Ed25519", "x": GOLDEN_X_ALIAS}
GOLDEN_KID_ALIAS = jwk_thumbprint(GOLDEN_JWK_ALIAS)
DOMAIN_UUID = "123e4567-e89b-42d3-a456-426614174000"
INSTANCE_UUID = "123e4567-e89b-42d3-a456-426614174111"


def _stub_dict(agent: str = "iris", **overrides) -> dict:
    stub = {
        "kid": GOLDEN_KID,
        "jwk": dict(GOLDEN_JWK),
        "agent": agent,
        "agent_urn": f"urn:oacp:agent:{DOMAIN_UUID}:{agent}",
        "instance_urn": f"urn:uuid:{INSTANCE_UUID}",
        "created_at_utc": "2026-07-13T00:00:00Z",
    }
    stub.update(overrides)
    return stub


class TrustTempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project_dir = self.tmp / "projects" / "demo"
        (self.project_dir / "agents" / "claude").mkdir(parents=True)
        self.catalog_path = self.project_dir / CATALOG_RELPATH
        self.pins_path = (
            self.project_dir / "agents" / "claude" / "trust" / "allowed_signers.yaml"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_stub(self, stub: dict, name: str = "key.pub.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(stub), encoding="utf-8")
        return path


class TestCatalogReader(TrustTempDirCase):
    def test_missing_file_is_empty_catalog(self) -> None:
        self.assertEqual(load_catalog(self.catalog_path), {})

    def test_round_trip(self) -> None:
        entries = {
            GOLDEN_KID: {
                "agent": "iris",
                "domain": DOMAIN_UUID,
                "instance": INSTANCE_UUID,
                "jwk": dict(GOLDEN_JWK),
                "created_at_utc": "2026-07-13T00:00:00Z",
            }
        }
        write_catalog(self.catalog_path, entries)
        loaded = load_catalog(self.catalog_path)
        self.assertEqual(loaded, entries)

    def test_version_handling(self) -> None:
        for version, ok in (("1", True), (1, True), (2, False), (None, False)):
            rendered = "signers: []\n" if version is None else (
                f"version: {json.dumps(version)}\nentries: []\n"
            )
            self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
            self.catalog_path.write_text(rendered, encoding="utf-8")
            if ok:
                self.assertEqual(load_catalog(self.catalog_path), {})
            else:
                with self.assertRaises(TrustRootError):
                    load_catalog(self.catalog_path)

    def test_rejections(self) -> None:
        entry = (
            f"  - {{agent: iris, kid: {GOLDEN_KID}, "
            f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n"
        )
        cases = [
            "version: 1\nentries: {}\n",                       # entries not a list
            "version: 1\nentries:\n  - not-a-mapping\n",
            "version: 1\nentries:\n  - {agent: iris}\n",       # missing kid
            (                                                   # malformed kid
                "version: 1\nentries:\n"
                "  - {agent: iris, kid: kid-1, "
                f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n"
            ),
            (                                                   # bad agent name
                "version: 1\nentries:\n"
                f"  - {{agent: 'no spaces allowed', kid: {GOLDEN_KID}, "
                f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n"
            ),
            (                                                   # non-Ed25519 jwk
                "version: 1\nentries:\n"
                f"  - {{agent: iris, kid: {GOLDEN_KID}, jwk: {{kty: RSA}}}}\n"
            ),
            (                                                   # private component
                "version: 1\nentries:\n"
                f"  - {{agent: iris, kid: {GOLDEN_KID}, "
                f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}, d: QUJD}}}}\n"
            ),
            (                                                   # reserved column type
                "version: 1\nentries:\n"
                f"  - {{agent: iris, kid: {GOLDEN_KID}, domain: 7, "
                f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n"
            ),
            (                                                   # kid not the thumbprint
                "version: 1\nentries:\n"
                f"  - {{agent: iris, kid: {'c' * 43}, "
                f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n"
            ),
            (                                                   # non-canonical x alias
                "version: 1\nentries:\n"
                f"  - {{agent: iris, kid: {GOLDEN_KID_ALIAS}, "
                f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X_ALIAS}}}}}\n"
            ),
            "version: 1\nentries:\n" + entry + entry,           # duplicate kid
        ]
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        for content in cases:
            self.catalog_path.write_text(content, encoding="utf-8")
            with self.assertRaises(TrustRootError, msg=content):
                load_catalog(self.catalog_path)


class TestPublicStub(TrustTempDirCase):
    def test_valid_stub_normalizes(self) -> None:
        entry = load_public_stub(self._write_stub(_stub_dict()))
        self.assertEqual(entry["kid"], GOLDEN_KID)
        self.assertEqual(entry["agent"], "iris")
        self.assertEqual(entry["domain"], DOMAIN_UUID)
        self.assertEqual(entry["instance"], INSTANCE_UUID)

    def test_kid_thumbprint_mismatch_rejected(self) -> None:
        stub = _stub_dict(kid="c" * 43)
        with self.assertRaises(TrustImportError):
            load_public_stub(self._write_stub(stub))

    def test_undecodable_key_material_rejected(self) -> None:
        # thumbprint self-consistency is not proof of usable key material:
        # a stub can hash any x string into a matching kid, so x must
        # strictly decode to exactly 32 bytes before anything is granted.
        from message_signing import b64url_encode

        for bad_x in ("A", "QUJD", b64url_encode(b"z" * 16), b64url_encode(b"z" * 33)):
            jwk = {"kty": "OKP", "crv": "Ed25519", "x": bad_x}
            stub = _stub_dict(jwk=jwk, kid=jwk_thumbprint(jwk))
            with self.assertRaises(TrustImportError, msg=bad_x):
                load_public_stub(self._write_stub(stub))

    def test_private_component_rejected(self) -> None:
        stub = _stub_dict()
        stub["jwk"]["d"] = "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A"
        with self.assertRaises(TrustImportError):
            load_public_stub(self._write_stub(stub))

    def test_non_canonical_x_alias_rejected(self) -> None:
        # a kid consistent with the alias spelling must not rescue it:
        # the boundary requires the canonical encoding itself, so one key
        # has exactly one x string and one kid.
        stub = _stub_dict(kid=GOLDEN_KID_ALIAS, jwk=dict(GOLDEN_JWK_ALIAS))
        with self.assertRaises(TrustImportError):
            load_public_stub(self._write_stub(stub))

    def test_agent_urn_name_mismatch_rejected(self) -> None:
        stub = _stub_dict(agent_urn=f"urn:oacp:agent:{DOMAIN_UUID}:codex")
        with self.assertRaises(TrustImportError):
            load_public_stub(self._write_stub(stub))

    def test_duplicate_json_keys_rejected(self) -> None:
        path = self.tmp / "dup.pub.json"
        body = json.dumps(_stub_dict())
        path.write_text(body[:-1] + f', "kid": "{GOLDEN_KID}"}}', encoding="utf-8")
        with self.assertRaises(TrustImportError):
            load_public_stub(path)

    def test_missing_urns_rejected(self) -> None:
        for field in ("agent_urn", "instance_urn"):
            stub = _stub_dict()
            del stub[field]
            with self.assertRaises(TrustImportError, msg=field):
                load_public_stub(self._write_stub(stub))


class TestImport(TrustTempDirCase):
    def test_round_trip_then_idempotent(self) -> None:
        stub_path = self._write_stub(_stub_dict())
        report = import_public_stub(stub_path, self.project_dir, receiver="claude")
        self.assertEqual((report["catalog"], report["pins"]), ("added", "added"))

        catalog = load_catalog(self.catalog_path)
        pins = load_allowed_signers(self.pins_path)
        self.assertIn(GOLDEN_KID, catalog)
        self.assertEqual(pins[GOLDEN_KID]["status"], "active")
        self.assertEqual(pins[GOLDEN_KID]["domain"], DOMAIN_UUID)

        again = import_public_stub(stub_path, self.project_dir, receiver="claude")
        self.assertEqual((again["catalog"], again["pins"]), ("unchanged", "unchanged"))

    def test_catalog_only_grants_nothing(self) -> None:
        stub_path = self._write_stub(_stub_dict())
        report = import_public_stub(stub_path, self.project_dir, catalog_only=True)
        self.assertEqual(report["catalog"], "added")
        self.assertEqual(report["pins"], "skipped")
        self.assertIn(GOLDEN_KID, load_catalog(self.catalog_path))
        self.assertFalse(self.pins_path.exists())

    def test_receiver_required_without_catalog_only(self) -> None:
        with self.assertRaises(TrustImportError):
            import_public_stub(
                self._write_stub(_stub_dict()), self.project_dir
            )

    def test_receiver_path_traversal_refused(self) -> None:
        # a trust file must never be creatable outside the selected
        # project: separators, dot components, and unknown agents are all
        # refused before anything is read or written.
        victim = self.tmp / "projects" / "victim"
        stub_path = self._write_stub(_stub_dict())
        for receiver in (
            "../../victim",
            "..",
            ".",
            "claude/../claude",
            "a\\b",
            ".hidden",
            "ghost",  # valid name, but no agent directory exists
            "",
        ):
            with self.assertRaises(TrustImportError, msg=repr(receiver)):
                import_public_stub(
                    stub_path, self.project_dir, receiver=receiver
                )
        self.assertFalse(victim.exists())
        self.assertFalse(
            (self.tmp / "projects" / "trust").exists(),
            "traversal must not create sibling trust directories",
        )

    def test_symlinked_agent_dir_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.project_dir / "agents" / "linked").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaises(TrustImportError):
            import_public_stub(
                self._write_stub(_stub_dict()),
                self.project_dir,
                receiver="linked",
            )

    def test_same_kid_different_identity_refused(self) -> None:
        import_public_stub(
            self._write_stub(_stub_dict()), self.project_dir, receiver="claude"
        )
        conflict = _stub_dict(
            agent="codex", agent_urn=f"urn:oacp:agent:{DOMAIN_UUID}:codex"
        )
        with self.assertRaises(TrustImportError):
            import_public_stub(
                self._write_stub(conflict, "conflict.pub.json"),
                self.project_dir,
                receiver="claude",
            )

    def test_import_never_reactivates_a_revoked_pin(self) -> None:
        stub_path = self._write_stub(_stub_dict())
        import_public_stub(stub_path, self.project_dir, receiver="claude")
        pins = load_allowed_signers(self.pins_path)
        pins[GOLDEN_KID]["status"] = "revoked"
        from trust_root import write_pins

        write_pins(self.pins_path, pins)
        with self.assertRaises(TrustImportError):
            import_public_stub(stub_path, self.project_dir, receiver="claude")
        self.assertEqual(
            load_allowed_signers(self.pins_path)[GOLDEN_KID]["status"], "revoked"
        )

    def test_revoked_key_cannot_return_as_encoding_alias(self) -> None:
        # the alias spelling of a revoked key decodes to the identical key
        # bytes but thumbprints to a fresh kid; if it imported, the revoked
        # key material would be back as a new active pin and the same
        # private key would verify again. It must be refused outright, and
        # neither trust file may gain a second entry.
        stub_path = self._write_stub(_stub_dict())
        import_public_stub(stub_path, self.project_dir, receiver="claude")
        pins = load_allowed_signers(self.pins_path)
        pins[GOLDEN_KID]["status"] = "revoked"
        from trust_root import write_pins

        write_pins(self.pins_path, pins)

        alias = _stub_dict(kid=GOLDEN_KID_ALIAS, jwk=dict(GOLDEN_JWK_ALIAS))
        with self.assertRaises(TrustImportError):
            import_public_stub(
                self._write_stub(alias, "alias.pub.json"),
                self.project_dir,
                receiver="claude",
            )
        pins = load_allowed_signers(self.pins_path)
        self.assertEqual(list(pins), [GOLDEN_KID])
        self.assertEqual(pins[GOLDEN_KID]["status"], "revoked")
        self.assertEqual(list(load_catalog(self.catalog_path)), [GOLDEN_KID])

    def test_pin_without_reserved_columns_upgrades_in_place(self) -> None:
        # a same-identity pin that predates the reserved columns gains
        # them from the validated stub; authority and status are untouched
        # and re-import is then a no-op.
        self.pins_path.parent.mkdir(parents=True, exist_ok=True)
        self.pins_path.write_text(
            "version: 1\nsigners:\n"
            f"  - {{agent: iris, kid: {GOLDEN_KID}, status: active, "
            f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n",
            encoding="utf-8",
        )
        stub_path = self._write_stub(_stub_dict())
        report = import_public_stub(stub_path, self.project_dir, receiver="claude")
        self.assertEqual(report["pins"], "updated")

        pins = load_allowed_signers(self.pins_path)
        self.assertEqual(pins[GOLDEN_KID]["domain"], DOMAIN_UUID)
        self.assertEqual(pins[GOLDEN_KID]["instance"], INSTANCE_UUID)
        self.assertEqual(pins[GOLDEN_KID]["status"], "active")

        again = import_public_stub(stub_path, self.project_dir, receiver="claude")
        self.assertEqual(again["pins"], "unchanged")

    def test_concurrent_imports_preserve_every_entry(self) -> None:
        # whole-file re-emission without the project trust lock loses
        # updates: eight concurrent importers of distinct identities must
        # all survive in both the catalog and the receiver pins.
        import subprocess
        import time

        from message_signing import b64url_encode

        stubs = []
        for i in range(8):
            jwk = {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": b64url_encode(bytes([i + 1]) * 32),
            }
            stub = _stub_dict(jwk=jwk, kid=jwk_thumbprint(jwk))
            stubs.append(self._write_stub(stub, f"key{i}.pub.json"))

        go_file = self.tmp / "go"
        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        script = (
            "import os, sys, time\n"
            f"sys.path.insert(0, {scripts_dir!r})\n"
            "from trust_root import import_public_stub\n"
            f"while not os.path.exists({str(go_file)!r}):\n"
            "    time.sleep(0.01)\n"
            f"import_public_stub(sys.argv[1], {str(self.project_dir)!r},\n"
            "    receiver='claude')\n"
            "print('OK')\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(stub)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for stub in stubs
        ]
        # let every child reach the start gate before releasing it, so all
        # eight transactions genuinely contend (without the project trust
        # lock this scenario loses most entries)
        time.sleep(0.3)
        go_file.write_text("go", encoding="utf-8")
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())
            self.assertEqual(out.strip().decode(), "OK")

        self.assertEqual(len(load_catalog(self.catalog_path)), 8)
        self.assertEqual(len(load_allowed_signers(self.pins_path)), 8)


class TestRevoke(TrustTempDirCase):
    def _pin_for(self, receiver: str) -> None:
        (self.project_dir / "agents" / receiver).mkdir(
            parents=True, exist_ok=True
        )
        import_public_stub(
            self._write_stub(_stub_dict()), self.project_dir, receiver=receiver
        )

    def test_revoke_flips_status_and_keeps_jwk(self) -> None:
        self._pin_for("claude")
        report = revoke_pin(self.project_dir, GOLDEN_KID, receiver="claude")
        self.assertEqual(report["receivers"], {"claude": "revoked"})
        self.assertEqual(report["revoked"], ["claude"])

        pins = load_allowed_signers(self.pins_path)
        self.assertEqual(pins[GOLDEN_KID]["status"], "revoked")
        # Key material is retained so a manual reactivation cannot smuggle
        # a swapped key past the reader's re-validation.
        self.assertEqual(pins[GOLDEN_KID]["jwk"]["x"], GOLDEN_X)

    def test_revoke_is_idempotent(self) -> None:
        self._pin_for("claude")
        revoke_pin(self.project_dir, GOLDEN_KID, receiver="claude")
        again = revoke_pin(self.project_dir, GOLDEN_KID, receiver="claude")
        self.assertEqual(again["receivers"], {"claude": "unchanged"})
        pins = load_allowed_signers(self.pins_path)
        self.assertEqual(pins[GOLDEN_KID]["status"], "revoked")

    def test_revoke_unknown_kid_refused(self) -> None:
        self._pin_for("claude")
        with self.assertRaises(TrustRevokeError):
            revoke_pin(self.project_dir, GOLDEN_KID_ALIAS, receiver="claude")

    def test_revoke_non_canonical_kid_spelling_refused(self) -> None:
        self._pin_for("claude")
        alias_spelling = GOLDEN_KID[:-1] + _B64[_B64.index(GOLDEN_KID[-1]) | 1]
        self.assertNotEqual(alias_spelling, GOLDEN_KID)
        with self.assertRaises(AuthFormatError):
            revoke_pin(self.project_dir, alias_spelling, receiver="claude")
        # the canonical pin is untouched by the refused alias revoke
        pins = load_allowed_signers(self.pins_path)
        self.assertEqual(pins[GOLDEN_KID]["status"], "active")

    def test_revoke_all_receivers(self) -> None:
        self._pin_for("claude")
        self._pin_for("codex")
        # an agent without pins is skipped, not an error
        (self.project_dir / "agents" / "cursor").mkdir(parents=True)
        report = revoke_pin(self.project_dir, GOLDEN_KID, all_receivers=True)
        self.assertEqual(
            report["receivers"], {"claude": "revoked", "codex": "revoked"}
        )
        for receiver in ("claude", "codex"):
            pins_path = (
                self.project_dir / "agents" / receiver / "trust"
                / "allowed_signers.yaml"
            )
            pins = load_allowed_signers(pins_path)
            self.assertEqual(pins[GOLDEN_KID]["status"], "revoked")

    def test_revoke_all_receivers_requires_a_pin_somewhere(self) -> None:
        with self.assertRaises(TrustRevokeError):
            revoke_pin(self.project_dir, GOLDEN_KID, all_receivers=True)

    def test_revoke_all_receivers_is_all_or_nothing(self) -> None:
        # A malformed receiver between two valid ones must fail the whole
        # fleet revocation with ZERO pins changed — a partial revoke would
        # leave later receivers silently trusting the compromised kid
        # behind an error exit.
        self._pin_for("alice")
        self._pin_for("charlie")
        malformed = (
            self.project_dir / "agents" / "bob" / "trust"
            / "allowed_signers.yaml"
        )
        malformed.parent.mkdir(parents=True, exist_ok=True)
        malformed.write_text("version: 1\nsigners: not-a-list\n", encoding="utf-8")

        with self.assertRaises(TrustRootError):
            revoke_pin(self.project_dir, GOLDEN_KID, all_receivers=True)

        for receiver in ("alice", "charlie"):
            pins_path = (
                self.project_dir / "agents" / receiver / "trust"
                / "allowed_signers.yaml"
            )
            pins = load_allowed_signers(pins_path)
            self.assertEqual(pins[GOLDEN_KID]["status"], "active")

    def test_revoke_requires_exactly_one_target(self) -> None:
        self._pin_for("claude")
        with self.assertRaises(TrustRevokeError):
            revoke_pin(self.project_dir, GOLDEN_KID)
        with self.assertRaises(TrustRevokeError):
            revoke_pin(
                self.project_dir,
                GOLDEN_KID,
                receiver="claude",
                all_receivers=True,
            )

    def test_revoked_pin_refuses_reimport(self) -> None:
        self._pin_for("claude")
        revoke_pin(self.project_dir, GOLDEN_KID, receiver="claude")
        with self.assertRaises(TrustImportError):
            import_public_stub(
                self._write_stub(_stub_dict()),
                self.project_dir,
                receiver="claude",
            )


class TestDrift(TrustTempDirCase):
    def _codes(self, severity: str = None) -> list:
        findings = drift_report(self.project_dir)
        if severity is None:
            return [f["code"] for f in findings]
        return [f["code"] for f in findings if f["severity"] == severity]

    def test_clean_after_import(self) -> None:
        import_public_stub(
            self._write_stub(_stub_dict()), self.project_dir, receiver="claude"
        )
        self.assertEqual(self._codes(DRIFT_ERROR), [])
        self.assertEqual(self._codes(DRIFT_WARN), [])

    def test_unpinned_catalog_identity_is_not_drift(self) -> None:
        import_public_stub(
            self._write_stub(_stub_dict()), self.project_dir, catalog_only=True
        )
        self.assertEqual(self._codes(DRIFT_ERROR), [])
        self.assertEqual(self._codes(DRIFT_WARN), [])

    def test_pinned_but_not_cataloged_warns(self) -> None:
        self.pins_path.parent.mkdir(parents=True, exist_ok=True)
        self.pins_path.write_text(
            "version: 1\nsigners:\n"
            f"  - {{agent: iris, kid: {GOLDEN_KID}, status: active, "
            f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n",
            encoding="utf-8",
        )
        self.assertIn("pin-not-in-catalog", self._codes(DRIFT_WARN))

    def test_pin_integrity_failure_makes_pins_unreadable(self) -> None:
        # integrity is enforced by the reader itself (the verify
        # boundary), so a kid that is not the thumbprint of its jwk makes
        # the whole pins file unusable rather than a per-entry advisory.
        self.pins_path.parent.mkdir(parents=True, exist_ok=True)
        self.pins_path.write_text(
            "version: 1\nsigners:\n"
            f"  - {{agent: iris, kid: {'c' * 43}, status: active, "
            f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n",
            encoding="utf-8",
        )
        self.assertIn("pins-unreadable", self._codes(DRIFT_ERROR))

    def test_catalog_integrity_failure_makes_catalog_unreadable(self) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.write_text(
            "version: 1\nentries:\n"
            f"  - {{agent: iris, kid: {'c' * 43}, "
            f"jwk: {{kty: OKP, crv: Ed25519, x: {GOLDEN_X}}}}}\n",
            encoding="utf-8",
        )
        self.assertIn("catalog-unreadable", self._codes(DRIFT_ERROR))

    def test_unreadable_files_are_errors(self) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.write_text("version: 99\nentries: []\n", encoding="utf-8")
        self.pins_path.parent.mkdir(parents=True, exist_ok=True)
        self.pins_path.write_text("not: [valid\n", encoding="utf-8")
        codes = self._codes(DRIFT_ERROR)
        self.assertIn("catalog-unreadable", codes)
        self.assertIn("pins-unreadable", codes)

    def test_revoked_pin_missing_from_catalog_is_not_drift(self) -> None:
        self.pins_path.parent.mkdir(parents=True, exist_ok=True)
        self.pins_path.write_text(
            "version: 1\nsigners:\n"
            f"  - {{agent: iris, kid: {GOLDEN_KID}, status: revoked}}\n",
            encoding="utf-8",
        )
        self.assertEqual(self._codes(DRIFT_WARN), [])

    def test_has_trust_root(self) -> None:
        self.assertFalse(has_trust_root(self.project_dir))
        import_public_stub(
            self._write_stub(_stub_dict()), self.project_dir, catalog_only=True
        )
        self.assertTrue(has_trust_root(self.project_dir))

    def _empty_pins(self, agent: str = "claude") -> None:
        pins = (
            self.project_dir / "agents" / agent / "trust" / "allowed_signers.yaml"
        )
        pins.parent.mkdir(parents=True, exist_ok=True)
        pins.write_text("version: 1\nsigners: []\n", encoding="utf-8")

    def _findings(self, code: str) -> list:
        return [f for f in drift_report(self.project_dir) if f["code"] == code]

    def test_cataloged_not_pinned_notes_without_liveness_signal(self) -> None:
        # cataloged identity, receiver has a pins file but no pin, no other
        # receiver pins it, no inbox traffic — advisory note, never a warn
        # (the catalog stays zero-authority).
        import_public_stub(
            self._write_stub(_stub_dict()), self.project_dir, catalog_only=True
        )
        self._empty_pins()
        found = self._findings("catalog-not-pinned")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "ok")
        self.assertIn("signed-unknown-kid", found[0]["message"])

    def _write_inbox_message(self, sender: str, filename: str) -> None:
        inbox = self.project_dir / "agents" / "claude" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / filename).write_text(
            f"id: msg-x\nfrom: {sender}\nto: claude\n", encoding="utf-8"
        )

    def test_cataloged_not_pinned_warns_on_inbox_traffic(self) -> None:
        # the soak enablement gap: iris cataloged but unpinned while her
        # messages sit in the receiver's inbox — doctor must warn.
        import_public_stub(
            self._write_stub(_stub_dict()), self.project_dir, catalog_only=True
        )
        self._empty_pins()
        self._write_inbox_message("iris", "20260717000000_iris_task_request_ab12.yaml")
        found = self._findings("catalog-not-pinned")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], DRIFT_WARN)
        self.assertIn("inbox traffic", found[0]["message"])

    def test_traffic_signal_attributes_underscore_senders(self) -> None:
        # sender names and message types may both contain underscores, so
        # filename tokens cannot attribute traffic — the message's own
        # `from` field is the signal.
        import_public_stub(
            self._write_stub(_stub_dict(agent="review_bot")),
            self.project_dir,
            catalog_only=True,
        )
        self._empty_pins()
        self._write_inbox_message(
            "review_bot", "20260717000000_review_bot_task_request_ab12.yaml"
        )
        found = self._findings("catalog-not-pinned")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], DRIFT_WARN)
        self.assertIn("inbox traffic", found[0]["message"])

    def test_traffic_signal_has_no_prefix_false_positive(self) -> None:
        # a cataloged agent named `review` must not inherit review_bot's
        # traffic via filename-prefix confusion.
        from message_signing import b64url_encode

        jwk = {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": b64url_encode(bytes([7]) * 32),
        }
        stub = _stub_dict(agent="review", jwk=jwk, kid=jwk_thumbprint(jwk))
        import_public_stub(
            self._write_stub(stub), self.project_dir, catalog_only=True
        )
        self._empty_pins()
        self._write_inbox_message(
            "review_bot", "20260717000000_review_bot_task_request_ab12.yaml"
        )
        found = self._findings("catalog-not-pinned")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "ok")

    def test_cataloged_not_pinned_warns_when_pinned_elsewhere(self) -> None:
        (self.project_dir / "agents" / "codex").mkdir(parents=True, exist_ok=True)
        import_public_stub(
            self._write_stub(_stub_dict()), self.project_dir, receiver="codex"
        )
        self._empty_pins("claude")
        found = self._findings("catalog-not-pinned")
        claude_findings = [f for f in found if f["message"].startswith("claude:")]
        self.assertEqual(len(claude_findings), 1)
        self.assertEqual(claude_findings[0]["severity"], DRIFT_WARN)
        self.assertIn("another receiver pins it", claude_findings[0]["message"])

    def test_own_cataloged_identity_is_not_flagged(self) -> None:
        # a receiver never pins itself — its own cataloged identity is not
        # an enablement gap.
        import_public_stub(
            self._write_stub(_stub_dict(agent="claude")),
            self.project_dir,
            catalog_only=True,
        )
        self._empty_pins("claude")
        self.assertEqual(self._findings("catalog-not-pinned"), [])


@unittest.skipUnless(CRYPTO_AVAILABLE, "requires oacp-cli[crypto]")
class TestEndToEnd(TrustTempDirCase):
    def test_gen_import_verify(self) -> None:
        """oacp key gen → trust import → a signed message verifies."""
        oacp_home = self.tmp / "home"
        oacp_home.mkdir()
        report = generate_keypair("iris", oacp_home)
        stub_path = Path(report["public_stub_path"])

        result = import_public_stub(stub_path, self.project_dir, receiver="claude")
        self.assertEqual(result["pins"], "added")

        message = render_yaml(
            build_message_dict(
                sender="iris", recipient="claude", msg_type="notification",
                subject="Signed", body="hello",
            )
        ).encode("utf-8")
        signers = load_signers("iris", oacp_home)
        signed = sign_and_append(message, signers)

        pins = load_allowed_signers(self.pins_path)
        outcome = verify_message(signed, pins)
        self.assertEqual(outcome["status"], STATUS_VERIFIED)
        self.assertEqual(outcome["kid"], report["kid"])


if __name__ == "__main__":
    unittest.main()
