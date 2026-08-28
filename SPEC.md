# OACP — Protocol Specification

**License**: Apache-2.0

OACP (Open Agent Coordination Protocol) is a file-based coordination layer for
multi-agent engineering workflows: agents on different runtimes (Claude, Codex,
Cursor, Gemini, or any future runtime) collaborate asynchronously through YAML
messages in a shared filesystem — no server, no daemon. The protocol is a small
kernel — the message envelope and its lifecycle, message signing and
verification, receiver autonomy with audit receipts, and the org-memory layout —
plus userland conventions (review loops, task negotiation, session init, safety
defaults) that build on it. This file is the index: the normative text lives in
the documents below and is versioned with the `oacp-cli` package (see
[`CHANGELOG.md`](CHANGELOG.md)).

## Kernel documents

What every receiver must understand or verify to exchange a message. These four
documents ship inside the `oacp-cli` wheel.

| Document | Governs |
|---|---|
| [`docs/protocol/inbox_outbox.md`](docs/protocol/inbox_outbox.md) | Wire format including the signed `auth` trailer; directory layout; message types; lifecycle — a processed inbound message is archived to `inbox/archive/` by digest-checked, no-clobber move (never deleted), and an intake rejection is quarantined to `dead_letter/`; threading, broadcast, expiry, retention. |
| [`docs/protocol/message_signing.md`](docs/protocol/message_signing.md) | Trust root and receiver pins, verify modes (`off` / `warn` / `enforce`), receiver audit stamping, policy-file signing, key management, rotation and revocation, signing conformance. |
| [`docs/protocol/autonomy.md`](docs/protocol/autonomy.md) | Receiver autonomy: config and task profiles, the four-gate admission evaluator and hard stops, audit records (admission ledger, human outcomes, terminal finalization), threshold checkpoints and re-authorization, scope-envelope enforcement, continuation grants. |
| [`docs/protocol/org_memory.md`](docs/protocol/org_memory.md) | Org-level memory: directory structure, event file schema, the debrief store, permission model and lifecycle. |

## Userland documents

Conventions layered on the kernel. Adopt what fits; none of them change what a
receiver must verify.

| Document | Covers |
|---|---|
| [`docs/protocol/review_loop.md`](docs/protocol/review_loop.md) | Inbox-driven code review: stateless reviewer rounds, findings packets, quality gate, round budgets, post-LGTM nits. |
| [`docs/protocol/task_negotiation.md`](docs/protocol/task_negotiation.md) | Propose / accept / counter-propose handshake for splitting work between agents. |
| [`docs/protocol/multi_agent_shared_workspace.md`](docs/protocol/multi_agent_shared_workspace.md) | Shared-folder implementation → QA → deployment handoff with batched findings and signoff. |
| [`docs/protocol/session_init.md`](docs/protocol/session_init.md) | Runtime-agnostic session-start sequence and failure handling. |
| [`docs/protocol/cross_runtime_sync.md`](docs/protocol/cross_runtime_sync.md) | Keeping context consistent across runtimes: durable memory, handoff messages, review artifacts. |
| [`docs/protocol/runtime_capabilities.md`](docs/protocol/runtime_capabilities.md) | Static capability declarations, dynamic `status.yaml`, health-check contract, agent cards. |
| [`docs/protocol/agent_profiles.md`](docs/protocol/agent_profiles.md) | Two-tier identity: global agent profiles and project-level agent cards. |
| [`docs/protocol/agent_safety_defaults.md`](docs/protocol/agent_safety_defaults.md) | Baseline git, staging, inbox, credential, and scope rules every agent follows. |
| [`docs/protocol/credential_scoping.md`](docs/protocol/credential_scoping.md) | Per-agent, per-project least-privilege credentials and rotation. |
| [`docs/protocol/mcp_integration.md`](docs/protocol/mcp_integration.md) | Attaching MCP tool outputs to findings as structured evidence. |
| [`docs/protocol/dispatch_states.yaml`](docs/protocol/dispatch_states.yaml) · [`packet_states.yaml`](docs/protocol/packet_states.yaml) · [`skills_manifest.yaml`](docs/protocol/skills_manifest.yaml) | Machine-readable dispatch and review-packet state machines, and the skills manifest. |

Guides — [`docs/guides/setup.md`](docs/guides/setup.md),
[`adoption.md`](docs/guides/adoption.md), [`doctor.md`](docs/guides/doctor.md),
[`versioning.md`](docs/guides/versioning.md),
[`unified_skill_spec.md`](docs/guides/unified_skill_spec.md) — and the
executable conformance fixtures under [`tests/conformance/`](tests/conformance/)
(autonomy, signing, intake, envelope) round out the set. Runtime-specific skills
that operate the protocol live in the companion
[oacp-skills](https://github.com/kiloloop/oacp-skills) repository.

## Getting started

[`QUICKSTART.md`](QUICKSTART.md) sends a first message in five minutes;
[`README.md`](README.md) carries the command reference (`oacp --help` is
authoritative).
