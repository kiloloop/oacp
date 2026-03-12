# Inbox / Outbox Messaging Protocol

## Purpose

Point-to-point async messaging between agents. Complements the packet-based review workflow for lighter-weight coordination: task requests, questions, status pings, and handoff signals.

## Directory Layout

```
agents/<agent_name>/inbox/    # other agents write here
agents/<agent_name>/outbox/   # this agent's sent messages (copies)
```

## Message Format

Messages are YAML files. Filename: `<timestamp>_<from>_<type>.yaml`

Example: `20260211T1430Z_codex_task_request.yaml`

```yaml
id: "msg-20260211T1430Z-codex-001"
from: "codex"
to: "claude"
type: "task_request"       # task_request | question | notification | follow_up | handoff | handoff_complete | review_request | review_feedback | review_addressed | review_lgtm
priority: "P2"             # P0 | P1 | P2 | P3
created_at_utc: "2026-02-11T14:30:00Z"
related_packet: ""         # optional packet_id
related_pr: ""             # optional PR number
conversation_id: ""        # optional — conversation thread ID (see Conversation Threading)
parent_message_id: ""      # optional — ID of the message this replies to
context_keys: |            # optional — key items from prior conversation for continuity
  Summary of relevant prior context.
subject: "Review EdgeX spread calculation change"
body: |
  Free-form message content.
  Can be multi-line markdown.
```

## Message Types

| Type | Purpose | Expected Response |
|------|---------|-------------------|
| `task_request` | Ask agent to do work | Reply or packet creation |
| `question` | Ask for information or decision | Reply message |
| `notification` | FYI — no response needed by default | None, unless it reports findings/issues (then ack/reply expected) |
| `follow_up` | Capture non-blocking work that should be tracked after completion | None by default (recipient may convert to `task_request`) |
| `handoff` | Transfer ownership of in-flight work | Acknowledgment message |
| `handoff_complete` | Report that a handoff target is complete | None (or follow-up task request) |
| `review_request` | Request one reviewer invocation for a PR round | `review_feedback` or `review_lgtm` (reviewer exits after sending) |
| `review_feedback` | Return review findings for one round | `review_addressed`, then a new `review_request` for re-review |
| `review_addressed` | Report that feedback was addressed | New `review_request` to re-invoke reviewer, then `review_feedback` or `review_lgtm` |
| `review_lgtm` | Approve — quality gate passed | None (author may merge) |
| `brainstorm_request` | Open-ended research/analysis request — no PR, no fixed deliverable format | Feasibility report or analysis in outbox |
| `brainstorm_followup` | Amend scope of an in-progress brainstorm (references parent brainstorm_request) | Incorporated into next round |

See `docs/protocol/review_loop.md` for the full review loop protocol specification.

## Type-Specific Body Schemas

### `type: handoff`

`body` must be a structured YAML packet with required fields:

- `source_agent`
- `target_agent`
- `intent`
- `artifacts_to_review` (non-empty list)
- `definition_of_done` (non-empty list)
- `context_bundle.files_touched` (non-empty list)
- `context_bundle.decisions_made` (non-empty list)
- `context_bundle.blockers_hit` (non-empty list)
- `context_bundle.suggested_next_steps` (non-empty list)

Reference template: `templates/handoff_packet.template.yaml`

### `type: handoff_complete`

`body` must include these required fields:

- `issue`
- `pr`
- `branch`
- `tests_run`
- `next_owner`

Example:

```yaml
issue: "#77"
pr: "84"
branch: "codex/issue-77-handoff-protocol"
tests_run: "make test"
next_owner: "claude"
```

### `type: follow_up`

Use `follow_up` for non-blocking items that should be tracked after the current task/review closes. Do not use this type for blocking defects.

`body` must include these required fields:

- `source_type` (`review` | `task` | `deploy` | `incident` | `other`)
- `source_ref` (PR/issue/message ID or file path)
- `risk_tier` (`P2` or `P3`)
- `summary`
- `next_action`
- `owner`

Optional fields:

- `tracking_issue`
- `due_hint`

Example:

