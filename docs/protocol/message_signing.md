# Message Signing — Trust Root & Key Management (v0.4.0, warn mode)

Status: **non-normative** companion to the message-signing wire format.
The wire format itself — the raw-prefix detached-JWS `auth` trailer — is
specified in [`inbox_outbox.md` → "Signed messages"](inbox_outbox.md);
this document covers the trust root, verification modes, and key
management. The normative authority-doctrine amendment ships with the
first authority-bearing knob (a later 0.4.x/0.5 release) as its own
reviewed change.

## Warn-mode seam (v0.4.0)

**Warn mode records identity and grants no authority.** Every verification
outcome — `unsigned`, `signed-verified`, `signed-unknown-kid`,
`signed-INVALID`, `signed-REVOKED` — produces an annotation and a
`message_auth` audit block; none of them rejects, quarantines-as-rejection,
or changes how a message is processed. A verified signature is a recorded
fact about who signed, not a permission. Enforce mode (rejection, receipt
ledger, quarantine activation) is **v0.4.1**, activated only after a warn
soak on live fleet traffic; the seams exist in v0.4.0 and none activate.

Receivers opt in per-agent via `signing.verify_mode: off | warn` in
`agents/<receiver>/config.yaml`. An early `enforce` value degrades to
`warn`; anything else degrades to `off`.

## Trust root

Two files, deliberately asymmetric in authority:

| File | Location | Authority |
|------|----------|-----------|
| `catalog.yaml` | `projects/<project>/trust/` | **Zero.** A distribution catalog: records signer identities so receivers have a local place to import from. Being cataloged grants nothing. |
| `allowed_signers.yaml` | `projects/<project>/agents/<receiver>/trust/` | **All of it.** The receiver's own pins are the only thing consulted at verify time. No network, ever. |

A message from a cataloged-but-unpinned key still annotates
`signed-unknown-kid`. An *unpinned* catalog identity is a legitimate
resting state (the catalog grants nothing); a *pinned* identity missing
from the catalog means authority was granted to an identity that was never
recorded — that is the drift `oacp doctor --project <name>` reports
(warn), alongside integrity errors (an entry whose `kid` is not the
RFC 7638 thumbprint of its `jwk`).

Doctor also surfaces the *operational* half of that asymmetry as the
advisory code `catalog-not-pinned`: a cataloged identity a receiver does
not pin escalates from note to warn when it shows a liveness signal
(another receiver actively pins it, or the receiver's inbox holds traffic
from that agent). This is the verification-enablement gap in practice —
`signed-unknown-kid` annotations on live traffic while the trust root
otherwise reports clean are its signature. The advisory never makes
pinning mandatory.

### File format (v1)

Both files are format-versioned and CLI-managed (writers re-emit the whole
file; comments are not preserved). The `domain:` and `instance:` columns
are **reserved from day one and unused in v0.4.0** — they are
shape-checked (string or null) and carry no semantics until a future
format version assigns them some. Carrying them now means the day
cross-domain or per-machine trust semantics arrive, no pin file needs
rewriting.

```yaml
# agents/<receiver>/trust/allowed_signers.yaml
version: 1
signers:
  - agent: iris
    domain: <trust-domain uuid>      # reserved, unused in v0.4.0
    instance: <machine-instance uuid> # reserved, unused in v0.4.0
    kid: <RFC 7638 thumbprint, 43-char base64url>
    jwk: {kty: OKP, crv: Ed25519, x: <base64url>}
    status: active                   # active | revoked
```

The project catalog uses the same columns under an `entries:` list, with
`created_at_utc` instead of `status` (a catalog entry has no status —
it grants nothing to revoke).

### Import flow

```bash
# on the signer's machine — mints the keypair + a public stub
oacp key gen --agent iris
# → $OACP_HOME/keys/<domain>/iris/<instance>/<kid>.json       (private, 0600)
# → $OACP_HOME/keys/<domain>/iris/<instance>/<kid>.pub.json   (public stub)

# on the receiver's side — catalog + pin in one step
oacp trust import /path/to/<kid>.pub.json --project my-project --agent claude

# record identity only, grant nothing
oacp trust import /path/to/<kid>.pub.json --project my-project --catalog-only

# inspect / audit
oacp trust list --project my-project
oacp doctor --project my-project      # catalog-vs-pins drift check

# revoke a pin (per receiver, or fleet-wide in one transaction)
oacp trust revoke <kid> --project my-project --agent claude
oacp trust revoke <kid> --project my-project --all-receivers
```

