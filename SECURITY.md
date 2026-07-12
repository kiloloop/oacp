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

- **Message integrity** — YAML files are not signed or encrypted. OACP assumes a trusted shared filesystem. Do not expose the OACP home directory to untrusted users.
- **Credential scoping** — The protocol defines credential boundaries per agent (see `docs/protocol/credential_scoping.md`). Report issues where these boundaries can be bypassed.
- **Script injection** — Report any case where user-controlled input in message fields can lead to command injection via OACP scripts.
- **Path traversal** — Report any case where message fields or script arguments can read or write files outside the intended directories.

### Trust ceiling on shared hosts (Tier 1.5)

On a single-OS-user host, OACP cannot *cryptographically prevent* one agent
from impersonating another, because key-read access and inbox-write access
collapse to the same uid. Today, `from` fields are unauthenticated and provide
traceability only — not identity assurance. Message signing (an optional
extension under design, not yet implemented) would add tamper-evidence and
third-party-verifiable identity provenance for honest agents; it would still
not provide same-host anti-impersonation. Hard inter-agent isolation requires
one OS user per agent (Tier-2), containers, or separate hosts with enforced
access controls. Hardware-backed keys can strengthen future signing identity
assurance, but they do not isolate a shared filesystem or process space. Treat
any `from` field on a shared host as **provenance, not an anti-spoofing
guarantee.**

## Acknowledgments

We appreciate responsible disclosure and will credit reporters in release notes (unless you prefer to remain anonymous).
