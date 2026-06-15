---
created_at_utc: 2026-05-21T16:33:30Z
date: 2026-05-21
agent: codex
project: parcel-api
type: rule
source_ref: review-packet-pr31-r1
related: ["PR #31", "event/20260508-141220-utc-timestamps-pr-review", "event/20260515-102045-utc-timestamps-webhooks"]
---

Third timestamp-format defect in three weeks: CSV export wrote naive
local-time datetimes (caught in PR #31 review). Proposing promotion to org
rule: all client-visible timestamps are UTC ISO-8601 (RFC 3339) — REST
responses, webhook payloads, and exports. Pattern has now repeated 3x, which
meets the promotion bar.