```yaml
source_type: "review"
source_ref: "PR #152"
risk_tier: "P3"
summary: "Clarify timeout fallback behavior in docs"
next_action: "Open doc cleanup PR after merge"
owner: "codex"
tracking_issue: "#156"
due_hint: "next sprint"
```

### `type: notification` (Done shorthand)

Completion notifications (for example subjects starting with `Done:` or `Done -`) may include a `follow_ups:` shorthand block in `body` to enumerate deferred non-blocking items.

Conventions:

- Use `follow_ups: none` when no deferred items remain.
- Otherwise, use a YAML list and include `tier`, `summary`, `owner`, and `tracking` when available.

Example:

```yaml
subject: "Done: #156 follow-up protocol docs"
body: |
  Completed protocol updates and validation.

  follow_ups:
    - tier: P3
      summary: "Add follow_up type enforcement to validate_message.py"
      owner: codex
      tracking: "#157"
```

### Review-Loop Telemetry Guidance

For review-loop message types (`review_feedback`, `review_lgtm`, and optionally `review_addressed`), agents may attach telemetry as optional top-level fields:

- `model`
- `turns`
- `input_tokens`
- `output_tokens`
- `wall_time_s`
- `est_cost_usd`

These fields are optional and backward-compatible. Review-loop bodies may also include a nested `telemetry:` map for richer per-round detail.

## Lifecycle

1. Sender creates message YAML.
2. Sender writes to `agents/<recipient>/inbox/<filename>.yaml`.
3. Sender copies to their own `agents/<sender>/outbox/<filename>.yaml`.
4. Recipient reads from inbox when polling or at session start.
5. Recipient deletes from inbox after processing.
6. Replies are new messages in the original sender's inbox.

## Polling Convention

Agents should check their inbox:
- At session start (before beginning work)
- After completing each major task
- There is no real-time notification — this is a poll-based protocol
- Wait states should use shell/script polling loops (`sleep` + re-check), not repeated LLM turns.

## Processing Order

When multiple messages are pending in an agent's inbox, process by priority: P0 first, then P1, then P2/P3. Within the same priority, process in arrival order (oldest first by filename timestamp). `task_request` and `review_request` types take precedence over `notification` and `follow_up` at the same priority level.

## Inbox Cleanup

- Delete messages from inbox immediately after processing (see Lifecycle step 5).
- Outbox files: retain for 30 days, then may be pruned by the sending agent.
- Dispatcher cleanup: when archiving a completed dispatch, also delete any intermediate WIP/ack notifications for that dispatch from the dispatcher's own inbox.

## Conversation Threading

Agents can maintain continuity across handoffs and multi-step exchanges using optional conversation threading fields.

### Fields

| Field | Format | Purpose |
|-------|--------|---------|
| `conversation_id` | `conv-<YYYYMMDD>-<agent>-<seq>` | Groups related messages into a logical conversation thread |
| `parent_message_id` | Same format as `id` | Links a reply to the specific message it responds to |
| `context_keys` | YAML block scalar (multi-line string) | Summarizes key items from the prior conversation the recipient needs for continuity |

### How It Works

1. **Starting a conversation**: The initiating agent generates a `conversation_id` (e.g., `conv-20260211-codex-001`) and includes it in the first message.
2. **Continuing a conversation**: Subsequent messages in the same thread reuse the same `conversation_id` and set `parent_message_id` to the `id` of the message being replied to.
3. **Handoff messages**: When handing off work (`type: handoff`), the sender SHOULD include `conversation_id` and `context_keys` so the receiving agent can pick up without re-reading the full history.
4. **Context keys**: A concise summary of decisions made, artifacts produced, and open questions from the prior conversation. This avoids the anti-pattern of forwarding raw conversation transcripts.

### Example: Handoff with Context

```yaml
id: "msg-20260211T1630Z-gemini-003"
from: "gemini"
to: "claude"
type: "handoff"
priority: "P1"
created_at_utc: "2026-02-11T16:30:00Z"
conversation_id: "conv-20260211-gemini-001"
parent_message_id: "msg-20260211T1500Z-codex-002"
context_keys: |
  - QA adapter skeleton merged (PR #42)
  - Remaining: integration tests for Codex runtime
  - Decision: use pytest fixtures, not mocks, for runtime adapter
  - Open question: timeout threshold for long-running validations
subject: "Handoff: QA adapter integration tests"
body: |
  Handing off integration test work for the QA adapter.
  See context_keys for prior decisions and open items.
```

