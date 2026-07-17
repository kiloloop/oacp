# OACP Signing Conformance Fixtures

Byte-exact conformance corpus for the v0.4.0 message-signing wire format
("raw-prefix detached-JWS auth trailer v1", `docs/protocol/message_signing.md`).
The committed bytes are the contract: any implementation of the trailer
framing, the verify-before-parse flow, or the signer must reproduce exactly
what these fixtures pin.

## Layout

- `messages/` — the corpus. `signed_*.yaml` and `unsigned_*.yaml` are goldens;
  `tamper_*.yaml` are negative vectors derived from `signed_basic`. Every
  signed golden ships its exact unsigned payload as `<case>.prefix.yaml` —
  the trailer boundary is pinned by data, not by the code under test.
  The corpus is exempt from EOL conversion (see the repo `.gitattributes`);
  several vectors are deliberate CRLF / trailing-whitespace artifacts.
- `expected/` — one file per case: pinned digests (raw bytes, signed prefix,
  per-signature JWS preimage) plus the full `message_auth` block, warn
  annotation, and validator observations for the committed bytes.
- `keys/` — fixture Ed25519 keys, **published test vectors** (in the spirit
  of RFC 8037 A.3). They grant no authority anywhere and must never be
  pinned outside this corpus. Private fixture material lives only here.
- `pins/allowed_signers.yaml` — the receiver pins the corpus verifies
  against: `alice` and `bob` active, `carol` revoked. The cast is the
  standard cryptographic one — fixture identities only, mapped to no real
  agent anywhere.
- `regen_fixtures.py` — deterministic regeneration; also the normative
  definition of every tamper mutation.

## What consumers must match

Normative for any implementation:

- `trailer_state` — the tri-state boundary classification (`ok` /
  `malformed` / `absent`), and the exact prefix bytes for `ok`.
- `prefix_sha256`, `signing_input_sha256` — the signed bytes and the
  RFC 7515 signing inputs. Byte-different means different message.
  Preimage digests are pinned by forensic extraction, not the acceptance
  gate: a vector whose trailer spelling is rejected still has well-defined
  signing inputs (`protected` + signed prefix are untouched); `[]` is
  reserved for artifacts with no extractable protected value and signed
  prefix.
- `message_auth.status`, `signatures_checked[].outcome`, `kid`,
  `verified_signer`, `verified_instance`, `claimed_sender`,
  `payload_sha256` — the verify outcome against `pins/`.
- Canonical trailer encodings — the outer `auth` value and every
  `signatures[].signature` member are the canonical unpadded base64url
  spelling of their bytes, and the container JSON is the signer's compact
  sorted-key emit. One signed message has exactly one wire spelling: the
  `tamper_sig_alias`, `tamper_auth_alias`, and `tamper_container_reformat`
  vectors pin the rejection of every other spelling.

Informative (this implementation's surface; other implementations may
diverge): `reason` prose, `annotation` strings, `validate_errors` wording.

## Regenerating — a deliberate act

**A golden change is a wire-format change.** Do not regenerate to make a
failing test pass — the failure is the corpus doing its job. A diff here
means observed behavior moved against the committed contract, and that
needs a ruling on the protocol level before any fixture is rewritten.

```sh
python3 tests/conformance/signing/regen_fixtures.py --check   # diff only
python3 tests/conformance/signing/regen_fixtures.py --write   # after a ruling
```

Everything is deterministic — Ed25519 signatures (RFC 8032), the signer's
compact sorted-key JSON emit, and pinned message ids/timestamps — so a
no-op regen is byte-for-byte idempotent (CI enforces this via the runner's
regen check).

Guard rails built into `--write`:

- It refuses to overwrite a `*.prefix.yaml` whose rendered bytes changed
  unless `--accept-render-change` is also given: renderer output drift for
  a pinned spec is itself a wire-format event.
- `--mint-keys` exists for bootstrapping an empty corpus only. Never mint
  over an existing corpus — new keys orphan every committed golden and
  every digest.

The executable runner is `tests/test_signing_conformance.py`: both parse
paths (`message_signing.split_signed_message`,
`message_verify.classify_auth_trailer`) must agree byte-for-byte on every
fixture, the validator cross-check must agree, `verify_message` must
reproduce every pinned `message_auth` block, and re-signing each golden
must reproduce the committed bytes exactly.
