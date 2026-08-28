# Org-Level Memory Protocol

## Purpose

Shared, cross-project memory for multi-agent organizations. Agents across projects read org-wide decisions, conventions, and events from a single location. Complements per-project memory (`$OACP_HOME/projects/<project>/memory/`) — does not replace it.

## Directory Structure

```
$OACP_HOME/org-memory/
  recent.md           # always-loaded rolling summary (~60K chars / ~15K tokens)
  decisions.md        # topical: org-wide decisions (illustrative default)
  rules.md            # topical: standing conventions (illustrative default)
  events/             # chronological: timestamped entries
    YYYYMMDD-HHMMSS-short-slug.md
  debriefs/           # central session-debrief store (append-only, full records)
    <project>/
      <YYYY>/<MM>/
        YYYYMMDD-<agent>-<session>.md
```

`decisions.md` and `rules.md` are illustrative defaults, not protocol requirements. Adopters choose which topical files to create (e.g., `agents.md`, `architecture.md`).

Keep `recent.md` to approximately 60,000 characters (roughly 15,000 tokens),
because its context cost is paid at every session start. Approximately 150
lines remain a secondary readability hint only; when the measures disagree,
the character budget governs. Collapse older detail into topical files or
history so the file remains a rolling summary rather than a complete log.

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

## Debrief Store

`org-memory/debriefs/` is the central, append-only store for full session
debriefs. Every agent's end-of-session debrief lands here — one immutable file
per session — instead of in per-project trees. Debriefs are the full-fidelity
session record; events remain the filtered-outcome channel. The two are
distinct artifact classes and neither substitutes for the other.

`oacp org-memory init` creates `debriefs/` (with a `.gitkeep` placeholder so
the empty directory survives git-based sync). `oacp doctor` checks the store
setup — directory presence, path layout, lingering staging artifacts,
irregular entries — and never opens debrief files: content and format
verification belong to the writer contract (read-back at publication) and to
git history, not to the doctor. The kernel owns only this layout and schema;
the writer that produces debrief files is adopter tooling (for example, a
debrief skill script) — there is no kernel subcommand for writing debriefs.

### Path and File Naming

```
org-memory/debriefs/<project>/<YYYY>/<MM>/<YYYYMMDD>-<agent>-<session>.md
```

