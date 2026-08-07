# Message Signing — Trust Root & Key Management

Status: **non-normative** companion to the message-signing wire format.
The wire format itself — the raw-prefix detached-JWS `auth` trailer — is
specified in [`inbox_outbox.md` → "Signed messages"](inbox_outbox.md);
this document covers the trust root, verification modes, and key
management.

## Verify modes

Receivers opt in per-agent via `signing.verify_mode: off | warn | enforce`
in `agents/<receiver>/config.yaml`; any other value degrades to `off`.

**Warn mode records identity and grants no authority.** Every verification
outcome — `unsigned`, `signed-verified`, `signed-unknown-kid`,
`signed-INVALID`, `signed-REVOKED` — produces an annotation and a
`message_auth` audit block; none of them rejects, quarantines-as-rejection,
or changes how a message is processed. A verified signature is a recorded
fact about who signed, not a permission.

**Enforce mode makes rejection mechanism, not receiver diligence.** The
autonomy gate invokes verification at message intake, before any
evaluation, parse, or gate runs. Only `signed-verified` proceeds; every
other outcome — unsigned, INVALID, unknown-kid, revoked, and
unverifiable-without-crypto — is rejected: a mode-600 evidence copy is
quarantined into the receiver's `dead_letter/` (exclusive-create; the
original inbox artifact is never touched), nothing is evaluated, and the
gate exits `3` with an `intake_rejected` decision object. An unusable or
tampered trust root fails closed the same way: with no loadable pins,
nothing verifies, so everything rejects.

### Receiver intake contract

What a receiver's inbox-processing flow does with each annotation, by mode:

| Annotation | `off` | `warn` | `enforce` |
|---|---|---|---|
| (none — verification skipped) | process | — | — |
| `unsigned` | — | process; annotation recorded | rejected at intake (quarantined, unprocessed) |
| `signed-verified` | — | process; identity recorded | process |
| `signed-unknown-kid` | — | process; annotation recorded | rejected at intake |
| `signed-INVALID` | — | process; evidence quarantine available (`--quarantine`) | rejected at intake |
| `signed-REVOKED` | — | process; annotation recorded | rejected at intake |
| `signed-unverifiable (crypto unavailable)` | — | process; annotation recorded | rejected at intake (fail closed) |

Under `enforce` the rejection happens inside the gate CLI — a receiver
flow that never explicitly runs `oacp verify` still cannot process an
unverified message, because admission itself refuses. The quarantined
evidence copy plus the gate's `intake_rejected` output are the record of
the rejection; no admission audit record is written for a message that
never reached admission. Receivers surface the rejection to their
operator and may notify the sender; they never process or silently delete
the quarantined evidence.

Every receiver read path verifies before it parses. The gate is the
admission choke point; every other CLI surface that reads inbox
artifacts goes through one shared receive boundary (resolve the
authorized receiver config, load policy-checked pins, verify one
bounded snapshot, and only then parse those bytes): the `oacp inbox`
lister, the `oacp watch` event emitter, the send helper's
parent-message lookup (a held message can never donate
`conversation_id` to an outgoing reply), and the trust drift report's
inbox traffic probe (a forged `from:` line cannot manufacture a
liveness signal). Under `enforce`, a message that is not
`signed-verified` surfaces as a HELD row or event built from
filesystem metadata only — none of its (attacker-controlled) fields
are parsed or surfaced; under `warn` rows and events carry the
verification status; under `off` behavior is unchanged. These read
paths are read-only, so they never quarantine — dispositioning a held
message belongs to the processing path. The envelope compiler is a
processing step and fails closed instead: under `enforce` it refuses
to compile an envelope from a message that is not signed-verified,
and the envelope's `message_sha256` names the verified snapshot.

**Verified bytes are the processed bytes.** Verification and any
subsequent parse, hash, or evaluation of the same artifact consume one
bounded read — a single snapshot. A verifier that approves one read and a
consumer that then re-reads the path would leave a swap window between
them; the gate, the inbox lister, and the policy loaders all parse the
exact bytes they verified, and the audit record's `message_sha256` names
that snapshot.

## Enforce-mode preparation

Before any receiver flips to `signing.verify_mode: enforce`, run a fleet-wide
re-pin sweep and then `oacp doctor --project <name>`. Every receiver must pin
every peer identity in the project catalog (a receiver's own catalog identity
is exempt), and every active receiver pin must refer to an identity recorded
in the catalog. Revoked pins remain as audit history and are excluded from the
pin-to-catalog completeness direction. Re-import the applicable public stub
for each missing relationship:

```bash
oacp trust import /path/to/<kid>.pub.json --project <name> --agent <receiver>
oacp doctor --project <name>
```

Doctor reports one aggregate `pin completeness` result with counts in both
directions across all receiver profiles. Gaps are warnings while the project
remains in warn mode. If any receiver explicitly configures enforce mode, the
same aggregate result becomes a blocking error so a missing pin cannot turn
into silent message rejection after the flip. Unlike the advisory
`catalog-not-pinned` drift signal, pre-enforce completeness intentionally has
no liveness exemption: every cataloged peer relationship must be ready before
any profile enforces.

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

