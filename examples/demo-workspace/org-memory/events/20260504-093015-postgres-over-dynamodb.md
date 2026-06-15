---
created_at_utc: 2026-05-04T09:30:15Z
date: 2026-05-04
agent: claude
project: parcel-api
type: decision
source_ref: debrief-20260504-s3
related: ["PR #12"]
---

Chose PostgreSQL over DynamoDB for the parcel store. Tracking history is
relational (parcel → scans → locations), the team already operates Postgres
for auth, and single-region latency is fine for v1. Revisit only if sustained
scan volume exceeds ~50M rows/month.
