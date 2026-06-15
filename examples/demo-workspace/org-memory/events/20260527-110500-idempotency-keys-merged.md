---
created_at_utc: 2026-05-27T11:05:00Z
date: 2026-05-27
agent: claude
project: parcel-api
type: event
source_ref: debrief-20260527-s14
related: ["PR #35"]
---

PR #35 merged: idempotency keys on `POST /shipments`. Couriers retry
label purchases on timeout; duplicate shipments dropped to zero in staging
replay. Keys live in Redis with a 24h TTL.
