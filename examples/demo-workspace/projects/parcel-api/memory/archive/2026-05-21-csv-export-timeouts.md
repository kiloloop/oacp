# Archived Thread: CSV Export Timeouts Past ~100k Rows

Closed 2026-05-21. Chunked HTTP streaming shipped in PR #31.

---

**2026-05-12 — claude wrap-up**
Couriers running bulk export on large depots are hitting 504s at around 100k
rows. Root cause: the export endpoint serializes the full result set into memory
before writing the response. No streaming; one giant Python list held in the
worker while the client waits. Filed as a thread to track — fix is
straightforward (stream the response) but touching the export path risks the
timestamp-format issue that already burned us in PR #18, so codex should review.

**2026-05-15 — codex wrap-up**
Reproduced the timeout at 120k rows in staging. Confirmed the root cause: the
query loads the entire scan-history table for the depot into memory before any
serialization starts. Flagged that as a separate concern — the eager full-table
query load is an OOM risk independent of the streaming fix (large enough depot
will OOM the worker even after we stream the response). Logged that separately
in `../known_debt.md`. The streaming fix itself can land without it.

**2026-05-21 — claude wrap-up**
Closed. PR #31 ships chunked HTTP streaming for the CSV export endpoint — 504s
gone at 120k rows in staging. The naive-datetime bug that came up in review
turned into the third timestamp strike; codex proposed promoting it to a
standing rule (nova promoted it the same day) →
[`../../../../org-memory/events/20260521-163330-utc-timestamps-rule-proposal.md`](../../../../org-memory/events/20260521-163330-utc-timestamps-rule-proposal.md).
The eager full-table query load remains open in `../known_debt.md` — that's an
OOM risk at ~1M rows and was not in scope for this fix.
