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
`reason_codes`, and `matched_pattern` when present must match.

These fixtures may also include:

- `actuals:` pointing at checkpoint input under `actuals/`
- `expected.logged_notes` for demoted side-effect verb matches
- `expected.continuation_grant` for default-off and enabled grant behavior
- `expected.result.threshold_checkpoint` for envelope drift decisions
