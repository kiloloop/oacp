---
created_at_utc: 2026-05-08T14:12:20Z
date: 2026-05-08
agent: codex
project: parcel-api
type: event
source_ref: review-packet-pr18-r1
related: ["PR #18"]
---

PR #18 review: `GET /parcels/{id}` returned scan timestamps in server local
time with no offset. Fixed to UTC ISO-8601 before merge. Flagging because two
client integrations were already compensating for it independently.
