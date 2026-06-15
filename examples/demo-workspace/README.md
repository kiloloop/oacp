# Demo Workspace — OACP Memory, Populated

*This is what your project remembers after a month of agent sessions — without
anyone writing a wiki.*

A constructed example of OACP's two memory levels mid-flight, for a fictional
three-agent team. The blank skeletons that `oacp init` and `oacp org-memory
init` create show the *format*; this shows the *system working* — decisions
accumulating, debt tracked with origins, threads opened and closed, and a
repeating pattern promoted into standing knowledge.

Everything here is fictional and self-contained. The directory tree is shaped
exactly like a real `$OACP_HOME`, so it also serves as drop-in fixture data
for tools that read OACP workspaces (demos, screenshots, the OACP Explorer).

## The team

| Agent | Role |
| --- | --- |
| `claude` | Implementer (API + data model) |
| `codex` | QA / reviewer |
| `nova` | Coordinator — curates org-memory, cuts releases |

One project: **parcel-api**, a parcel-tracking REST service (FastAPI +
PostgreSQL + Redis).

## What to look at first: project memory

The four files under `projects/parcel-api/memory/` map 1:1 onto what
engineering teams already track informally — the populated state speaks for
itself.

**Start with [`projects/parcel-api/memory/known_debt.md`](projects/parcel-api/memory/known_debt.md)**

Each debt row has an origin. The N+1 scan-history query was caught in a PR
review (PR #18), logged with its source, fixed in PR #37 three weeks later, and
the row now shows "Paid down 2026-05-30" instead of disappearing. Nothing was
lost; you can see where it came from and when it was resolved. The remaining
three rows are open — logged, not rediscovered every session.

**Then [`projects/parcel-api/memory/open_threads.md`](projects/parcel-api/memory/open_threads.md)**

Two threads are open with opened-dates and owning agents. One — the CSV export
timeout problem — is in "Recently Closed": it has an opened-date (2026-05-12),
a closed-date (2026-05-21), a one-line resolution, and a pointer to the full
thread archive. The archive file
([`projects/parcel-api/memory/archive/2026-05-21-csv-export-timeouts.md`](projects/parcel-api/memory/archive/2026-05-21-csv-export-timeouts.md))
contains three dated wrap-up entries from the agents involved — root cause,
codex's staging repro and the separate debt item it surfaced, and the final
close note linking the PR that fixed it.

**Then [`projects/parcel-api/memory/decision_log.md`](projects/parcel-api/memory/decision_log.md)**

Dated decisions with rationale, PR references, and links to artifacts. The
May 21 entry records the *why* behind deferring query-side streaming. The
May 27 idempotency entry points at the review packet where it was settled.
The May 4 init entry records the command used to bootstrap the workspace.

**And [`projects/parcel-api/memory/project_facts.md`](projects/parcel-api/memory/project_facts.md)**

Stack, team, conventions — the static context every session loads.

## The cross-project layer: org-memory curation

The org-memory layer accumulates raw events across all projects and the
coordinator curates them into standing knowledge. Follow one pattern through
the loop:

1. **An event happens** — codex catches local-time timestamps in a PR review:
   [`org-memory/events/20260508-141220-utc-timestamps-pr-review.md`](org-memory/events/20260508-141220-utc-timestamps-pr-review.md)
2. **It repeats** — a week later, webhook payloads have the same class of bug:
   [`org-memory/events/20260515-102045-utc-timestamps-webhooks.md`](org-memory/events/20260515-102045-utc-timestamps-webhooks.md)
3. **Third strike, promotion proposed** — agents can propose rules via a
   `type: rule` event; the coordinator decides:
   [`org-memory/events/20260521-163330-utc-timestamps-rule-proposal.md`](org-memory/events/20260521-163330-utc-timestamps-rule-proposal.md)
4. **The coordinator promotes it** — [`org-memory/rules.md`](org-memory/rules.md)
   now carries the rule with "promoted from 3 events" provenance pointing back
   at all three.
5. **The rolling summary reflects it** — [`org-memory/recent.md`](org-memory/recent.md)
   (the always-loaded file) lists the rule under Standing Rules, so every
   agent sees it without scanning events.

`decisions.md` shows the same flow for a one-shot architectural call
(PostgreSQL over DynamoDB) rather than a repeating pattern.

## The two levels

```
demo-workspace/                       ← shaped like $OACP_HOME
  projects/
    parcel-api/
      memory/                         project-scoped durable memory
        project_facts.md              stack, roles, conventions
        decision_log.md               dated decisions (cross-refs org level)
        known_debt.md                 verified debt — open and paid-down
        open_threads.md               open threads, recently closed
        archive/
          2026-05-21-csv-export-timeouts.md   closed thread, 3 dated entries
  org-memory/                         org-wide knowledge
    recent.md                         always-loaded rolling summary
    rules.md                          standing conventions (curated)
    decisions.md                      architectural calls (curated)
    events/                           raw signal — 6 events, 3 types
```

The two levels cross-reference where it's natural: the project's
`decision_log.md` points up to `org-memory/decisions.md` for the promoted
Postgres decision; the archived CSV thread links to the org-memory rule-proposal
event it triggered; `org-memory/recent.md` points down at the project's
`open_threads.md` for in-flight work.

## Format reference

- Event frontmatter schema, naming, and the promotion lifecycle:
  [`docs/protocol/org_memory.md`](../../docs/protocol/org_memory.md)
- Blank scaffolds: `oacp org-memory init` (org level) and `oacp init
  <project>` (project level — `memory/` files are created by the init
  script).
