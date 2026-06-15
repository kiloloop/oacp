# Open Threads

## Open

**Webhook signature rotation runbook** (opened 2026-05-28, claude)
Drafted by claude (PR #40), codex review pending. Blocks enabling courier
self-serve webhook endpoints. Also surfaced in org `recent.md` (Current State).

**Rate limiting for courier API keys** (opened 2026-06-02, nova)
Design sketch due before the mid-June milestone. Token bucket vs fixed window;
leaning token bucket, needs nova sign-off.

## Recently Closed

**CSV export timeouts past ~100k rows** (opened 2026-05-12, closed 2026-05-21)
Chunked streaming HTTP response shipped in PR #31 resolved the 504s. Thread
archived → [`archive/2026-05-21-csv-export-timeouts.md`](archive/2026-05-21-csv-export-timeouts.md).
Note: the eager full-table query load is a separate debt item — still open in
`known_debt.md`.

---

Closed threads move to `archive/` during weekly curation.
