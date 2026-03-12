# Review Loop Protocol

## Purpose

Defines an inbox-based automated review loop for multi-agent code review. This protocol replaces manual review coordination with a structured message exchange that drives author-reviewer interaction through findings, fixes, and quality gate verification.

The review loop reuses the existing inbox/outbox messaging infrastructure (`docs/protocol/inbox_outbox.md`) and the findings packet format (`templates/findings_packet.template.yaml`).

## Relationship to Inbox/Outbox

The review loop adds four message types to the inbox protocol:

| Type | Direction | Purpose |
|------|-----------|---------|
| `review_request` | Author -> Reviewer | Request code review of a PR |
| `review_feedback` | Reviewer -> Author | Return review findings |
| `review_addressed` | Author -> Reviewer | Report that feedback was addressed |
| `review_lgtm` | Reviewer -> Author | Approve — quality gate passed |

All messages use standard inbox format (see `docs/protocol/inbox_outbox.md`) with type-specific body content described below.

When non-blocking findings are deferred, reviewers should record them in `review_lgtm.body.nits` (structured list). Agents may additionally mirror those items using `follow_up` messages and/or completion-notification `follow_ups:` shorthand for backward compatibility.

## Message Flow (Stateless Reviewer Rounds)

```
Author                                    Reviewer (invocation A)
  |                                              |
  |--- review_request --------------------------->|   (PR#, branch, summary, budgets)
  |                                              |
  |<-- review_feedback OR review_lgtm -----------|   (one terminal response)
  |                                              |
  |              reviewer invocation A exits      |
  |
  |--- review_addressed ------------------------->|   (commit SHA, addressed summary)
  |
  |--- review_request (round N+1) -------------->|   (explicit re-invocation)
  |
  |<-- review_feedback OR review_lgtm -----------|   (invocation B terminal response)
```

A single reviewer invocation handles one round and then exits. If a round returns `review_feedback`, the author addresses findings and explicitly re-invokes the reviewer with a new `review_request` for round N+1.

## Message Schemas

### `review_request`

Sent by the author to initiate (or re-initiate) a review invocation.

```yaml
id: "msg-20260213T1000Z-claude-001"
from: "claude"
to: "codex"
type: "review_request"
priority: "P1"
created_at_utc: "2026-02-13T10:00:00Z"
related_pr: "86"
subject: "Review: review loop protocol (#86)"
body: |
  pr: 86
  branch: claude/review-loop-protocol
  diff_summary: |
    Adds review loop protocol spec, reviewer workflow,
    reviewer runbook, 4 new message types in validate_message.py,
    tests, and CHANGELOG/inbox_outbox.md updates.
  handoff_ref: ""
  max_turns_reviewer: 8
  max_runtime_s_reviewer: 600
```

**Body fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `pr` | Yes | Pull request number |
| `branch` | Yes | Branch name under review |
| `diff_summary` | Yes | Brief summary of the changes |
| `handoff_ref` | No | Reference to a handoff packet if this review follows a handoff |
| `max_turns_reviewer` | No | Hard turn budget for reviewer invocation (default `8`) |
| `max_runtime_s_reviewer` | No | Hard runtime budget in seconds for reviewer invocation (default `600`) |

### `review_feedback`

Sent by the reviewer after examining the PR.

```yaml
id: "msg-20260213T1030Z-codex-001"
from: "codex"
to: "claude"
type: "review_feedback"
priority: "P1"
created_at_utc: "2026-02-13T10:30:00Z"
related_pr: "86"
subject: "Review feedback: round 1 (#86)"
body: |
  findings_packet: packets/findings/20260213_review_loop_codex_r1.yaml
  round: 1
  blocking_count: 2
  telemetry:
    model: gpt-5
    turns: 6
    input_tokens: 210000
    output_tokens: 9000
    wall_time_s: 142
    est_cost_usd: 0.18
```

**Body fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `findings_packet` | Yes | Path to the findings packet (relative to project workspace) |
| `round` | Yes | Current review round number (1-based) |
| `blocking_count` | Yes | Number of blocking findings in this round |
| `escalation` | No | Escalation marker (for example `max_rounds_exceeded` or `reviewer_budget_exceeded`) |
| `telemetry` | No | Optional telemetry map with `model`, `turns`, `input_tokens`, `output_tokens`, `wall_time_s`, `est_cost_usd` |

The findings packet follows the standard format in `templates/findings_packet.template.yaml`.

