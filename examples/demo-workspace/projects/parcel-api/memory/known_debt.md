# Known Debt

Use this file to track verified, unresolved project debt that future sessions
should not rediscover from scratch.

| Item | Severity | Date Found | Source | Status |
| --- | --- | --- | --- | --- |
| N+1 scan-history query on `GET /parcels/{id}` — one query per scan row | Medium | 2026-05-08 | PR #18 review | Paid down 2026-05-30 (PR #37, eager-load with joined scan history) |
| Webhook retries have no dead-letter queue — failed deliveries vanish after 5 attempts | Medium | 2026-05-15 | PR #24 review | Open |
| CSV export loads the full scan table into memory; OOM risk past ~1M rows | Low | 2026-05-21 | PR #31 review | Open |
| `parcel_api/time.py` serializer helper not yet adopted by admin UI templates | Low | 2026-05-22 | codex audit | Open |