`oacp trust import` refuses: a stub whose `kid` is not the thumbprint of
its `jwk`, an `x` that is not the canonical base64url encoding of a
32-byte Ed25519 public key (one key has exactly one spelling and one
`kid` — an encoding alias must not mint a second identity for revoked
key material), any `jwk` carrying a private component, a same-`kid` entry
that differs from what is already recorded, and — always — reactivating a
`revoked` pin (revocation is a receiver decision; remove the entry
manually to re-trust). Project and receiver names are validated with
containment checks (a trust file is never creatable outside the selected
project), and the whole catalog-then-pins transaction holds a project
trust lock so concurrent imports serialize instead of dropping each
other's entries. The pin reader enforces the same strict profile at the
verify boundary — an entry with a wrong thumbprint or private material
makes the whole file unusable rather than silently trusted. How a stub
travels between machines is out of scope for v0.4.0: any channel the
operator already trusts for configuration (the pins are receiver-local
either way).

## Receiver audit stamping (`--attach-audit`)

Receivers that keep autonomy audit records stamp the verification outcome
into the record with the same invocation that verifies the message:

```bash
oacp verify <message.yaml> --project <p> --receiver <r> \
  --attach-audit "$OACP_HOME/projects/<p>/agents/<r>/audit/autonomy_decisions/<record>.yaml"
```

`--attach-audit` takes a filesystem path used as given (no workspace-root
inference), so pass the record's canonical runtime path — audit records
live under `$OACP_HOME`, not the source checkout. The record must already
exist: write the autonomy decision record first, then stamp it.

This is the one supported stamping path. It writes the block under the
shared audit lock, atomically, and refuses to overwrite a recorded block —
hand-writing a `message_auth` block (or free-text prose about the
verification) is the anti-pattern: hand-shaped variants drift across
records and instrumentation cannot parse them.

The canonical location is **`result.message_auth`** inside the schema-v2
audit record, and the canonical shape is exactly what verification
returns:

```yaml
result:
  message_auth:
    status: verified            # unsigned | verified | untrusted | invalid | revoked | unsupported
    alg: EdDSA
    scheme: raw-prefix-v1
    claimed_sender: <agent name from the signed prefix>
    verified_signer: <urn:oacp:agent:...>   # null unless verified
    verified_instance: <urn:uuid:...>       # null unless verified
    kid: <RFC 7638 thumbprint>              # null unless verified
    payload_sha256: <hex digest of the signed prefix bytes>
    trust_source: <pins path consulted>
    signatures_checked:
      - {kid: ..., agent: ..., outcome: verified}
    reason: <string or null>
    verified_at_utc: "<ISO 8601 Z>"
```

Warn-mode semantics carry through unchanged: the block records identity
and grants no authority — gates and instrumentation consume it as
telemetry only.

## Key management

- **Keys are per-machine and never leave `$OACP_HOME/keys/`.** They are
  never synced and never committed. The memory-sync allowlist structurally
  excludes `keys/` on the push side, and the canonical workspace
  `.gitignore` carries an explicit `keys/` deny line that `oacp memory
  init` and the doctor `root-gitignore` drift check propagate fleet-wide.
- **The 0600 file keystore is the v0.4.0 floor, not the design.** Private
  key files are created `0600` under `0700` directories and loaded only
  after a mode check. The backend is pluggable by design: messages
  reference keys by `kid` only, so swapping the file keystore for an OS
  keychain or vault changes **no wire bytes** and invalidates no pins.
- **Same-UID threat model, stated plainly:** any process running as the
  same user can read these key files. On a shared machine, cross-agent
  impersonation (one local agent signing as another) is **not
  cryptographically prevented in v0.4.0 warn mode** — the signature
  attests to a key, and key custody on-machine is only as strong as the
  file permissions. This is an accepted, documented limitation of the
  warn-mode rollout.
- **Keystore hardening is planned follow-up work** for v0.4.1+: OS
  keychain / vault-backed signer backends behind the same `kid` seam.

## Rotation and revocation

Rotation is overlap-based: `oacp key gen` mints a new key alongside the
old; senders sign with every local key (capped at 8), so a message
verifies against either pin while receivers import the new stub. Remove
the retired key file to stop signing with it; receivers revoke the old pin
with `oacp trust revoke <kid> --project <name> --agent <receiver>` (or
`--all-receivers` for every receiver in the project that pins it — the
compromise-response path). The revoke validates the kid's canonical
spelling up front (an alias spelling is refused rather than silently
missing the pin), refuses a kid no receiver pins, keeps the key material
in place on the revoked entry so the reader's re-validation still applies,
and re-emits the file canonically under the project trust lock. A revoked
pin refuses messages it has not seen — and import never brings it back to
life.

## Conformance

The wire format is pinned by a byte-exact conformance corpus at
`tests/conformance/signing/` — golden fixtures for the trailer boundary,
signed prefix bytes, and JWS preimages, plus a tamper-detection suite
(byte flips, trailer transplant, kid substitution, whitespace/EOL edge
cases). Implementations of the framing or the verify flow should run
against it; the corpus README defines which expected fields are
normative and why regenerating goldens requires a ruling.