### `review_addressed`

Sent by the author after fixing issues raised in `review_feedback`.

```yaml
id: "msg-20260213T1100Z-claude-002"
from: "claude"
to: "codex"
type: "review_addressed"
priority: "P1"
created_at_utc: "2026-02-13T11:00:00Z"
related_pr: "86"
subject: "Feedback addressed: round 1 (#86)"
body: |
  commit_sha: abc1234
  changes_summary: |
    Fixed both blocking issues:
    - Added missing error handling for empty diff
    - Corrected max_rounds default from 5 to 2
  round: 1
  touched_files:
    - docs/protocol/review_loop.md
    - docs/protocol/inbox_outbox.md
  addressed_finding_ids:
    - F-001
    - F-002
```

**Body fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `commit_sha` | Yes | Commit SHA containing the fixes |
| `changes_summary` | Yes | Summary of what was changed to address the feedback |
| `round` | Yes | The round number whose feedback is being addressed |
| `touched_files` | No | List of files edited to address findings |
| `addressed_finding_ids` | No | List of finding IDs the author considers addressed |

### `review_lgtm`

Sent by the reviewer when the quality gate passes and the PR is merge-ready.

```yaml
id: "msg-20260213T1130Z-codex-002"
from: "codex"
to: "claude"
type: "review_lgtm"
priority: "P1"
created_at_utc: "2026-02-13T11:30:00Z"
related_pr: "86"
subject: "LGTM: review loop protocol (#86)"
body: |
  quality_gate_result: pass
  merge_ready: true
  nits:
    - nit_id: NIT-001
      tier: P3
      summary: "Clarify timeout fallback wording in docs/protocol/review_loop.md"
      owner: claude
      next_action: "Open doc cleanup PR after merge"
      tracking_ref: "#156"
      source: "F-004"
      expires_at_utc: "2026-03-15T00:00:00Z"
  telemetry:
    model: gpt-5
    turns: 4
    input_tokens: 98000
    output_tokens: 3200
    wall_time_s: 88
    est_cost_usd: 0.07
```

**Body fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `quality_gate_result` | Yes | Result of the quality gate check (`pass` or `fail`) |
| `merge_ready` | Yes | Whether the PR is ready to merge (`true` or `false`) |
| `nits` | No | Structured list of deferred non-blocking items (`P2`/`P3`) tracked after merge readiness |
| `follow_ups` | No | Legacy shorthand list of deferred items; may mirror `nits` during migration |
| `telemetry` | No | Optional telemetry map with `model`, `turns`, `input_tokens`, `output_tokens`, `wall_time_s`, `est_cost_usd` |

**`nits` item schema:**

| Field | Required | Description |
|-------|----------|-------------|
| `nit_id` | Yes | Stable identifier unique within the PR (for example `NIT-001`) |
| `tier` | Yes | Non-blocking severity (`P2` or `P3`) |
| `summary` | Yes | One-line description of the deferred item |
| `owner` | Yes | Agent responsible for driving the post-merge follow-up (default: PR author) |
| `next_action` | Yes | Concrete next step after merge |
| `tracking_ref` | No | Follow-up issue/task reference once batched |
| `source` | No | Origin pointer (finding ID, file path, or short ref) |
| `expires_at_utc` | No | UTC staleness deadline (`YYYY-MM-DDTHH:MM:SSZ`); default policy applies if omitted |

## Protocol Rules

### Stateless Reviewer Invocations

- Reviewer runtime **MUST** terminate after producing one terminal response (`review_feedback` or `review_lgtm`).
- Reviewer runtime **MUST NOT** wait in-session for `review_addressed`.
- Author runtime **SHOULD** coordinate multi-round progression by re-invoking reviewer for round N+1.

### Round Limits

- **Default maximum rounds**: 2 (aligned with `packet_states.yaml` and `multi_agent_shared_workspace.md`).
- Projects may configure up to 3 rounds via `max_review_rounds` in project `AGENTS.md`, but the default is 2.
- Configurable per review via the `review_request` body or project-level configuration.
- After reaching the maximum round count without passing the quality gate, the reviewer sends a final `review_feedback` with an escalation note in the body indicating the round limit was reached.

### Budget Controls

Reviewer invocation budgets are carried in `review_request`:

- `max_turns_reviewer` (default `8`)
- `max_runtime_s_reviewer` (default `600`)

