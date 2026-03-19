# Org-Level Memory Protocol

## Purpose

Shared, cross-project memory for multi-agent organizations. Agents across projects read org-wide decisions, conventions, and events from a single location. Complements per-project memory (`$OACP_HOME/projects/<project>/memory/`) — does not replace it.

## Directory Structure

```
$OACP_HOME/org-memory/
  recent.md           # always-loaded rolling summary (~150 lines guideline)
  decisions.md        # topical: org-wide decisions (illustrative default)
  rules.md            # topical: standing conventions (illustrative default)
  events/             # chronological: timestamped entries
    YYYYMMDD-HHMMSS-short-slug.md
```

`decisions.md` and `rules.md` are illustrative defaults, not protocol requirements. Adopters choose which topical files to create (e.g., `agents.md`, `architecture.md`).

## Event File Schema

```markdown
---
created_at_utc: 2026-03-17T17:01:20Z    # required — full timestamp for ordering + dedup
date: 2026-03-17                          # required — human-readable, matches filename prefix
agent: claude                             # required — agent that created the event
project: oacp-dev                         # required — originating project
type: decision                            # required — decision | event | rule
source_ref: debrief-20260317-s76          # optional — provenance for dual-write reconciliation
related: ["PR #43", "event/20260316-foo"]     # optional — cross-references
supersedes: event/20260310-old-decision   # optional — for decisions that override prior ones
---

Short description of what happened and why it matters.
```

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `created_at_utc` | ISO 8601 timestamp | Full timestamp for ordering and dedup |
| `date` | YYYY-MM-DD | Human-readable date, matches filename prefix |
| `agent` | string | Agent that created the event |
| `project` | string | Originating project |
| `type` | enum | `decision`, `event`, or `rule` |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `source_ref` | string | Provenance ID for dual-write reconciliation |
| `related` | list | Cross-references to PRs, issues, or other events |
| `supersedes` | string | Event path that this entry overrides |

### File Naming

Files are named `YYYYMMDD-HHMMSS-short-slug.md` where the timestamp provides sub-day ordering and the slug is a brief descriptor (lowercase, hyphen-separated).

Examples:
- `20260317-170120-api-convention.md`
- `20260318-091500-deploy-freeze.md`

## Permission Model

| Role | recent.md | Topical files | events/ |
|------|:---------:|:-------------:|:-------:|
| Agent read | yes | yes | yes |
| Agent write | no | no | yes |
| Coordinator write | yes | yes | yes |

- **Agents** write events only (append-only, no coordination needed)
- **Agents** may propose topical promotions or corrections via events (type: `rule` or `decision`) — the coordinator decides whether to incorporate
- **Agents** may proactively read `events/` for urgent context (e.g., "API X is down") without waiting for coordinator curation
- **Coordinator** curates topical files and `recent.md` from events
- Topical files are the structured knowledge layer; events are the raw signal

## Lifecycle

- Events are archived after an adopter-defined retention period (reference default: 30 days)
- Patterns that repeat 3+ times in events should be promoted to topical files
- `recent.md` reflects current state, not full history — it is a rolling summary

## Integration Pattern (Cortex Reference Implementation)

Cortex demonstrates the dual-pipeline pattern — same source data, two audiences. This is a reference implementation, not a protocol requirement.

**Debrief step (write):**
- Debrief → cortex inbox (existing, for human)
- Debrief → `org-memory/events/` (new, for agents)
- Both writes treated as a logical unit — retry/warn on partial failure
- `source_ref` in event frontmatter matches the debrief ID for reconciliation

**Sync step (curate):**
- Debriefs → SSOT + vault daily notes (existing, for human)
- Events → topical files + `recent.md` (new, for agents)
- Sync cross-references SSOT when curating topical files to prevent drift
- Sync is idempotent — handles duplicates/replays via `source_ref` + `created_at_utc`

**Consistency model:** Eventual, not strong. The two pipelines may temporarily diverge. `source_ref` enables reconciliation. Adopter defines failure semantics: retryable partial failure, blocked debrief, or acceptable degraded mode.

## v0.2 Scope

1. Format spec (directory structure, frontmatter schema, naming convention)
2. CLI: `oacp org-memory init` (scaffold directory) and `oacp write-event` (create event files)
3. Agents write events during debrief
4. Agents read topical files + `recent.md` for org context
5. Coordinator maintains topical files during sync

## v0.3+

- Agent write access to topical files (with schema validation)
- `recent.md` auto-generation from topical files + recent events
- Search/discovery tooling (BM25 or similar)
- Structured `id` field on events for cross-referencing

## Design Rationale

| Alternative | Why not |
|---|---|
| Single monolithic file | Wastes tokens, no progressive disclosure |
| Events only (Codex pattern) | Optimizes for writing, weak for reading — "What's our API convention?" shouldn't require scanning 50 event files |
| Topical only (Iris pattern) | No low-friction write path for agents — event files require only frontmatter, not schema knowledge |
| Inheritance model | Flat merge is simpler, no parent/child override complexity |

The hybrid (topical + events) gives agents a fast read path (topical files) and a fast write path (events/), with coordinator curation bridging the two.