- `<project>` — the originating project workspace name, exactly as it appears
  under `$OACP_HOME/projects/`, byte-for-byte and case-sensitive. The segment
  follows the workspace project-name rule (any name that does not start with
  `.` and contains no `/` or `\`), so every valid workspace name has a valid
  debrief path.
- `<YYYY>/<MM>` — the year and zero-padded month of the session start, in UTC.
  Both MUST agree with the filename's `<YYYYMMDD>` prefix.
- Filename grammar (anchored regex):

  ```
  ^(?P<date>\d{8})-(?P<agent>[A-Za-z0-9][A-Za-z0-9._-]{0,63})-(?P<session>[a-z0-9]{1,32})\.md$
  ```

  - `<YYYYMMDD>` — the session start date in UTC. MUST equal the date of
    `started_utc` in the frontmatter and the `<YYYY>/<MM>` parent directories.
  - `<agent>` — the writing agent's name, exactly as registered (the agent
    grammar is the protocol's canonical agent-name rule, so hyphens, dots,
    underscores, and mixed case are all representable). Case-sensitive and
    byte-for-byte identical to the frontmatter `agent` field.
  - `<session>` — a short stable session identifier: lowercase letters and
    digits only, 1-32 characters, **never hyphens**. Recommended: the first 8
    characters of the harness session UUID. Distinguishes multiple sessions
    by the same agent on the same day.
  - **Parse rule**: the session identifier is the substring after the *final*
    hyphen; hyphens may therefore appear inside the agent segment but never
    inside the session identifier, which keeps the three-part filename
    uniquely parseable for any valid agent name.

Examples: `debriefs/demo-project/2026/08/20260825-alice-1f3a9c2b.md`,
`debriefs/Demo_Project/2026/08/20260825-bob-ops-9f00aa11.md` (agent
`bob-ops`).

### Debrief File Schema

```markdown
---
schema_version: 1                        # required — integer, this schema
project: demo-project                    # required — matches the <project> path segment
agent: alice                             # required — matches the filename <agent> segment
runtime: claude                          # required — runtime family that ran the session
session: 1f3a9c2b                        # required — matches the filename <session> segment
started_utc: 2026-08-25T20:04:11Z        # required — session start, ISO 8601 UTC (Z)
ended_utc: 2026-08-25T22:01:47Z          # required — session end, ISO 8601 UTC (Z); >= started_utc
content_sha256: <64 lowercase hex chars> # required — SHA-256 of the body (definition below)
immutable: true                          # required — literal true; the append-only assertion
---

<body: the full session debrief, free-form Markdown>
```

**Body and content hash.** The body is every byte after the line that closes
the frontmatter block (the second `---` line, including its trailing newline),
exactly as stored — no trailing-whitespace or newline normalization.
`content_sha256` is the lowercase-hex SHA-256 of those UTF-8 bytes. Writers
compute the hash over the final body bytes at write time; validators recompute
it the same way.

### Append-Only Rule

Debrief files are written once, at session end, and never rewritten:

- One session = one file. A later session the same day writes a new file with
  a different `<session>` identifier — never an append to an existing file.
- Corrections and follow-ups are new artifacts (a new debrief, or an event
  referencing the original) — never edits to a landed debrief.
- A file whose body no longer matches its `content_sha256`, or that was
  rewritten after publication, is a protocol violation. Detection is an
  adopter-tooling concern (boundaries below); `oacp doctor` checks setup
  only and does not open debrief files.

**Rewrite detection (normative).** On a git-backed store, git state and
history are the authoritative rewrite evidence: a tracked debrief modified or
deleted in the working tree, or touched by more than one commit, is flagged;
when the evidence queries themselves fail, the validator reports the evidence
as unavailable rather than clean. On a store that is not a git repository,
the fallback signal is the file's modification time measured against its own
`ended_utc`: publication legitimately happens shortly after session end, so a
modification time up to exactly 3,600 seconds after `ended_utc` is accepted
as the publication window, and a file whose modification time exceeds
`ended_utc` by MORE than 3,600 seconds (strictly greater) is flagged.
Validators implement this exact boundary.

**Writer commit contract.** Because writers live outside the kernel, this
section is the shared authority every independent writer implements against.
Publication is failure-atomic: the canonical path only ever holds a complete,
verified record — never partial bytes.

- **Stage privately first.** Write the full record to a private staging file
  in the same `<project>/<YYYY>/<MM>/` directory, named
  `.stage.<final-name>.<nonce>` (the leading dot keeps it outside the
  canonical namespace; `<nonce>` is a writer-unique token). Flush it and
  verify the staged bytes (size and `content_sha256`) before publication.
- **Publish with an atomic no-replace primitive** — `link()` to the canonical
  name followed by unlinking the staging name, `renameat2(...,
  RENAME_NOREPLACE)`, or an equivalent that fails if the target exists. A
  rename that can replace an existing target is forbidden, and so is writing
  through the canonical path directly: `O_CREAT|O_EXCL` on the canonical
  path is NOT sufficient — it reserves the name atomically but publishes
  content non-atomically, so a failure between open and write would expose a
  partial record.
- **Collision rule, evaluated at publish.** If the canonical path already
  exists: byte-identical to the staged record = idempotent success (remove
  the staging file and report success); any difference = hard failure — the
  writer reports it and recovers by publishing under a *new* `<session>`
  identifier, never by replacing the existing record.
- **Failure leaves the canonical namespace clean.** Validation errors, short
  writes, crashes, and read-back mismatches before publication MUST leave
  the canonical path absent; at worst a private staging file remains.
  Writers deterministically remove or adopt *their own* stale staging
  artifacts on retry; `oacp doctor` reports lingering staging files.
- **Verify after publish.** Read the canonical file back and confirm
  `content_sha256`; a mismatch is a reported failure, never a silent
  success.
- The target MUST be a regular file. Writers MUST NOT follow symlinks, and
  MUST fail when the existing path is a symlink or any non-regular file.
- Parent directories (`<project>/<YYYY>/<MM>/`) may be created with ordinary
  make-parents semantics; only the file itself carries the publication rule.
- Writer conformance tests accompany the writer implementation and MUST pin:
  exception/short-write before publish (canonical path stays absent),
  interrupted publication recovery, read-back mismatch, retry after success
  (idempotent), differing-content collision, symlink-at-target, and
  concurrent identical and differing writers.

### Curation Guard

Raw debriefs never enter `recent.md` or `events/` — only curated folds do.
The coordinator reads debriefs and promotes durable outcomes into events and
topical files; the raw session narrative stays in `debriefs/`. This keeps the
always-loaded surfaces bounded and the event channel filtered. Debrief files
are likewise never auto-loaded at session start.

### Sync

The cross-machine memory-sync allowlist covers `org-memory/debriefs/**` (it is
inside `org-memory/**`, which syncs in full). No per-adopter sync
configuration is needed.

## Permission Model

| Role | recent.md | Topical files | events/ | debriefs/ |
|------|:---------:|:-------------:|:-------:|:---------:|
| Agent read | yes | yes | yes | yes |
| Agent write | no | no | yes | yes (own sessions, append-only) |
| Coordinator write | yes | yes | yes | yes (append-only) |

- **Agents** write events and their own session debriefs (both append-only, no coordination needed)
- **Agents** may propose topical promotions or corrections via events (type: `rule` or `decision`) — the coordinator decides whether to incorporate
- **Agents** may proactively read `events/` for urgent context (e.g., "API X is down") without waiting for coordinator curation
- **Coordinator** curates topical files and `recent.md` from events
- Topical files are the structured knowledge layer; events are the raw signal

## Lifecycle

- Events are archived after an adopter-defined retention period (reference default: 30 days)
- Patterns that repeat 3+ times in events should be promoted to topical files
- `recent.md` reflects current state, not full history — it is a rolling summary
- Debriefs are permanent history: individual files are never rewritten (see
  Append-Only Rule); retention beyond that is adopter-defined

## Integration Pattern (Cortex Reference Implementation)

Cortex demonstrates the dual-pipeline pattern — same source data, two audiences. This is a reference implementation, not a protocol requirement.

**Debrief step (write):**
- Debrief → cortex inbox (existing, for human)
- Debrief → `org-memory/debriefs/` (the canonical immutable session record — see Debrief Store)
- Curated outcome events → `org-memory/events/` (filtered derivatives, for agents — never the raw debrief; see Curation Guard)
- All three writes treated as a logical unit — retry/warn on partial failure
- `source_ref` in event frontmatter matches the debrief-store filename stem (`<YYYYMMDD>-<agent>-<session>`) for reconciliation

**Sync step (curate):**
- Debriefs (read from `org-memory/debriefs/`) → SSOT + vault daily notes (existing, for human)
- Events → topical files + `recent.md` (new, for agents)
- Sync cross-references SSOT when curating topical files to prevent drift
- Sync is idempotent — handles duplicates/replays via `source_ref` + `created_at_utc`

**Consistency model:** Eventual, not strong. The pipelines may temporarily diverge. `source_ref` enables reconciliation against the debrief-store record. Adopter failure semantics (retryable partial failure, blocked debrief, or acceptable degraded mode) apply to the inbox and event writes; the debrief-store write is required by the Debrief Store section, and a store write that ultimately fails is a reported failure to retry, never an accepted degraded mode.

## v0.2 Scope

1. Format spec (directory structure, frontmatter schema, naming convention)
2. CLI: `oacp org-memory init` (scaffold directory, including `debriefs/`) and `oacp write-event` (create event files)
3. Agents write their full session debrief to `org-memory/debriefs/` (via adopter tooling — see Debrief Store) and curated outcome events during debrief
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