When a budget is exhausted before review completion, reviewer sends `review_feedback` with escalation `reviewer_budget_exceeded` and exits.

### Shell-Only Polling Rule

Any wait state (inbox polling, timeouts, reminders) **MUST** use shell or script loops. Periodic idle polling **MUST NOT** consume LLM turns.

### Timeout Behavior

Timeout ownership is on the author coordinator:

- If no reviewer response arrives within policy timeout, author may send reminder `notification`.
- If stale beyond escalation threshold, author escalates via `notification` (for example to project coordinator).
- Stale reviews do not block other work; author may resume by sending `review_request` again.

### Scoped Re-Review

Reviewer should prioritize re-review scope in this order:

1. Files listed in `review_addressed.touched_files`
2. Files referenced by unresolved findings
3. Files implicated by validation failures or other regression signals

Reviewer may expand to full diff inspection when regression signals appear.

### Escalation

Escalation occurs when:

1. **Max rounds exceeded**: reviewer sends `review_feedback` with `escalation: max_rounds_exceeded`.
2. **Reviewer budget exceeded**: reviewer sends `review_feedback` with `escalation: reviewer_budget_exceeded`.
3. **Unresolvable disagreement**: either party sends a `question` message to coordinator/team lead.
4. **Staleness/timeout**: author escalates via `notification`.

### Risk-Tiered Finding Outcomes (#155)

Issue #155 research recommends deterministic triage and risk-tiered gates. This protocol maps that approach onto inbox severity levels:

- High-risk review outcomes map to blocking severities (`P0`/`P1`)
- Medium-risk outcomes map to reviewer-judgment severity (`P2`)
- Low-risk outcomes map to non-blocking severity (`P3`)

| Tier | Merge impact | Exit path |
|------|--------------|-----------|
| `P0` | Always blocking | Reviewer sends `review_feedback`; must be fixed before `review_lgtm` |
| `P1` | Blocking | Reviewer sends `review_feedback`; must be fixed before `review_lgtm` |
| `P2` | Reviewer judgment | Treat as blocking (`review_feedback`) when risk is material; otherwise defer in `review_lgtm.nits` |
| `P3` | Non-blocking | Do not hold the loop open; capture in `review_lgtm.nits`, then proceed to `review_lgtm` if gate passes |

When only `P3` findings remain, reviewer should approve with `review_lgtm` and record deferred items in `nits` (optionally mirrored via `follow_up`/`follow_ups`), instead of sending another blocking round.

### Post-LGTM Nit Lifecycle

Post-LGTM nits are tracked as structured `review_lgtm.body.nits` entries and move through this lifecycle:

1. **Capture (reviewer, pre-merge)**: Reviewer includes every deferred `P2`/`P3` item in `nits` with `nit_id`, `owner`, and `next_action`.
2. **Adopt (author, at merge)**: Author confirms nit ownership and preserves the `nits` list in merge context (PR comment, merge summary, or completion notification).
3. **Batch (author, within 24h post-merge)**: Author opens or updates tracking per batching policy and adds `tracking_ref` for each nit.
4. **Resolve or expire (author/dispatcher)**: Items are closed when fixed, or explicitly marked stale per the staleness policy below.

#### Ownership Model

| Role | Responsibility |
|------|----------------|
| Reviewer | Captures deferred non-blocking findings in `nits` before sending `review_lgtm`; assigns initial `owner` (default: author). |
| Author | Primary owner after merge; must batch tracked nits and drive closure/update status. |
| Dispatcher | Escalation owner for stale nits; reassigns owner or converts stale items into `task_request` when thresholds are exceeded. |

#### Batching Strategy

Recommended default is **one follow-up issue per PR** (for example, `Post-LGTM nits: PR #123`) with a checklist keyed by `nit_id`.

Use a **standing nit backlog** only when all of the following are true:

- nits are repetitive maintenance items in the same subsystem,
- ownership is stable, and
- each backlog entry still records `source PR` and `nit_id` for traceability.

#### Expiration / Staleness Policy

- If `expires_at_utc` is omitted, default to **30 days after merge**.
- At **14 days unresolved**, owner should send/update a `notification` with current plan.
- At **30 days unresolved** (or `expires_at_utc`, whichever is earlier), dispatcher should either:
  - convert the nit to a scoped `task_request` (`P2` by default), or
  - mark it closed as stale/accepted-risk with rationale in the tracking artifact.
- Nits must not remain indefinitely in an unowned/untracked state.

### Quality Gate

