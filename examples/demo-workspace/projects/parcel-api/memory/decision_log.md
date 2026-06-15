# Decision Log

## 2026-06-02
- v0.2.0 release cut from main (PR #38). Release notes in `artifacts/`.

## 2026-05-27
- Idempotency keys live in Redis with a 24h TTL rather than Postgres — the
  duplicate-shipment window is minutes, not days, and this keeps the
  shipments table append-only (PR #35). Decision settled in codex's round-1
  findings packet on PR #35.

## 2026-05-21
- CSV export: chunk the HTTP response now (PR #31); defer query-side streaming
  until the OOM debt bites — tracked in `known_debt.md`. The chunked fix was
  sufficient to close the 504 thread; the eager full-table query load stays
  open as separate risk.

## 2026-05-04
- PostgreSQL over DynamoDB for the parcel store. Promoted to org level —
  rationale and revisit trigger in `../../../org-memory/decisions.md`.
- Project workspace initialized (`oacp init parcel-api --agents claude,codex,nova`).
