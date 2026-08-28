# Org-memory debrief store conformance corpus

Pinned decision contract for `oacp doctor`'s Org Memory category
(`check_org_memory` in `scripts/oacp_doctor.py`), which checks the
setup of the central debrief store specified in
`docs/protocol/org_memory.md` → "Debrief Store": directory presence,
canonical path layout, lingering staging artifacts, and irregular
entries. The doctor never opens debrief files — content and format
verification belong to the writer contract and git history, so no case
here exercises file contents.

Each case under `cases/<name>/` holds:

- `org-memory/` — a miniature store tree copied into a temp OACP home
  by the runner (`tests/test_org_memory_doctor.py`).
- `expected.yaml` — the exact set of non-ok findings the checker must
  emit (`findings: []` means the store must validate clean), each as
  `name` + `severity` + optional `message_contains`.

Finding names and severities here are pinned: changing them is a
behavior change to doctor's output contract and must update this corpus
in the same commit.