The quality gate determines whether reviewer sends `review_lgtm` or another `review_feedback`. The gate passes when:

- No unresolved P0 findings
- No unresolved blocking findings (`P1` and blocking `P2`)
- Deferred non-blocking findings (`P2`/`P3`) are captured in structured `review_lgtm.nits` tracking (optionally mirrored via `follow_up`/`follow_ups`)
- All validation commands recorded with passing outcomes

This aligns with existing merge-readiness criteria in the shared workspace protocol.

Integration points:
- `scripts/preflight.py` — automated quality checks (conflict markers, linting, etc.)
- Future: `check_quality_gate.py` — dedicated script for full quality-gate evaluation

## Comparison with background review automation

The inbox-based review loop and project-specific background review automation are complementary:

| Aspect | Review Loop (Inbox) | Background PR poller |
|--------|----------------------|--------------------------|
| Trigger | Explicit `review_request` message | GitHub PR comment/review detection |
| Communication | Inbox YAML messages | GitHub API + runtime-specific automation |
| Reviewer runtime model | Stateless one-invocation-per-round | Fresh process per poll cycle |
| State tracking | Message chain in inbox/outbox | External state file |
| Runtime | Any (Claude, Codex, Gemini, manual) | Runtime-specific |
| Best for | Structured multi-round reviews, explicit round controls | Continuous background monitoring, quick automated fixes |

Both can coexist. Use background polling for lightweight monitoring and the review loop protocol for explicit quality gate sign-off and cross-agent coordination.

## Example: Stateless Multi-Round Exchange

### Round 1

**1. Author sends `review_request`:**

```yaml
id: "msg-20260213T0900Z-claude-001"
from: "claude"
to: "gemini"
type: "review_request"
priority: "P1"
created_at_utc: "2026-02-13T09:00:00Z"
related_pr: "90"
subject: "Review: new logging module (#90)"
body: |
  pr: 90
  branch: claude/jsonl-logging
  diff_summary: |
    Adds structured JSONL logging to all scripts.
    New file: scripts/logger.py (200 lines).
    Modified: 5 existing scripts to use new logger.
  max_turns_reviewer: 8
  max_runtime_s_reviewer: 600
```

**2. Reviewer invocation A sends `review_feedback` and exits:**

```yaml
id: "msg-20260213T0945Z-gemini-001"
from: "gemini"
to: "claude"
type: "review_feedback"
priority: "P1"
created_at_utc: "2026-02-13T09:45:00Z"
related_pr: "90"
subject: "Review feedback: round 1 (#90)"
body: |
  findings_packet: packets/findings/20260213_logging_gemini_r1.yaml
  round: 1
  blocking_count: 1
```

**3. Author sends `review_addressed`:**

```yaml
id: "msg-20260213T1015Z-claude-002"
from: "claude"
to: "gemini"
type: "review_addressed"
priority: "P1"
created_at_utc: "2026-02-13T10:15:00Z"
related_pr: "90"
subject: "Feedback addressed: round 1 (#90)"
body: |
  commit_sha: def5678
  changes_summary: |
    Fixed blocking P1: added file rotation to prevent unbounded log growth.
  round: 1
  touched_files:
    - scripts/logger.py
  addressed_finding_ids:
    - F-001
```

### Round 2

**4. Author re-invokes reviewer with a new `review_request`:**

```yaml
id: "msg-20260213T1018Z-claude-003"
from: "claude"
to: "gemini"
type: "review_request"
priority: "P1"
created_at_utc: "2026-02-13T10:18:00Z"
related_pr: "90"
subject: "Re-review: round 2 (#90)"
body: |
  pr: 90
  branch: claude/jsonl-logging
  diff_summary: |
    Round 2 re-review after addressing F-001.
```

**5. Reviewer invocation B sends `review_lgtm` and exits:**

```yaml
id: "msg-20260213T1045Z-gemini-002"
from: "gemini"
to: "claude"
type: "review_lgtm"
priority: "P1"
created_at_utc: "2026-02-13T10:45:00Z"
related_pr: "90"
subject: "LGTM: new logging module (#90)"
body: |
  quality_gate_result: pass
  merge_ready: true
  nits:
    - nit_id: NIT-001
      tier: P3
      summary: "Normalize timeout wording for reviewer budget examples"
      owner: claude
      next_action: "Fold into docs cleanup issue"
      tracking_ref: "#156"
```

The author may now merge the PR.
