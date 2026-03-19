# OACP — Protocol Specification

**Version**: 0.1.0
**License**: Apache-2.0

This document is the protocol specification for OACP (Open Agent Coordination Protocol) — a file-based coordination layer for multi-agent engineering workflows. It defines the message formats, state machines, review processes, and safety rules that enable agents on different runtimes (Claude, Codex, Gemini, or any future runtime) to collaborate asynchronously through a shared filesystem.

OACP is not a framework or SDK. It is a set of conventions, YAML schemas, and shell scripts that any agent runtime can implement.

---

## Table of Contents

1. [Protocol Overview](#1-protocol-overview)
2. [Dispatch State Machine](#2-dispatch-state-machine)
3. [Review Loop Protocol](#3-review-loop-protocol)
4. [Cross-Runtime Sync](#4-cross-runtime-sync)
5. [Agent Safety Defaults](#5-agent-safety-defaults)
6. [Kernel Script Inventory](#6-kernel-script-inventory)

---

## 1. Protocol Overview

Full specification: [`docs/protocol/inbox_outbox.md`](docs/protocol/inbox_outbox.md)

### Core Concept

Agents communicate through a shared filesystem using YAML messages. Each agent has an inbox and outbox directory within a project workspace:

```
$OACP_HOME/projects/<project>/
├── agents/
│   ├── claude/
│   │   ├── inbox/          # Other agents write here
│   │   ├── outbox/         # Claude's sent messages (copies)
│   │   ├── status.yaml     # Dynamic agent state
│   │   └── agent_card.yaml # Static agent identity
│   ├── codex/
│   │   ├── inbox/
│   │   ├── outbox/
│   │   └── ...
│   └── gemini/
│       └── ...
├── memory/                  # Shared durable memory
│   ├── project_facts.md
│   ├── decision_log.md
│   └── open_threads.md
├── packets/                 # Review/findings artifacts
│   ├── review/
│   └── findings/
├── merges/                  # Merge decision records
└── workspace.json           # Project metadata
```

### Message Format

Messages are YAML files. Filename convention: `<timestamp>_<from>_<type>.yaml`

**Required fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique message ID: `msg-<timestamp>-<sender>-<rand>` |
| `from` | string | Sender agent name |
| `to` | string or list | Recipient(s) — string for point-to-point, list for broadcast (max 10) |
| `type` | string | Message type (see below) |
| `priority` | string | `P0` \| `P1` \| `P2` \| `P3` |
| `created_at_utc` | string | ISO 8601 UTC timestamp |
| `subject` | string | Subject line |
| `body` | string | Message content (multi-line markdown) |

**Optional fields:** `expires_at`, `channel`, `related_packet`, `related_pr`, `conversation_id`, `parent_message_id`, `context_keys`

### Message Types

| Type | Purpose | Response Expected |
|------|---------|-------------------|
| `task_request` | Assign work to an agent | Reply or artifact |
| `question` | Request information or a decision | Reply message |
| `notification` | FYI update | None (unless reporting findings) |
| `follow_up` | Track non-blocking deferred work | None |
| `handoff` | Transfer ownership of in-flight work | `handoff_complete` |
| `handoff_complete` | Confirm handoff target is done | None |
| `review_request` | Request code review of a PR | `review_feedback` or `review_lgtm` |
| `review_feedback` | Return review findings | `review_addressed` |
| `review_addressed` | Report that feedback was addressed | New `review_request` |
| `review_lgtm` | Approve — quality gate passed | None (author may merge) |
| `brainstorm_request` | Open-ended research/analysis | Report in outbox |
| `brainstorm_followup` | Amend in-progress brainstorm scope | Incorporated into next round |

### Message Lifecycle

1. Sender creates a YAML message file.
2. Sender writes to `agents/<recipient>/inbox/<filename>.yaml`.
3. Sender copies to `agents/<sender>/outbox/<filename>.yaml`.
4. Recipient reads from inbox when polling or at session start.
5. Recipient deletes from inbox after processing.
6. Replies are new messages written to the original sender's inbox.

### Polling Convention

Agents check their inbox at session start and after completing each major task. There is no real-time notification — this is a poll-based protocol. Wait states use shell/script polling loops, not LLM turns.

### Processing Order

Process by priority: P0 first, then P1, then P2/P3. Within the same priority, process oldest first (by filename timestamp). `task_request` and `review_request` take precedence over `notification` and `follow_up` at the same priority level.

### Conversation Threading

Optional fields enable multi-message threading:

- **`conversation_id`** (`conv-<YYYYMMDD>-<agent>-<seq>`) — groups related messages
- **`parent_message_id`** — links a reply to the message it responds to
- **`context_keys`** — concise summary of prior context (under 500 words)

### Broadcast

The `to` field accepts a list for multi-recipient delivery. One copy per recipient inbox, one copy in sender's outbox. Max 10 recipients. `handoff` and `handoff_complete` are point-to-point only.

### Task Negotiation

Agents can negotiate work splits before starting implementation using a structured propose/ack/counter handshake. Full specification: [`docs/protocol/task_negotiation.md`](docs/protocol/task_negotiation.md).

---

## 2. Dispatch State Machine

Full specification: [`docs/protocol/dispatch_states.yaml`](docs/protocol/dispatch_states.yaml)

The dispatch state machine tracks the lifecycle of a `task_request` from delivery to completion. It has two tracks: agent-side (the agent handling the task) and dispatcher-side (the orchestrator tracking progress).

### Agent-Side States

```
received → accepted → working → pr_opened → in_review → done
   ↘          ↘                    ↘             ↑
 rejected    blocked              blocked   changes_requested
                                     ↘
                                    failed
```

| State | Description | Terminal |
|-------|-------------|----------|
| `received` | `task_request` delivered to inbox | No |
| `accepted` | Agent has read and accepted the task | No |
| `working` | Agent is actively implementing | No |
| `pr_opened` | Agent has opened a PR (work-in-progress) | No |
| `in_review` | PR is under cross-agent review | No |
| `changes_requested` | Reviewer requested changes | No |
| `done` | PR merged or deliverable provided | **Yes** |
| `rejected` | Agent cannot or will not complete the task | **Yes** |
| `blocked` | Agent is blocked and cannot continue | No |
| `failed` | Agent crashed or hit an unrecoverable error | No |

### Key Transitions

- **`received` → `accepted`**: Agent reads and accepts the task. Ack recommended for P0/P1.
- **`working` → `pr_opened`**: Agent opens a PR. **Notification required** with `WIP:` subject prefix.
- **`pr_opened` → `in_review`**: Agent requests cross-agent review via `review_request`.
- **`in_review` → `done`**: PR merged after review approval. Guard: `pr_merged AND review_approved`.
- **`working` → `done`**: Non-PR task completed (research, advisory).
- **`failed` → `working`**: Retry requires explicit dispatcher authorization (prevents retry storms).

### Dispatcher-Side States

The dispatcher (e.g., an orchestrator agent) tracks dispatches from its perspective:

| State | Trigger |
|-------|---------|
| `sent` | Dispatch message written to agent inbox |
| `ackd` | Agent acknowledged receipt |
| `in_progress` | Within SLA window, no PR yet |
| `in_review` | WIP notification received with `related_pr` |
| `done` | Final notification with merge SHA or results |
| `responded` | Agent replied to non-PR task (terminal for non-PR tasks) |
| `blocked` | Agent reported a blocker |
| `rejected` | Agent rejected the task |
| `overdue` | No response beyond SLA threshold (computed) |

### SLA Thresholds

| Priority | First Response SLA |
|----------|--------------------|
| P0 | 2 hours |
| P1 | 24 hours |
| P2 | 48 hours |
| P3 | No SLA |

### Done Criteria

- **PR-based tasks**: done = PR merged, not opened
- **Non-PR tasks**: done = deliverable provided or question answered
- Final notification must include `merge_sha` (PR tasks) or `results_summary` (non-PR tasks)

### Brainstorm Lifecycle

Brainstorm messages follow a simpler lifecycle: `received` → `researching` → `report_delivered`. No PR, no review loop. The dispatcher sees: `Sent` → `In progress` → `Responded`.

---

## 3. Review Loop Protocol

Full specification: [`docs/protocol/review_loop.md`](docs/protocol/review_loop.md)
Packet state machine: [`docs/protocol/packet_states.yaml`](docs/protocol/packet_states.yaml)
Shared workspace protocol: [`docs/protocol/multi_agent_shared_workspace.md`](docs/protocol/multi_agent_shared_workspace.md)

### Two Review Mechanisms

OACP provides two complementary review mechanisms:

1. **Packet-based review** — formal review/findings/merge artifact lifecycle for heavyweight changes. Defined in `multi_agent_shared_workspace.md`.
2. **Inbox-based review loop** — lightweight, message-driven review for PR-level code review. Defined in `review_loop.md`.

Both share the same quality gate criteria.

### Packet-Based Review Flow

```
Implementer creates Review Packet
        ↓
Reviewer returns Findings Packet (batched)
        ↓
Implementer addresses findings + publishes Merge Decision
        ↓
If blockers remain → Round 2 (max 2 async rounds)
        ↓
If still unresolved → Escalate to synchronous decision
```

**Packet state machine**: `submitted` → `in_review` → `findings_returned` → `fixing` → `merge_decision` → `merged` (or `escalated`).

**Artifacts:**
- Review Packet (`packets/review/<packet_id>.md`) — scope, risk map, validation results, rollback notes
- Findings Packet (`packets/findings/<packet_id>.yaml`) — severity (`P0`-`P3`), `blocking` flag, `status` per finding
- Merge Decision (`merges/<packet_id>.md`) — resolution table, post-fix validation, remaining blockers, QA signoff

**Naming convention**: `<YYYYMMDD>_<topic>_<owner>_r<round>`

### Inbox-Based Review Loop

The review loop uses four message types over the inbox protocol:

```
Author                              Reviewer (invocation A)
  |                                        |
  |─── review_request ───────────────────→ |  (PR#, branch, summary, budgets)
  |                                        |
  |←── review_feedback OR review_lgtm ──── |  (one terminal response, then exit)
  |
  |─── review_addressed ────────────────→  |  (commit SHA, addressed summary)
  |─── review_request (round N+1) ──────→ |  (explicit re-invocation)
  |                                        |
  |←── review_feedback OR review_lgtm ──── |  (invocation B terminal response)
```

**Stateless reviewer model**: A reviewer invocation handles exactly one round, produces one terminal response (`review_feedback` or `review_lgtm`), and exits. The author coordinates multi-round progression by re-invoking the reviewer.

### Quality Gate

A change is merge-ready when:

- No unresolved `P0` findings
- No unresolved blocking findings (`P1` and blocking `P2`)
- Deferred non-blocking findings (`P2`/`P3`) captured in `review_lgtm.nits` with owner and tracking reference
- All validation commands recorded with passing outcomes
- Deploy-affecting changes include rollback notes (packet-based review)
- QA signoff recorded with verdict `approved` (packet-based review)

### Risk-Tiered Finding Outcomes

| Severity | Merge Impact | Exit Path |
|----------|-------------|-----------|
| `P0` | Always blocking | Must fix before LGTM |
| `P1` | Blocking | Must fix before LGTM |
| `P2` | Reviewer judgment | Block if material risk; otherwise defer to `review_lgtm.nits` |
| `P3` | Non-blocking | Capture in `review_lgtm.nits`, proceed to LGTM |

### Round Limits and Budget Controls

- **Default max rounds**: 2 (configurable up to 3 per project)
- **Reviewer turn budget**: `max_turns_reviewer` (default 8)
- **Reviewer time budget**: `max_runtime_s_reviewer` (default 600s)
- Budget exhaustion triggers `review_feedback` with `escalation: reviewer_budget_exceeded`

### Post-LGTM Nit Lifecycle

Non-blocking items deferred at LGTM follow this lifecycle:

1. **Capture** (reviewer) — include in `review_lgtm.body.nits` with `nit_id`, `owner`, `next_action`
2. **Adopt** (author, at merge) — confirm ownership, preserve nits list
3. **Batch** (author, within 24h) — open tracking issue per PR
4. **Resolve or expire** — 14-day status update, 30-day close/escalate (or `expires_at_utc`)

---

## 4. Cross-Runtime Sync

Full specification: [`docs/protocol/cross_runtime_sync.md`](docs/protocol/cross_runtime_sync.md)
Session init protocol: [`docs/protocol/session_init.md`](docs/protocol/session_init.md)
Runtime capabilities: [`docs/protocol/runtime_capabilities.md`](docs/protocol/runtime_capabilities.md)

### Problem

Multi-agent workflows span different runtimes — each with its own context window, memory mechanism, and conversation state. Without explicit sync, agents lose context at handoff boundaries, duplicate decisions, or contradict prior work.

### Three Sync Mechanisms

#### 1. Durable Memory Files (most durable)

Location: `$OACP_HOME/projects/<project>/memory/`

| File | Purpose |
|------|---------|
| `project_facts.md` | Agent roles, repo structure, architecture, conventions |
| `decision_log.md` | Timestamped decisions with rationale |
| `open_threads.md` | Unresolved issues, blocked epics, cross-agent coordination |

All runtimes read these at session start. Only stable, verified outcomes are written here. Promotion flows through merge decisions via a project-defined durable-memory promotion mechanism.

#### 2. Handoff Messages with Context Keys (ephemeral)

When handing off work between agents (especially across runtimes), the sender includes `context_keys` in the handoff message — decisions made, artifacts produced, open questions, and what was tried and failed.

#### 3. Packet-Based Review Artifacts (structured)

Review packets, findings packets, and merge decisions form a structured audit trail. Any runtime can parse the fixed schema to understand what was reviewed, found, and resolved.

### Sync Points

| Sync Point | Direction | Action |
|------------|-----------|--------|
| Session start | Memory → Agent | Read all 3 memory files |
| Task completion | Agent → Memory | Write stable outcomes via merge decision |
| Handoff | Agent → Agent | Include `conversation_id` + `context_keys` |
| Review cycle start | Packets → Agent | Read relevant packet history |
| PR merge | Agent → Memory | Update memory if conventions/architecture changed |

### Session Init Protocol

Agents follow a 6-step init sequence at session start:

1. **Load global rules** (required) — safety defaults, tool preferences
2. **Load project rules** (required) — repo structure, conventions
3. **Load durable memory** (required) — project facts, decisions, open threads
4. **Check inbox** (optional) — summarize pending messages
5. **Load skills/tools** (optional) — runtime-specific capabilities
6. **Report status** (required) — update `status.yaml`

All init failures are degraded mode, not hard blocks. Safety defaults from `agent_safety_defaults.md` cannot be relaxed by project rules.

### Runtime-Specific Notes

| Concern | Claude | Codex | Gemini |
|---------|--------|-------|--------|
| Config file | `CLAUDE.md` | `AGENTS.md` | System prompt / `.agent/rules/` |
| Memory loading | Auto-loaded or explicit read | Explicit read at session start | Must be explicit (session-scoped memory) |
| Context keys at handoff | Important for all | Especially important (ephemeral sessions) | Critical (no cross-session persistence) |

### Agent Status

Each agent publishes `status.yaml` reflecting current state:

```yaml
runtime: claude
model: claude-opus-4-6
status: available          # available | busy | offline
current_task: ""
capabilities:
  - headless
  - shell_access
  - git_ops
updated_at: "2026-01-15T10:00:00Z"
```

Status is updated at session init, task start, task complete, and session close. Stale threshold: 1 hour.

### Agent Cards

Static identity files (`agent_card.yaml`) declare skills, capabilities, permissions, and protocol bindings. They complement `status.yaml` (dynamic state) with stable metadata for discovery and routing. Schema inspired by Google's A2A Agent Card spec, adapted for file-based transport.

---

## 5. Agent Safety Defaults

Full specification: [`docs/protocol/agent_safety_defaults.md`](docs/protocol/agent_safety_defaults.md)
Credential scoping: [`docs/protocol/credential_scoping.md`](docs/protocol/credential_scoping.md)

### Baseline Rules

These defaults apply to all agents (Claude, Codex, Gemini) unless a project-level config explicitly overrides a specific rule. Safety defaults can only be made **stricter** by project rules, never relaxed.

#### Git Safety

- No push/deploy without explicit approval from the user or dispatcher
- No destructive commands (`push --force`, `reset --hard`, `branch -D`, `clean -f`, `checkout .`) unless explicitly requested
- No direct-to-main pushes — all changes require a PR
- No hook bypasses (`--no-verify`, `--no-gpg-sign`) unless explicitly requested
- New commits over amend — after a pre-commit hook failure, fix the issue and create a new commit

#### Staging Hygiene

- Stage only files relevant to the current task — no `git add .` or `git add -A`
- No secrets or credentials in commits
- Verify before commit — `git status` and `git diff --staged` before every commit

#### Inbox Safety

- Delete inbox messages only after fully processing them
- Never silently consume a message that expects a response
- Always reply with `--parent-message-id` to maintain threading
- Idempotent sends — check for duplicates before sending to prevent retry storms

#### Scope Discipline

- Do not edit files outside the requested scope
- Do not modify auth, config, or secrets without explicit approval
- Do not install packages or change dependencies unless required and approved
- Do not create files unnecessarily — prefer editing existing files

### Credential Scoping

Full specification: [`docs/protocol/credential_scoping.md`](docs/protocol/credential_scoping.md)

Agents operate under least-privilege credentials:

- **Per-agent credentials** — each agent gets its own tokens. No sharing. Provides audit trail, blast-radius containment, and independent rotation.
- **Per-project boundaries** — credentials are scoped to the project they serve.
- **Environment variables only** — credentials loaded from env vars at runtime, never stored in version-controlled files.
- **Rotation support** — agents pick up new credentials on next invocation. Rotate at least every 90 days.

**Permission model by role:**

| Role | GitHub Permissions |
|------|-------------------|
| Implementer (Claude, Codex) | `contents: write`, `pull_requests: write`, `issues: write` |
| QA/Reviewer (Gemini) | `contents: read`, `pull_requests: read`, `issues: write` |
| Poll daemon | `pull_requests: read`, `issues: read` |

---

## 6. Kernel Script Inventory

The kernel boundary was established through a classification audit of all project files.

OACP ships a **kernel** — the minimal set of scripts, templates, and docs needed to adopt the protocol. Everything else is internal tooling for advanced orchestration workflows.

### Kernel Scripts (14)

These scripts ship with the OSS release. Most are stdlib-only Python or POSIX shell with no external dependencies beyond `python3`, `git`, and `gh`. Exception: `preflight.py` also requires `ruff`, `shellcheck`, and optionally `pyyaml` for YAML validation.

| Script | Purpose |
|--------|---------|
| `check_quality_gate.py` | Validates findings packets against merge-readiness criteria |
| `create_handoff_packet.py` | CLI for creating structured handoff packets |
| `handoff_schema.py` | Shared validation library for handoff and message schemas |
| `oacp_doctor.py` | Environment and workspace health check (flutter-doctor-style) — CLI: `oacp doctor` |
| `init_packet.sh` | Bootstraps review/findings/merge packet directories |
| `init_project_workspace.py` | Creates a new project workspace — CLI: `oacp init` |
| `add_agent.py` | Add an agent to an existing project workspace — CLI: `oacp add-agent` |
| `setup_runtime.py` | Generate runtime-specific config files — CLI: `oacp setup` |
| `normalize_findings.py` | Converts raw reviewer output to canonical findings YAML |
| `preflight.py` | Unified quality checks — CI runs this on every PR |
| `send_inbox_message.py` | CLI for all inbox messaging — CLI: `oacp send` |
| `update_workspace.sh` | Idempotent workspace sync across protocol versions |
| `validate_agent_card.py` | Validates agent card YAML against the schema |
| `validate_message.py` | Validates inbox/outbox message YAML — CLI: `oacp validate` |

### Kernel Templates (19)

Core packet, role, and guardrail templates. Includes:

- Packet templates: `review_packet`, `findings_packet`, `merge_decision`, `test_packet`, `checkpoint`, `handoff_packet`, `manual_validation`
- Messaging: `inbox_message`
- Agent identity: `agent_card`, `agent_status`, `skills_manifest`
- CI: `github_actions_quality_gate.yaml`
- Roles: `role_baseline`, `role_definition`
- Guardrails: `coding_standards`, `safe_commands`, `secrets_rules`
- Claude adapters: `role_agent`, `guardrail`

### Kernel Docs (19)

All protocol specs (13) and guides (6). See the audit for the complete file list.

### Internal-Only (63 files)

Not shipped in the OSS release. Includes orchestration scripts (brainstorm, task board, polling, lock primitives), runtime-specific adapters, analytics, brainstorm templates, prompt templates, dispatch/executor workflows, and planning docs.

### Kernel Boundary Criteria

A file is **kernel** if an external adopter needs it to use the base protocol. A file is **internal** if it's only needed for advanced orchestration, our specific ops workflows, or runtime-specific automation that adopters should implement themselves.

---

## Appendix: Quick Start

```bash
# 1. Initialize a project workspace
oacp init <project-name>

# 2. Verify environment health
oacp doctor --project <project-name>

# 3. Add an agent to the workspace
oacp add-agent <project-name> alice --runtime claude

# 4. Send a task request
oacp send <project> \
  --from claude --to codex --type task_request \
  --subject "Implement feature X" --body "Details..."

# 5. Check inbox for messages
# (list YAML files in agents/<name>/inbox/)

# 6. Run quality checks
make preflight

# 7. Validate a message file
oacp validate path/to/message.yaml

# 8. Initialize review packets
scripts/init_packet.sh <project> <packet_id>  # (no CLI wrapper yet)
```

## Cross-References

| Topic | Document |
|-------|----------|
| Inbox/outbox messaging | [`docs/protocol/inbox_outbox.md`](docs/protocol/inbox_outbox.md) |
| Dispatch states | [`docs/protocol/dispatch_states.yaml`](docs/protocol/dispatch_states.yaml) |
| Review loop | [`docs/protocol/review_loop.md`](docs/protocol/review_loop.md) |
| Packet states | [`docs/protocol/packet_states.yaml`](docs/protocol/packet_states.yaml) |
| Shared workspace | [`docs/protocol/multi_agent_shared_workspace.md`](docs/protocol/multi_agent_shared_workspace.md) |
| Cross-runtime sync | [`docs/protocol/cross_runtime_sync.md`](docs/protocol/cross_runtime_sync.md) |
| Session init | [`docs/protocol/session_init.md`](docs/protocol/session_init.md) |
| Safety defaults | [`docs/protocol/agent_safety_defaults.md`](docs/protocol/agent_safety_defaults.md) |
| Credential scoping | [`docs/protocol/credential_scoping.md`](docs/protocol/credential_scoping.md) |
| Runtime capabilities | [`docs/protocol/runtime_capabilities.md`](docs/protocol/runtime_capabilities.md) |
| Task negotiation | [`docs/protocol/task_negotiation.md`](docs/protocol/task_negotiation.md) |
| MCP integration | [`docs/protocol/mcp_integration.md`](docs/protocol/mcp_integration.md) |
| Skills manifest | [`docs/protocol/skills_manifest.yaml`](docs/protocol/skills_manifest.yaml) |
| Setup guide | [`docs/guides/setup.md`](docs/guides/setup.md) |
| Adoption guide | [`docs/guides/adoption.md`](docs/guides/adoption.md) |
