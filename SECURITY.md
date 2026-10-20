# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in OACP, please report it responsibly.

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on the affected repository when it is enabled. If private reporting is unavailable, use the repository's documented private security contact or another non-public maintainer channel.

## Response Timeline

- **Acknowledgment**: Within 48 hours of receiving your report.
- **Assessment**: We will evaluate the severity and impact within 7 days.
- **Fix**: Critical vulnerabilities will be patched within 30 days. We will coordinate disclosure timing with you.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.x     | Yes       |

## Scope

OACP is a file-based coordination protocol. Security considerations include:

- **Message authenticity** — Messages carry an Ed25519 signature trailer (`auth:`) that receivers verify against pinned signer identities (see `docs/protocol/message_signing.md`). Message bodies are not encrypted. Report any case where a receiver in `enforce` mode processes a message that is unsigned, fails verification, or comes from a signer it has not pinned.
- **Credential scoping** — The protocol defines credential boundaries per agent (see `docs/protocol/credential_scoping.md`). Report issues where these boundaries can be bypassed.
- **Script injection** — Report any case where user-controlled input in message fields can lead to command injection via OACP scripts.
- **Path traversal** — Report any case where message fields or script arguments can read or write files outside the intended directories.

### Local forgery: the same-user threat model

Signing keys live under `$OACP_HOME/keys/` as `0600` files in `0700`
directories, per machine, and are never synced. Every agent runtime on a
host runs as the same OS user by default, so **any process running as that
user can read every local agent's private key and produce a valid
signature as any local agent.** The same process can also write directly
into any inbox. A shell-capable local agent, a tool with filesystem access,
or a compromised session can therefore forge a message from any identity on
that host, and the receiver's verification will pass.

Signing is still worth having. It gives:

- **Tamper evidence** — a message altered after signing fails verification.
- **Cross-host attribution** — a peer on another machine can verify that a
  message came from a key enrolled on this one, and a key cannot be used from
  a host it was never copied to.
- **Signer control** — receivers accept only pinned keys, and a revoked or
  unknown key is rejected under `enforce` mode.
- **An auditable trail** for honest agents, stamped onto each receiver's
  audit record at intake.

It does not give **same-host anti-impersonation**. A verified signature
proves that the message was signed with a given key, not which local agent
held it at the time. On a shared host, treat both the `from:` field and a
verified signature as **provenance within the trust boundary of the OS
user**, not as proof of authorship by one agent over another.

Closing the gap is an isolation decision, not a protocol change: one OS user
per agent with its key directory owned by that user, containers, or separate
hosts with enforced access controls. Hardware- or keychain-backed signers are
planned behind the same key-id seam and would protect the key material from
copying, but they do not isolate a shared filesystem or process space.

Same-user forgery on a shared host is a documented limitation, not a
vulnerability; please do not report it as one. Do report any case where a
receiver accepts a message that should have failed verification.

## Acknowledgments

We appreciate responsible disclosure and will credit reporters in release notes (unless you prefer to remain anonymous).
