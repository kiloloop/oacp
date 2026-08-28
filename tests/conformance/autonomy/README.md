# OACP Autonomy Conformance Fixtures

These fixtures define the canonical Phase 1 `auto_review` decision contract.
Runtime skills should load a receiver config, load a message, and compare their
decision to the matching file under `expected/`.

Phase 1 has no sender trust gate. Sender fields may be logged for audit
traceability, but they must not influence the decision.

Expected decision files use this shape:

```yaml
case: clean_auto_review_task
config: configs/auto_review_standard.yaml
message: messages/clean_task.yaml
expected:
  decision: auto_accepted
  mode: auto_review
  reason_codes:
    - task_profile_present
```

Consumers may add implementation-specific trace fields, but `decision`, `mode`,
`reason_codes`, and legacy `matched_pattern` when present must match. Evaluator
outputs also carry `matched_patterns`: every lexical hit has a pattern name,
source span, category, and non-empty `demotion_basis`; the executable runner
validates that additive provenance across every fixture.

These fixtures may also include:

- `now:` — the evaluation time (`YYYY-MM-DDTHH:MM:SSZ`) passed to the
  evaluator as `now_utc`. Required on any fixture whose decision depends
  on the clock (review-grant expiry, revoke-before-processing); fixtures
  without it evaluate at the real current time
- `actuals:` pointing at checkpoint input under `actuals/`
- `actuals.reauthorization` for checkpoint re-authorization arbitration:
  channel answers (`receiver_human` / `sender_reply` / `gh_comment`) judged
  against the pause being re-evaluated (same-channel, cross-channel,
  conflicting, reuse, and boundary-action cases)
- `actuals.review.live_head` for review-loop head checks: the live PR head
  the runtime observed, compared against the request's `declared_head`
- `audits:` pointing at prior same-thread audit records under `audits/`
- `expected.logged_notes` for demoted side-effect verb matches
- `expected.continuation_grant` for default-off and enabled grant behavior
- `expected.review_continuation` for review-loop continuation decisions
  (accepted, missing-approval, disabled, drifted, revoked, later-round,
  and the head-mismatch / prefix-collision recording cases)
- `expected.result.threshold_checkpoint` for envelope drift decisions
- `expected.breached` for the pinned top-level breach list
- `expected.task_profile` for full declared-profile capture
- `expected.admission_axes` for the pinned admission ledger (exact match)
- generated `matched_patterns` for complete lexical provenance; this is
  validated structurally by the runner rather than repeated in every expected
  YAML file

The executable runner is `tests/test_autonomy_gate.py`; every expected fixture
is evaluated against `scripts/autonomy_gate.py`. Evaluator reason codes are a
pinned enum, and any unregistered code fails the runner.

The `ledger_replay/` subdirectory holds an anonymized replay corpus: audit
records from a fleet corpus whose evaluator early-outs left envelope-derived
admission axes unrecorded. Each case pins the unchanged verdict together
with the complete `admission_axes` ledger and `co_occurring_reason_codes`
the evaluator must now produce; the runner is
`tests/test_autonomy_ledger_replay.py`. The same file's
`content_sensitivity` section replays the reply-only carve-out over every
content-sensitivity hard stop of one window plus a control, pinning which
records demote to a `lexical_advisory_reply_only` note and which keep the
hard stop.

The `records/` subdirectory pins a second contract class: audit-record
integrity findings (off-enum vocabulary, duplicate live evaluations,
paused-terminal shapes) validated by `scripts/finalize_autonomy_record.py`
and run by `tests/test_audit_record_conformance.py` — see
`records/README.md`.
