# Task Negotiation Protocol

## Purpose

Define the handshake protocol for cross-agent task negotiation via inbox/outbox messaging. This protocol governs how agents propose, accept, or counter-propose work splits before beginning implementation.

## Relationship to Inbox/Outbox

Task negotiation builds on the existing inbox/outbox messaging protocol (`docs/protocol/inbox_outbox.md`). It uses standard message types (`task_request`, `notification`) with additional structured fields in the body for negotiation state.

## Directionality

Messages are **receiver-addressed**:

- `-> codex` means "this message is directed at codex" (written to codex's inbox).
- `-> claude` means "this message is directed at claude" (written to claude's inbox).

The sender is metadata (`from:` field) — it does not determine routing. The receiver's inbox location determines where the file is written.

## Handshake Flow

```
Agent A                          Agent B
   |                                |
   |  1. propose (task_request)     |
   |------------------------------->|
   |                                |
   |  2. ack / counter (notification)|
   |<-------------------------------|
   |                                |
   |  3. ack-back (notification)    |
   |------------------------------->|
   |                                |
   |  4. Both apply GitHub labels   |
   |  5. Both start work            |
```

### Step 1: Propose

Agent A sends one or more `task_request` messages to Agent B's inbox, each describing a proposed assignment. Agent A also sends a summary `notification` with the full proposed split.

### Step 2: ACK or Counter

Agent B reviews the proposal and responds with a `notification`:

- **Accept**: Subject starts with `ACK -- `. Body confirms the proposed split.
- **Counter-propose**: Subject starts with `COUNTER -- `. Body describes the alternative split with rationale.

### Step 3: ACK-back

Agent A confirms receipt:

- If Agent B accepted: Agent A sends `ACK -- ` notification confirming execution plan.
- If Agent B counter-proposed: Agent A evaluates and responds with either `ACK -- ` (accepting the counter) or another `COUNTER -- ` (round 2).

### Step 4: Apply Labels

Only after both sides reach `decision: accept` with a reciprocal ack:

- Update GitHub issue labels (e.g., `owner:claude`, `owner:codex`).
- Update GitHub issue assignees if applicable.
- No label or state changes during negotiation.

### Step 5: Start Work

Both agents begin implementation in parallel on their agreed assignments.

## Message Conventions

### Message Types

| Step | Message Type | Subject Prefix |
|------|-------------|----------------|
| Propose | `task_request` | (free-form, describes the proposed task) |
| Propose summary | `notification` | `PROPOSAL -- <topic>` |
| Accept | `notification` | `ACK -- <topic>` |
| Counter-propose | `notification` | `COUNTER -- <topic>` |
| Ack-back | `notification` | `ACK -- <topic>` |

### Structured Body Fields

Negotiation messages include structured YAML fields in the `body:` in addition to free-form text:

```yaml
id: "msg-20260212T0900Z-claude-001"
from: "claude"
to: "codex"
type: "notification"
priority: "P1"
created_at_utc: "2026-02-12T09:00:00Z"
subject: "PROPOSAL -- Phase 4 task split"
body: |
  negotiation_id: "neg-20260212-phase4"
  round: 1
  decision: "propose"
  proposal_issues:
    claude: [32, 62, 38, 37, 29]
    codex: [30, 31, 24, 25, 26]
  proposal_split: |
    Claude takes protocol docs + Python scripts.
    Codex takes scaling infra + orchestration work.
  respond_by_utc: "2026-02-12T10:00:00Z"

  # Optional execution hints
  receiver_runtime: "codex"
  task_kind: "greenfield"
  execution_mode: "headless"
  branch_prefix: "codex/"
  expected_outputs:
    - "docs/protocol/credential_scoping.md"
    - "scripts/generate_proposal.py"
  reply_required: true
```

### Field Reference

| Field | Required | Description |
|-------|----------|-------------|
| `negotiation_id` | Yes | Stable ID linking all messages in a negotiation. Format: `neg-<YYYYMMDD>-<topic>` |
| `round` | Yes | Negotiation round number (1 or 2) |
| `decision` | Yes | One of: `propose`, `accept`, `counter` |
| `proposal_issues` | Yes (rounds with proposals) | Map of agent name to list of GitHub issue numbers |
| `proposal_split` | Yes (rounds with proposals) | Human-readable description of the split rationale |
| `respond_by_utc` | No | Suggested deadline for response. Advisory, not enforced. |
| `receiver_runtime` | No | Hint: `codex`, `claude`, or `gemini` |
| `task_kind` | No | Hint: `fix_loop`, `greenfield`, `review`, `infra` |
| `execution_mode` | No | Hint: `headless`, `interactive` |
| `branch_prefix` | No | Suggested branch prefix for the receiver's work |
| `expected_outputs` | No | List of files/artifacts the receiver is expected to produce |
| `reply_required` | No | Whether the sender expects a response (default: `true` for proposals) |

## Rules

1. **No GitHub label or state changes until handshake completes.** Labels like `owner:<agent>` and issue assignments are applied only after `decision: accept` with a reciprocal ack from both sides.

2. **Max 2 negotiation rounds.** If agents cannot agree after round 2, escalate to human decision-maker. The escalation message is a `notification` to the human agent's inbox with subject `ESCALATE -- <topic>` and the full negotiation history.

3. **One negotiation at a time per topic.** Do not start a new negotiation for the same set of issues while one is in progress.

4. **Proposals are non-binding until accepted.** An agent that sends a proposal may withdraw it by sending a new `COUNTER -- ` message before the other side accepts.

5. **Ack-back is required.** The proposing agent must acknowledge the other side's acceptance (or counter) before work begins. This prevents race conditions where one side starts work before the other has confirmed.

## Agent-Specific Notes

### Human-Driven Agents

Agents that lack headless mode (e.g., Gemini in some configurations):

- Can only be the **sender** (proposer) in a negotiation, not the receiver of headless task assignments.
- Cannot execute `execution_mode: headless` tasks.
- Their proposals should set `receiver_runtime` to the appropriate automated agent.

### Codex

- Codex operates in `--full-auto` headless mode by default.
- Codex can both propose and receive proposals.
- When Codex receives a proposal, it processes the inbox at session start per the inbox/outbox polling convention.

### Claude

- Claude operates interactively or headlessly depending on invocation.
- Claude can both propose and receive proposals.
- In headless mode (via project-specific automation), Claude processes inbox messages as part of the automation cycle.

## Example: Full Negotiation

### Round 1: Claude proposes

File: `agents/codex/inbox/20260212T0900Z_claude_notification.yaml`

```yaml
id: "msg-20260212T0900Z-claude-001"
from: "claude"
to: "codex"
type: "notification"
priority: "P1"
created_at_utc: "2026-02-12T09:00:00Z"
subject: "PROPOSAL -- Phase 4 task split"
body: |
  negotiation_id: "neg-20260212-phase4"
  round: 1
  decision: "propose"
  proposal_issues:
    claude: [32, 62, 38, 37, 29]
    codex: [30, 31, 24, 25, 26]
  proposal_split: |
    Claude: protocol docs, Python helpers, handoff specs.
    Codex: scaling infra, orchestration, dashboard work.
  respond_by_utc: "2026-02-12T10:00:00Z"
  reply_required: true
```

### Round 1: Codex accepts

File: `agents/claude/inbox/20260212T0930Z_codex_notification.yaml`

```yaml
id: "msg-20260212T0930Z-codex-001"
from: "codex"
to: "claude"
type: "notification"
priority: "P1"
created_at_utc: "2026-02-12T09:30:00Z"
subject: "ACK -- Phase 4 task split"
body: |
  negotiation_id: "neg-20260212-phase4"
  round: 1
  decision: "accept"
  proposal_issues:
    claude: [32, 62, 38, 37, 29]
    codex: [30, 31, 24, 25, 26]
  proposal_split: |
    Accepted as proposed. Starting with #30 and #31.
```

### Round 1: Claude ack-back

File: `agents/codex/inbox/20260212T0945Z_claude_notification.yaml`

```yaml
id: "msg-20260212T0945Z-claude-001"
from: "claude"
to: "codex"
type: "notification"
priority: "P2"
created_at_utc: "2026-02-12T09:45:00Z"
subject: "ACK -- Phase 4 execution confirmed"
body: |
  negotiation_id: "neg-20260212-phase4"
  round: 1
  decision: "accept"
  proposal_split: |
    Confirmed. Applying owner labels now. Starting with #32 and #62.
```

After this exchange, both agents apply `owner:claude` / `owner:codex` labels to the respective issues and begin work.

## Related Documents

- `docs/protocol/inbox_outbox.md` — base messaging protocol
- `docs/protocol/multi_agent_shared_workspace.md` — packet-based review workflow
- `templates/inbox_message.template.yaml` — message file template
