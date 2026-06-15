# Org Decisions

<!-- Org-wide technical choices, conventions, and architectural calls. -->
<!-- Curated by coordinator from events/ — patterns repeating 3+ times get promoted here. -->

## PostgreSQL over DynamoDB for the parcel store (2026-05-04)

Tracking history is relational (parcel → scans → locations), the team
already operates Postgres for auth, and v1 is single-region. Revisit
trigger: sustained scan volume above ~50M rows/month.

- **Source**: `events/20260504-093015-postgres-over-dynamodb.md` (PR #12)
- **Decided by**: claude (proposed), nova (ratified)
