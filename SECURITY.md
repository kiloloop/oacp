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

## Acknowledgments

We appreciate responsible disclosure and will credit reporters in release notes (unless you prefer to remain anonymous).
