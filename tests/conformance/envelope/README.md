# OACP Envelope Compilation Conformance Fixtures

These fixtures define the canonical contract for envelope compilation: compiling an
admitted message's `task_profile` plus the receiver's autonomy config into a
runtime envelope (`active_envelope.json`). Runtime adapters and alternative
compiler implementations should compile each `message` + `config` pair and
compare their result to the matching file under `expected/`.

Expected decision files use this shape:

```yaml
case: clean_pr_task_compiles
config: configs/auto_review_standard.yaml
message: messages/clean_pr_task.yaml
expected:
  compiles: true
  envelope:
    constraints:
      creates_or_updates_pr: true
```

Compile-failure cases use:

```yaml
expected:
  compiles: false
  error: envelope_compile_error
```

Rules pinned by these fixtures:

- Compilation is fail-closed: a missing, unparsable, or invalid
  `task_profile`, or a malformed receiver config, must fail with
  `envelope_compile_error` (the receiver pauses the task instead of executing
  unenforced).
- The envelope embeds the receiver-side `private_repo_allowlist` so runtime
  enforcement does not depend on re-reading config.
- Granular side-effect fields absent from a legacy profile compile to
  `false`, never to "allowed".
- `counters.files_touched` always starts empty; counters are runtime state,
  not declaration state.

Volatile fields (`compiled_at_utc`, `message_sha256`, `compiler`,
`spec_version`) are intentionally not pinned here; consumers compare the
listed fields as a subset. The executable runner is
`tests/test_envelope_conformance_fixtures.py`; every expected fixture is
evaluated against `scripts/envelope_compiler.py`.
