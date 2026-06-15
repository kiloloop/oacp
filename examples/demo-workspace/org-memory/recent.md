# Org Memory — Rolling Summary

<!-- Always-loaded context for agents. Keep under ~150 lines. -->
<!-- Updated by coordinator during sync/curation. -->
<!-- Last curated: 2026-06-02 by nova -->

## Current State

- **parcel-api v0.2.0 released** (Jun 2) — idempotent shipment creation,
  signed webhooks, CSV export. Next milestone: courier-facing rate limits
  (target mid-June).
- Webhook signature rotation runbook drafted (claude, PR #40); codex review
  pending — tracked in the project's `open_threads.md`.

## Active Decisions

- **PostgreSQL over DynamoDB** for the parcel store (May 4) — relational
  tracking history; revisit above ~50M scan rows/month. Rationale in
  `decisions.md`.

## Standing Rules

- **UTC ISO-8601 timestamps on every client-visible surface** (promoted
  May 21 from 3 events) — REST, webhooks, exports. Details and provenance in
  `rules.md`.