### Guidelines

- All threading fields are **optional**. Messages without them are standalone.
- `conversation_id` format: `conv-<YYYYMMDD>-<originating_agent>-<sequence>`. The originating agent is the one who started the conversation.
- `context_keys` should be **concise** (under 500 words). Reference packet IDs or file paths for longer context.
- Do not include raw conversation transcripts or ephemeral debugging output in `context_keys`.

## Broadcast Delivery

The `to` field accepts a **string** (single recipient) or a **list of strings** (broadcast to multiple recipients).

### Broadcast Constraints

- Maximum 10 recipients per broadcast.
- Sender must not appear in the recipient list.
- `type: handoff` and `type: handoff_complete` do not support broadcast (they are point-to-point only).

### Delivery Model

- **Inbox**: One copy of the message is written to each recipient's inbox.
- **Outbox**: A single copy is written to the sender's outbox, with the full recipient list in the `to` field.

### Example

```yaml
id: "msg-20260213T1200Z-claude-ab12"
from: "claude"
to: [codex, gemini]
type: "notification"
priority: "P2"
created_at_utc: "2026-02-13T12:00:00Z"
subject: "Daily standup summary"
body: |
  Completed review loop protocol. Starting inbox v2 next.
```

## Proposal Expiry

Messages may include an optional `expires_at` field (ISO 8601 UTC) indicating when the message is no longer actionable.

### Processing Rule

- Recipients **should** skip expired messages when polling their inbox.
- Optionally, a recipient may reply with subject `"EXPIRED: <original_subject>"` to acknowledge the expiry.
- Expired messages are **not** auto-archived or auto-deleted — expiry is advisory and log-only.

### Example

```yaml
expires_at: "2026-02-14T12:00:00Z"
```

## Channel Tagging

Messages may include an optional `channel` field (free-text string, max 64 chars, alphanumeric + hyphens + underscores) to categorize conversations.

### Suggested Channels

| Channel | Use Case |
|---------|----------|
| `brainstorm` | Multi-agent ideation sessions |
| `review` | Code review and findings discussion |
| `deploy` | Deployment coordination and readiness |
| `incident` | Urgent issue response |

Channels are **not enforced** — agents may use any string. These are suggestions for consistency.

### Example

```yaml
channel: review
```

## Updated Message Schema

The complete message schema with all fields (required and optional):

```yaml
# Required fields
id: "msg-<timestamp>-<sender>-<rand>"
from: "<sender_agent>"
to: "<recipient>"                    # string or list of strings (broadcast)
type: "<message_type>"               # task_request | question | notification | follow_up | handoff | handoff_complete | review_request | review_feedback | review_addressed | review_lgtm | brainstorm_request | brainstorm_followup
priority: "<P0-P3>"
created_at_utc: "<ISO 8601 UTC>"
subject: "<subject_line>"
body: |
  <message content>

# Optional fields
expires_at: "<ISO 8601 UTC>"         # when message expires (advisory)
channel: "<free_text>"               # conversation category tag
related_packet: "<packet_id>"
related_pr: "<pr_number>"
conversation_id: "<conv-YYYYMMDD-agent-seq>"
parent_message_id: "<msg_id>"
model: "<runtime_model_id>"          # optional telemetry (commonly for review-loop messages)
turns: "<int>"                       # optional telemetry
input_tokens: "<int>"                # optional telemetry
output_tokens: "<int>"               # optional telemetry
wall_time_s: "<int>"                 # optional telemetry
est_cost_usd: "<float>"              # optional telemetry
context_keys: |
  <summary of prior context>
```

## Rules

- **One message per file.** Do not append to existing files.
- **Inbox is write-once for senders.** Only the inbox owner deletes from it.
- **Do not replace findings packets with inbox text.** Use `packets/findings/` for full QA feedback; use `follow_up` only to track deferred non-blocking items after findings are recorded.
- **Keep messages small.** If the content is long, reference a packet or file path instead.
- **Priority P0 = urgent.** If you drop a P0 message, also use an out-of-band channel (Telegram, etc.) to alert the recipient.