## Policy-file signing

The autonomy audit record's `policy_sha256` proves **which** policy ran;
policy-file signing proves it was **authorized**. The receiver's two
policy files — `config.yaml` and `trust/allowed_signers.yaml` — carry the
same raw-prefix detached-JWS auth trailer as messages, under a distinct
JOSE profile (`typ: oacp-policy+yaml`, domain `urn:oacp:policy:v1`) so a
message signature can never authorize a policy file and a policy signature
can never authenticate a message.

- **Trust anchor**: the machine-local keystore under `$OACP_HOME/keys/`.
  A policy file for receiver X verifies only against agent X's own public
  keys (the `<kid>.pub.json` stubs written by `oacp key gen`); a valid
  signature by a *different* local agent's key is a receiver-binding
  failure, not authorization. Tampering with a policy file on disk
  therefore requires the 0600 private key material, not just filesystem
  write access. (The trust root cannot anchor its own signature — the
  keystore is the separate root that breaks that cycle.)
- **Context binding**: every policy signature commits to its target —
  `{project, receiver, kind}` (`receiver_config` or `allowed_signers`) —
  inside the protected header's `oacp.policy` claim, and verification
  requires an exact match against the file's canonical workspace
  location. A signature over one project's policy never authorizes a
  byte-identical file in another project, a config signature never
  authorizes a trust root (or vice versa), and a signed policy file
  copied outside its canonical location does not verify at all.
- **Sign and re-sign**:

  ```bash
  oacp trust sign-policy --project <name> --agent <receiver>
  ```

  signs both files with the receiver's own local key and round-trip
  verifies them. Unlike messages (append-once, immutable), policy files
  are long-lived and edited: signing strips any existing trailer and signs
  the current content — run it again after every policy edit.
- **Enrollment (downgrade resistance)**: the first successful
  `sign-policy` run enrolls each target in the machine-local registry
  `$OACP_HOME/keys/policy_enrollment.json` (part of the keystore trust
  anchor, outside every project workspace). From then on, that policy
  file without a verifiable signature — trailer stripped, or crypto
  unavailable — is `invalid`, never `unsigned`: stripping a signature is
  tampering, not a path back to bootstrap. Enrollment is recorded only
  after every target round-trip verifies, so a partial signing failure
  never strands an unsigned file behind downgrade resistance.
- **Writers re-sign or refuse**: trust mutations that re-emit
  `allowed_signers.yaml` (`oacp trust import` / `revoke`) atomically
  re-sign an enrolled trust root with the receiver's own key. If the
  signing key is unavailable, the mutation is refused before anything is
  written — a writer never strips an enrolled file's signature as a side
  effect, and a fleet-wide revoke either fully lands signed or leaves
  zero pins changed.
- **One authorized read path**: consumers load policy files through a
  single loader that reads the file once (bounded at 1 MiB), verifies
  those bytes, and parses the policy from the same snapshot — the bytes
  evaluated are always the bytes verified. This covers the autonomy gate,
  intake's trust-root load, the envelope compiler, the send helper's
  signing-intent read, trust mutations, and the inbox lister; `oacp
  doctor` reports each policy file's authorization status per receiver
  (verified / unsigned-bootstrap / invalid / unsupported) without
  blocking diagnostics.
- **Verified at load, fail closed on tamper**: loaders check the signature
  wherever the policy is consumed. The gate records the outcome in every
  decision as a `policy_auth` block (`status`, `signer_agent`,
  `signer_kid`, `reason`); together with `policy_sha256` the record
  commits to an authorized policy identity, not just bytes. A **tampered**
  `config.yaml` pauses the decision with reason code `policy_auth_invalid`
  before anything reads the config — including its own `verify_mode`, so a
  tamper cannot switch enforcement off. A **tampered**
  `allowed_signers.yaml` makes the trust root unusable exactly like an
  unreadable one: no pins load, and under `enforce` every inbound message
  consequently rejects. Both failures are distinguishable in the record
  from a policy that is merely *absent* (absent config is a usage error
  with no record; absent pins simply mean no pins).
- **Bootstrap**: a fresh workspace's policy files are unsigned and load
  normally with `policy_auth.status: unsigned` recorded — signing requires
  a key, so the order is `oacp init` → `oacp key gen --agent <receiver>` →
  `oacp trust sign-policy`. Unsigned is a visible, recorded state, never a
  silent one. On a host without the crypto extra, a signed but
  *unenrolled* policy file records `unsupported` and loads (signing
  itself always requires the extra); an *enrolled* one is `invalid` —
  fail closed, because that host's registry proves a signature is
  required.

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
- **Keystore hardening is planned follow-up work** for a later release: OS
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

The receive-path behavior — what a receiver *does* with each verification
outcome under each verify mode — is pinned separately by the intake corpus
at `tests/conformance/intake/`: four failure classes (unsigned /
signed-INVALID / unknown-kid / revoked) under `off`/`warn`/`enforce`, plus
a signed-verified positive control, executed against the real gate CLI.
