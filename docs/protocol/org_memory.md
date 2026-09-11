# Memory Layout Protocol

## Purpose

Durable memory shared by every agent and runtime in an OACP home, laid out in
three tiers:

| Tier | Where | Holds |
|---|---|---|
| Per-project | `projects/<project>/memory/` | The four-file active working set one project's agents read at session start, plus `archive/` |
| Org | `org-memory/` | Cross-project decisions, conventions, events, and the debrief store |
| Cross-machine | The home as a git repository | The two storage tiers above, carried between machines by an allowlisted sync |

The kernel owns this layout: the paths, the files, who writes what, the marker
and ignore files, and the sync allowlist. `oacp init` scaffolds the per-project
tier against it. Memory *tooling* — the org-tier scaffold, capture, recall,
archive, the sync verbs, a memory doctor — is userland and lives in
[`agent-memory`](https://github.com/kiloloop/agent-memory) (`agent-memory-cli`
on PyPI), which implements this layout and links back here; this document
names no command surface. Runtime loading mechanics stay where they are: the
session-start read set is [Session Init](session_init.md#step-3-load-durable-memory)
Step 3, and org-memory retrieval policy is the
[memory context guide](../guides/memory-context.md).

Lean-kernel admission: what every receiver must understand to share memory
with another is the layout — an entry written to
`projects/<project>/memory/decision_log.md` on one machine must be found at
that path by every other agent and machine. Everything else about memory (what
a good entry looks like, when to promote, how to search) is convention and
lives in guides and skills.

## Layout

The complete layout, relative to the OACP home (`$OACP_HOME`). `*` stands for
one project name; a trailing `/` marks a directory. This block is the finite
grammar of the layout: an implementation creates nothing else and a document
names nothing else.

```oacp-memory-layout
.gitignore
.oacp-memory-repo
org-memory/
org-memory/recent.md
org-memory/decisions.md
org-memory/rules.md
org-memory/events/
org-memory/debriefs/
projects/
projects/*/memory/
projects/*/memory/project_facts.md
projects/*/memory/decision_log.md
projects/*/memory/open_threads.md
projects/*/memory/known_debt.md
projects/*/memory/archive/
projects/*/memory/.cache/
```

`<project>` follows the workspace project-name rule: any name that does not
start with `.` and contains no `/` or `\`. Everything else in the home is not
memory — `keys/` (the signing keystore, denied below), a project's `agents/`
and `packets/` trees, `state/`, and any other sibling stay on the machine that
wrote them. `.gitkeep` placeholders that scaffolding drops into empty
directories so they survive git are scaffolding, not layout.

Scaffolding creates missing entries only. A file slot that is already
occupied — by a regular file, a directory, or a link, dangling included — is
never rewritten and never followed. An existing directory is entered as found,
a directory symlink included. A home that has drifted from this layout is
reported by tooling, not silently repaired.

## Per-Project Tier

**Location:** `projects/<project>/memory/`, created by `oacp init <project>`
with the rest of the project workspace.

### The four files

The active working set. Every runtime reads these four files, in this order, at
session start ([Session Init](session_init.md#step-3-load-durable-memory)
Step 3), and only verified, stable outcomes are written to them.

| File | Holds | Written by |
|---|---|---|
| `project_facts.md` | Agent roles, repo structure, architecture, conventions | Any agent, via the project's durable-memory promotion flow |
| `decision_log.md` | Dated decisions with rationale. Append-only: a decision is superseded by a newer entry, never edited | Any agent, via the promotion flow |
| `open_threads.md` | Unresolved issues, blocked work, cross-agent coordination | Any agent, via the promotion flow |
| `known_debt.md` | Verified unresolved debt that should persist across sessions | Any agent, via the promotion flow |

Scaffolding writes each file once, from a template, and never overwrites one
that exists.

**Promotion flow.** Memory is written at stable points, not during work. Merge
decisions and equivalent terminal artifacts carry a "Durable Memory Updates"
section, and the project's promotion mechanism appends approved entries to the
matching file, deduplicating against existing content. Raw logs, long command
output, transcripts, and in-progress state never enter these files (see
[Durable Memory Promotion](multi_agent_shared_workspace.md#durable-memory-promotion)).

### `archive/`

`projects/<project>/memory/archive/` holds files retired from the active set —
supplementary notes a project accumulated, or an older working file replaced by
a newer one — for historical retention. Archived files are retrieved when a
task needs them; restoring one to `memory/` does not add it to the required
session-start reads, which stay the four files above.

### `.cache/`

`projects/<project>/memory/.cache/` is reserved for local, regenerable state
(indexes, scratch) that tooling keeps beside the tier. It never syncs (see the
allowlist below) and is never a session-start read.

## Org Tier

**Location:** `org-memory/`, beside `projects/`, created by `agent-memory org
init`. Shared, cross-project memory: agents across projects read org-wide
decisions, conventions, and events from a single location. It complements
per-project memory and never replaces it.

```
org-memory/
  recent.md           # rolling summary (~60K chars / ~15K tokens)
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

Org memory is retrieved on demand by default, separately from the four
project memory files read during init. Runtime or project guidance may opt
into a bounded startup summary; the directory layout does not require eager
loading. See the adopter guide for [memory context and retrieval](../guides/memory-context.md).

Keep `recent.md` to approximately 60,000 characters (roughly 15,000 tokens)
unless adopter guidance sets a smaller budget. Approximately 150 lines remain
a secondary readability hint only; when the measures disagree, the character
budget governs. Collapse older detail into topical files or history so the
file remains a bounded rolling summary when retrieved. This is a curation
budget, not a requirement or allowance to load that much context at startup.

### Event File Schema

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

#### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `created_at_utc` | ISO 8601 timestamp | Full timestamp for ordering and dedup |
| `date` | YYYY-MM-DD | Human-readable date, matches filename prefix |
| `agent` | string | Agent that created the event |
| `project` | string | Originating project |
| `type` | enum | `decision`, `event`, or `rule` |

#### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `source_ref` | string | Provenance ID for dual-write reconciliation; when the event was folded from a session debrief, the debrief-store filename stem (`<YYYYMMDD>-<agent>-<session>`) |
| `related` | list | Cross-references to PRs, issues, or other events |
| `supersedes` | string | Event path that this entry overrides |

#### File Naming

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

The org-tier scaffold creates `debriefs/` (with a `.gitkeep` placeholder so
the empty directory survives git-based sync). The memory doctor checks the
store setup — directory presence, path layout, lingering staging artifacts,
irregular entries — and never opens debrief files: content and format
verification belong to the writer contract (read-back at publication) and to
git history, not to the doctor. The kernel owns only this layout and schema;
the writer that produces debrief files is adopter tooling (for example,
agent-memory's debrief verb) — there is no kernel subcommand for writing
debriefs.

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
curated surfaces bounded and the event channel filtered. Debrief files
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

## Cross-Machine Tier

Optional. A home that syncs is a plain git repository at the home root with
one remote; the layout does not change and no server is involved. The two
storage tiers are what syncs, selected by an allowlist; every other path in
the home stays on the machine that wrote it. What an implementation does with
the allowlist — commit, push, fast-forward pull, refuse to merge — is the
tool's contract, not the kernel's: see agent-memory's
[sync commands](https://github.com/kiloloop/agent-memory/blob/main/docs/commands.md#sync).

### The marker

`.oacp-memory-repo` at the home root marks a home whose memory syncs. Presence
is the whole signal: tooling and startup hooks that pull memory check for the
file and do nothing when it is absent, so removing it disables sync locally
without touching the repository or its history. Its content is informational.
The name is a compatibility contract with every existing home; keep it
verbatim. Syncing is opt-in, so scaffolding a home never creates the marker.

### The sync allowlist

`.gitignore` at the home root carries the canonical allowlist, byte for byte:

```gitignore
*
!*/
!/.gitignore
!/.oacp-memory-repo
!org-memory/**
!projects/*/memory/**
projects/*/memory/.cache/
# never sync private key material — explicit deny, wins over any future allowlist widening
keys/
```

Line by line:

- **Deny everything, then re-allow directories** (`*`, `!*/`) so the tier
  patterns below can reach into the tree.
- **The synced set** is the ignore file and the marker at the home root
  (anchored: the two explicit name exceptions apply there only, and a deeper
  file of either name follows the ordinary tier rules below), `org-memory/**`
  in full (the debrief store included), and `projects/*/memory/**` for every
  project.
- **`projects/*/memory/.cache/` never syncs** — the one excluded subtree
  inside a tier.
- **`keys/` never syncs.** The signing keystore
  ([key management](message_signing.md#key-management)) is denied last, after
  every allow rule, so the deny wins even if the allowlist is widened above
  it.

### Which paths sync

The allowlist is also a predicate over home-relative paths, and the predicate,
not the ignore file, is what a sync engine trusts:

- `.gitignore` and `.oacp-memory-repo` at the home root are allowed by name.
  A deeper file of either name is admitted only when the tier rules below
  admit it (`org-memory/.gitignore` is; `other/.gitignore` is not).
- A path with any component named `keys` is denied, at any depth, whatever
  the ignore file says.
- A path strictly inside a tier directory (`org-memory/…`,
  `projects/<project>/memory/…`) is allowed unless its first component below
  the tier is one of that tier's unsynced names. The project tier excludes
  `.cache`; the org tier excludes nothing, so `org-memory/.cache/…` syncs.
  The tier directory itself is not a path inside it.
- Every other path is denied.

The tier directories an engine may stage are enumerated in allowlist order:
`org-memory/` first, then each existing `projects/<project>/memory/` in
project-name order.

## Conformance

The layout ships as data. `tests/conformance/memory_layout/layout.yaml`
carries the same entry set as the Layout block above, and
`canonical_memory_gitignore.txt` beside it is the allowlist byte for byte.
`tests/test_memory_layout_fixture.py` holds this document, the fixture, and
the kernel's project-tier scaffold to one another in both directions, and
derives the allowlist's rule lines from the fixture's tiers and never-synced
names so the two fixture files cannot disagree. An implementation vendors the
two fixture files and asserts its own layout table, ignore text, and org-tier
scaffold against them the same way.

## Design Rationale

| Alternative | Why not |
|---|---|
| Single monolithic file | Wastes tokens, no progressive disclosure |
| Events only | Optimizes for writing, weak for reading — "What's our API convention?" shouldn't require scanning 50 event files |
| Topical only | No low-friction write path for agents — event files require only frontmatter, not schema knowledge |
| Inheritance model | Flat merge is simpler, no parent/child override complexity |

The hybrid (topical + events) gives agents a fast read path (topical files) and a fast write path (events/), with coordinator curation bridging the two.
