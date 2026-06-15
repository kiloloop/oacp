# Org Rules

<!-- Standing conventions: naming, API patterns, timezone, style, etc. -->
<!-- Curated by coordinator from events/ — patterns repeating 3+ times get promoted here. -->

## Timestamps: UTC ISO-8601 (RFC 3339) on every client-visible surface

All client-visible timestamps — REST responses, webhook payloads, exports —
use UTC ISO-8601 with an explicit `Z` offset. No epoch seconds, no naive
local time.

- **Promoted**: 2026-05-21 by nova, from 3 events:
  - `events/20260508-141220-utc-timestamps-pr-review.md` (REST responses)
  - `events/20260515-102045-utc-timestamps-webhooks.md` (webhook payloads)
  - `events/20260521-163330-utc-timestamps-rule-proposal.md` (CSV export + promotion proposal)
- **Enforcement**: codex review checklist; shared serializer helper in
  `parcel_api/time.py`
