---
created_at_utc: 2026-05-15T10:20:45Z
date: 2026-05-15
agent: claude
project: parcel-api
type: event
source_ref: debrief-20260515-s9
related: ["PR #24", "event/20260508-141220-utc-timestamps-pr-review"]
---

Webhook delivery payloads used epoch seconds while the REST API returns
ISO-8601 — second timestamp-format mismatch in a week (see related event).
Converted webhook `occurred_at` to UTC ISO-8601 in PR #24.
