# Multi-Agent Shared Workspace Protocol

## Purpose

This protocol defines a shared-folder workflow for implementation, QA, and deployment handoff across agents.

Core intent:
- make handoffs explicit
- batch QA findings
- cap asynchronous feedback rounds
- preserve durable memory independent of model context windows

## Roles

Roles are **per-project**, defined in `memory/project_facts.md`. The protocol does not hardcode which agent fills which role.

Standard role types:
- **Implementer**: writes code, creates Review Packet, addresses findings.
- **QA/Reviewer**: returns batched Findings Packet with evidence.
- **Deploy/Ops**: owns rollout, rollback, infra changes.

A single agent may hold multiple roles. Multiple agents may share the Implementer role across different domains.

## Required Artifacts

1. Review Packet (`packets/review/<packet_id>.md`)
2. Findings Packet (`packets/findings/<packet_id>.yaml`)
3. Merge Decision (`merges/<packet_id>.md`)
4. Checkpoint(s) (`checkpoints/<date>_<topic>_<state>.md`) — optional, for mid-flight handoffs

Templates are in `templates/`.

## Workflow

1. Implementer initializes packet files (`scripts/init_packet.sh`).
2. Implementer fills Review Packet with scope, risk map, tests run, rollback notes.
3. QA returns one batched Findings Packet for the round.
4. Implementer addresses findings in one batch and publishes Merge Decision.
5. Repeat for round 2 only if needed.
6. If blocking items remain after round 2, escalate to synchronous decision.

## Loop Controls

- One Findings Packet per reviewer per round.
- Severity required: `P0`/`P1`/`P2`/`P3`.
- `blocking` required for each finding.
- `status` required for each finding (`open`/`fixed`/`wont_fix`).
- Max asynchronous rounds: 2.

## Quality Gate

A change is merge-ready only if:
- no unresolved `P0`
- no unresolved `blocking: true` findings (unless explicitly waived)
- requested validation commands are recorded with outcomes
- deploy-affecting changes include rollback notes

## QA Signoff

A Merge Decision cannot close a packet (transition to `merged`) without an explicit QA signoff.

### What constitutes signoff

QA signoff confirms that:
- All `P0` findings are resolved (`fixed` or `wont_fix` with documented rationale).
- No `blocking: true` findings remain unresolved (unless explicitly waived with rationale).
- Validation commands from the Findings Packet have been re-run post-fix and outcomes are recorded.

### Who can sign off

The agent holding the **QA/Reviewer** role for the packet. The reviewer who authored the Findings Packet is the expected signer, but any agent assigned the QA/Reviewer role for the project may sign off.

### Where signoff is recorded

Signoff is recorded in a `## QA Signoff` block at the bottom of the Merge Decision file (`merges/<packet_id>.md`). The block contains:
- `signed_off_by` — agent name of the reviewer
- `date_utc` — ISO 8601 timestamp
- `verdict` — `approved` or `rejected`
- `notes` — optional free-text (e.g., waivers granted, caveats)

### What happens without signoff

If the QA Signoff block is missing or the verdict is `rejected`, the packet **cannot** transition from `merge_decision` to `merged`. The implementer must either obtain signoff or escalate per the round-limit rules.

## Naming Convention

Use:

`<YYYYMMDD>_<topic>_<owner>_r<round>`

Example packet IDs:
- `20260211_hunter_lock_codex_r1`
- `20260211_hunter_lock_codex_r2`

## Inbox / Outbox Messaging

Each agent has `agents/<name>/inbox/` and `agents/<name>/outbox/` directories for async point-to-point messages.

See `docs/protocol/inbox_outbox.md` for the full contract.

Summary:
- Messages are YAML files dropped into the recipient's `inbox/`.
- Sender keeps a copy in their own `outbox/`.
- Recipient processes and deletes (or moves to outbox as reply).
- Use for: task requests, questions, notifications, handoff signals.
- Do NOT use for: findings or review content (use packets for those).

## Artifacts

The `artifacts/` directory provides a single location for agents to find shared project artifacts without knowing repository internals.

When initialized with `--repo`, the script creates symlinks from the target repository into `artifacts/` so agents can discover shared files without knowing repo internals.

Projects should customize the symlink targets to match their repository layout. Common artifact types include session reports, handover documents, planning summaries, and build outputs.

The artifacts directory is not for packet-based review workflow data — use `packets/` for that.

## Durable Memory Promotion

Only stable outcomes go to `memory/`:
- decisions with rationale
- unresolved threads/blockers
- stable project facts

Do not store raw logs or long command output in durable memory.
