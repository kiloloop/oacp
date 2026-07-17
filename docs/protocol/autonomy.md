# Receiver Autonomy Policy

## Purpose

OACP autonomy controls whether a receiver may move an inbox message from
`received` to `accepted` without interactive human confirmation. It does not
grant runtime tool permissions, and it does not relax agent safety defaults.

Phase 1 ships two modes:

| Mode | Behavior |
|------|----------|
| `always_pause` | Default. Receiver pauses for human review before accepting work. |
| `auto_review` | Receiver may auto-accept only messages that pass the deterministic gates below. |

Sender trust is messenger-bound and is not part of OACP autonomy v1/v2. Sender
fields may be logged in audit events for traceability, but sender identity does
not gate Phase 1 decisions.

## Receiver Config

Receiver policy lives at `agents/<receiver>/config.yaml`:

```yaml
autonomy:
  default_mode: always_pause
  auto_review_thresholds:
    max_estimated_minutes: 45
    max_expected_files_touched: 5
    destructive_ops: pause
    external_side_effects: allow_pr_artifacts
    auth_config_or_secrets: pause
    dependency_changes: pause
    public_visibility: pause
    git_push_or_deploy: pause
  allow_without_task_profile:
    - brainstorm_request
  private_repo_allowlist:
    - example-org/private-repo
  continuation_grants:
    enabled: false
```

When config is absent, receivers behave as `always_pause`. Malformed config
causes a pause and should be surfaced by `oacp doctor`.

`external_side_effects` accepts three policy actions:

| Action | Behavior |
|---|---|
| `pause` | Pause every declared external side effect. |
| `allow_pr_artifacts` | Allow PR creation/update, review comments, issue comments, and declared issue filing (`files_issues`) only when `target_repo` appears in the receiver-controlled `private_repo_allowlist`; direct main pushes, deploys, and publishes still pause, and a declared `merges_pr` always pauses at admission (`merges_pr_pause`) so merge authority passes a human at least once. |
| `allow` | Allow declared ordinary external side effects; non-demotable hard stops still pause. |

All other policy actions remain `pause`. A private PR-artifact profile must set
`target_repo: owner/repo`, `public_visibility: false`,
`external_side_effects: true`, and at least one of `creates_or_updates_pr`,
`comments_on_github`, or `files_issues` to `true`. The sender declaration is
necessary but not sufficient: `target_repo` must also match the receiver's
independent `private_repo_allowlist`. Branch commits and pushes needed to
create that artifact are folded into `external_side_effects`; direct pushes to
`main` are not. `merges_pr` deliberately never joins the auto-accept class: a
dispatch that declares it is otherwise admissible, but the merge declaration
itself pauses for human approval (a prior human-approved continuation grant
covering `merges_pr` satisfies that requirement).

The legacy `git_push_or_deploy: pause` policy is enforced at Gate 3 for direct
main pushes and other non-demotable action phrases. There is intentionally no
separate task-profile field: ordinary branch pushes supporting an allowlisted
private PR artifact are part of `external_side_effects`, while direct main
pushes remain hard stops.

## Message Fields

Messages may include top-level `autonomy_hint: auto_proceed`. This is advisory
only; the receiver's config and local evaluator are authoritative.

For `auto_review`, `task_request` and `question` messages require a
machine-parseable `task_profile` block in `body`:

```yaml
task_profile:
  estimated_minutes: 20
  risk_tier: P3
  expected_files_touched: 3
  destructive_ops: false
  external_side_effects: false
  touches_auth_config_or_secrets: false
  touches_dependencies: false
  public_visibility: false
  target_repo: ""
  creates_or_updates_pr: false
  comments_on_github: false
  commits_changes: false
  merges_pr: false
  files_issues: false
  sends_oacp_reply_only: true
  continuation_grants: {}
```

Missing `task_profile` pauses with `task_profile_missing`. An unparsable or
schema-invalid profile pauses with `task_profile_unparsable`; it is not a fatal
message-schema error. Message types listed in `allow_without_task_profile`, such
as `brainstorm_request`, may auto-accept without the block.

The core declaration is complete only when it includes the two numeric fields,
`risk_tier`, and all five legacy risk booleans shown above. Granular side-effect
booleans are optional but must agree with `external_side_effects`; a profile
that declares a PR/comment/commit/merge/issue capability while declaring no
external side effects pauses with `declaration_error`.

The granular vocabulary is `creates_or_updates_pr`, `comments_on_github`,
`commits_changes`, `merges_pr` (landing a PR), and `files_issues` (issue
create/edit/close plus label creation — issue-adjacent metadata). The
declarable set, the continuation-grant coverable set, and the checkpoint's
`side_effects_actual` keys are the same set by construction: everything a
checkpoint can observe, a sender can declare and a grant can cover.

### Sender-marked guardrails

Senders may isolate non-operative safety language in a fenced body section:

````markdown
```oacp-guardrails
Do not merge, deploy, publish, or touch credentials.
```
````

Gate 3 excludes well-formed `oacp-guardrails` fence contents from ordinary
side-effect, auth/config/secrets, and ambiguous-scope pause classification, but
records every matching term as a `lexical_advisory`; fenced text is never
invisible to the audit. Destructive commands, direct main pushes, credential
rotation, dependency installation, public-repository text, memory SSOT text,
and pricing/commercial content are scanned across the raw body and remain hard
even inside the fence. An unclosed or differently labeled fence is not skipped.

## Four-Gate Evaluator

If any required gate is missing or uncertain, the receiver pauses.

1. **Message integrity**
   - Message validates against OACP schema.
   - Message is not expired.
   - Raw YAML hash is recorded as `message_sha256` before processing.
   - Message ID has not already been auto-accepted by this receiver.
   - `autonomy_hint`, if present, remains advisory only.
2. **Declared task profile**
   - Required for `task_request` and `question`.
   - Normalizes the profile into a scope envelope with time, files, risk
     booleans, side-effect booleans, and optional continuation grants.
   - Cross-checks `estimated_minutes` (45-minute standard cap),
     `expected_files_touched` (5-file standard cap), destructive scope,
     sensitive scope, and side-effect scope against receiver policy.
   - Applies `allow_pr_artifacts` only to the declared private-repository
     artifact class (`creates_or_updates_pr`, `comments_on_github`,
     `files_issues`) when `target_repo` also appears in the
     receiver-controlled allowlist; public or unlisted repository artifacts
     and every other external side-effect class pause, and a declared
     `merges_pr` pauses at admission regardless.
   - Pauses contradictory profile fields with `declaration_error`.
3. **Receiver classification**
   - Pause unconditionally on destructive command tokens: `rm -rf`, `--force`,
     `--no-verify`, `--dangerously-skip-permissions`.
   - For task-like messages, pause when the body asks for deploy, push to main,
     merge, publish, credential rotation, or dependency install.
   - When a complete profile explicitly declares `merges_pr: true`, the
     lexical `merge` match alone is demoted to a `lexical_advisory_declared`
     note so the declared merge reaches the granular Gate-2 path — it still
     pauses there with `merges_pr_pause` on first admission, and only a
     human-approved continuation grant covering `merges_pr` admits the
     follow-up. Merge wording without the declaration stays a hard stop.
   - For types listed in `allow_without_task_profile`, side-effect verbs such
     as deploy/publish/merge are logged as notes instead of hard stops.
     Destructive tokens still pause.
   - Path-like tokens such as `packets/deploy/` are not deploy verbs.
   - Exclude sender-marked `oacp-guardrails` fences from demotable pause
     classification while logging their matches as advisories. Suppress
     demotable matches in clauses headed by `no`, `not`, `never`, or `do not`.
   - With a complete profile, demote side-effect or sensitive-scope lexical
     matches to a logged `lexical_advisory` when the corresponding declaration
     is `false`. Missing/unparsable profiles and contradictory declarations do
     not receive this demotion.
   - When policy explicitly uses `external_side_effects: allow`, declared
     ordinary external side-effect verbs are also advisory; non-demotable hard
     stops remain hard.
   - Pause when the body touches declared auth/config/secrets/credentials or
     public-repository scope, or any memory SSOT scope.
   - Keep `commercial`, `pricing`, public-repository text, and memory SSOT text
     hard with no fence or negation demotion. Pricing/commercial matches are
     reported separately as `hard_stop_content_sensitivity` rather than action
     risk.
   - Pause when file scope is ambiguous or broader than the declared profile.
4. **Runtime/workspace**
   - Worktree is clean or the task can be isolated to a fresh branch.
   - No conflicting active task exists on the same repo.
   - Required tools are available.

LLM judgment may reduce false positives after deterministic gates pass. It
cannot override hard stops.

## Hard-Stop Override

Regardless of autonomy mode, receivers must pause on destructive command tokens
(`rm -rf`, `--force`, `--no-verify`,
`--dangerously-skip-permissions`), direct main pushes, credential rotation,
dependency installation, memory SSOT scope, and pricing/commercial content.
Declared auth/config/secrets/dependencies/public scope still pauses through Gate
2. The only standard external-side-effect exception is the configured
`allow_pr_artifacts` private-repository class described above.

Continuation grants do not override destructive tokens, auth/secrets/credentials,
dependency, public-scope, pricing/commercial, config, or memory-SSOT hard stops.
When explicitly enabled, a valid continuation grant may cover declared external
side effects only for the scoped PR, GitHub comment, or commit continuation
fields that the grant marks true.

## Audit Events

Every autonomy decision writes one YAML file:

`agents/<receiver>/audit/autonomy_decisions/YYYYMMDDTHHMMSSZ_<message-id>.yaml`

```yaml
schema_version: 2
spec_version: "0.4.0"
created_at_utc: "2026-05-12T13:23:25Z"
receiver: codex
sender: iris
message_id: msg-20260512132325-iris-de62
message_type: task_request
message_subject: "Small docs cleanup"
conversation_id: conv-20260512-iris-001
parent_message_id: null
message_path: agents/codex/inbox/20260512132325_iris_task_request.yaml
message_sha256: "..."
decision: auto_accepted
mode: auto_review
policy_path: agents/codex/config.yaml
policy_sha256: "..."
reason_codes:
  - task_profile_present
  - risk_threshold_passed
thresholds:
  max_estimated_minutes: 45
  max_expected_files_touched: 5
task_profile:
  estimated_minutes: 20
  risk_tier: P3
  expected_files_touched: 3
  destructive_ops: false
  external_side_effects: false
  touches_auth_config_or_secrets: false
  touches_dependencies: false
  public_visibility: false
  target_repo: ""
  creates_or_updates_pr: false
  comments_on_github: false
  commits_changes: false
  merges_pr: false
  files_issues: false
  sends_oacp_reply_only: true
  continuation_grants: {}
breached: []
co_occurring_reason_codes: []
runtime:
  agent: codex
  model: gpt-5
evaluator:
  source: scripts/autonomy_gate.py
  content_sha256: "<sha256 of the evaluator file bytes>"
  git_sha: 0e382a1        # best-effort; null outside a clean checkout
  executed: true
result:
  final_state: done
  completion_kind: auto_accepted
  actual_minutes: null
  actual_files_touched: null
  predicted_risk_materialized: false
  completed_at_utc: null
  envelope_enforcement: none
  threshold_checkpoint:
    evaluated: false
    actual_minutes: null
    actual_files_touched: null
    side_effects_actual: {}
    breached: false
    breached_fields: []
    declaration_errors: []
    breach_basis: null
    paused_at_utc: null
    action: not_evaluated
    predicted_risk_materialized: false
    completed_at_utc: null
  human_outcome:
    recorded: false
    actor: null
    decision: null
    decided_at_utc: null
    decision_latency_seconds: null
    pause_reason_codes: []
    grant:
      decision: not_recorded
      request_present: false
      request_error: null
      requested_scope: null
      granted_scope: null
  reply_message_id: msg-...
  artifacts: []
```

`policy_path` and `policy_sha256` may be null when the pause is caused by
missing or malformed config. `sender` is normally traceability metadata and
also binds an enabled standing grant to the sender that received approval.
`policy_sha256` is the SHA-256 of a canonical, key-sorted serialization of the
parsed policy, so comments and YAML formatting do not produce false drift.
`spec_version: "0.4.0"` pins Gate 1 integrity enforcement plus the recalibrated
Gate 2/3 policy, full task-profile capture, explicit `breached` list, and the
outcome block shown above. Audit `schema_version: 2` adds thread identity and
the structured `result.human_outcome` block. Recorders may upgrade a v1 audit
to v2 when the first human outcome is written; standing grants trust only v2
records.

`evaluator` is **gate-emitted, not receiver-composed**: the evaluator
self-stamps its provenance into every decision it returns, and receivers
copy the block verbatim into the audit record. `content_sha256` (the hash
of the evaluator file bytes) is the load-bearing identity — it survives
wheel installs and identifies local-ahead code that no commit names.
`git_sha` is best-effort convenience, present only when the file matches
the committed blob at HEAD; a dirty tree, an untracked copy, or a
non-checkout install all record `null` rather than a SHA that names code
which did not run. The only evaluator block a receiver ever authors by
hand is the no-executed-gate case: `executed: false` with no hashes.

`breached` is always an ordered list, but its entries intentionally reflect the
evaluation phase. Admission-time pauses record pinned gate reason codes (for
example `estimated_minutes_exceeds_threshold`); post-accept checkpoint pauses
record the concrete declared/actual field paths that exceeded the envelope
(for example `side_effects_actual.creates_or_updates_pr`). `completion_kind`
distinguishes those two phases; `reason_codes` remains the canonical taxonomy
for the decision itself.

`co_occurring_reason_codes` records pinned reason codes that also held but did
not drive the verdict. Numeric Gate-2 thresholds are evaluated before any
early-out, so a pause taken for another reason (most commonly a lexical hard
stop) still records a co-occurring `estimated_minutes_exceeds_threshold` or
`expected_files_touched_exceeds_threshold`: silence in a pause record means
the thresholds passed, never that they went unevaluated. The list is sorted,
deduplicated against `reason_codes`, and empty on auto-accepted decisions.
Threshold-calibration analytics should read `reason_codes` and
`co_occurring_reason_codes` together.

### Pinned completion_kind taxonomy

`result.completion_kind` names the terminal shape of the **evaluation** only —
one axis, enumerated and conformance-pinned like the reason codes:

| Kind | Meaning |
|---|---|
| `auto_accepted` | Every gate passed; work may begin. |
| `admission_paused` | Paused at admission (mode, integrity, profile, lexical, declared-risk, or threshold cause — the cause lives in `reason_codes`). |
| `checkpoint_paused` | Paused at a post-accept §E threshold/declaration checkpoint. |
| `config_malformed` | Receiver config could not be resolved; no gates ran. |

The pause *cause* belongs to `reason_codes`, the run state to
`result.final_state`, and human decisions to `result.human_outcome`.
Receivers copy the evaluator's `completion_kind` verbatim and never overwrite
it at terminal update time — a paused-then-approved task keeps
`admission_paused` while `final_state` moves to `done` and `human_outcome`
records the approval. Receiver-composed values outside this enum are
non-conforming. Records written before this pin carry mixed
cause/event/state values (`hard_stop`, bare `paused`, fused decision+state
kinds) and cannot be bucketed against the pinned enum.

### Human approval and decline outcomes

When a paused task is approved, modified, or declined, record the decision in
the same audit file:

```bash
oacp autonomy-outcome <audit.yaml> \
  --decision approved \
  --decided-at 2026-05-12T13:25:00Z \
  --actor alice
```

The recorder copies the pause reason codes, computes decision latency from the
pause moment, and locks the full read-modify-write sequence before an atomic
replacement. It refuses to overwrite a recorded outcome unless `--replace` is
explicit. `decision` is `approved`, `modified`, or `declined`.

The recorder is state-aware about where the pause moment lives:

- **Admission pauses** (`completion_kind: admission_paused`): the record was
  created at the pause, so latency measures from `created_at_utc` — even
  when actuals attached to the evaluation happen to breach the checkpoint
  (the pause the human decided on is still the admission pause).
- **Checkpoint pauses** (an auto-accepted admission whose
  `threshold_checkpoint.breached` is true, or a paused decision whose
  `completion_kind` is `checkpoint_paused`): the record predates the pause,
  so latency measures from `threshold_checkpoint.paused_at_utc` and
  `pause_reason_codes` reflects the checkpoint (`threshold_checkpoint_breached`
  or `declaration_error`). A checkpoint record without `paused_at_utc` is
  refused rather than silently measured from admission time.

For records that genuinely predate the pinned `completion_kind` enum
(schema version 1 with no pinned kind), the recorder falls back to the
checkpoint reason codes to classify the pause phase. A current-schema
record whose kind is missing or out of vocabulary is refused loudly —
it is malformed, not legacy — and the breached in-place auto-accepted
shape must itself carry `checkpoint_paused` (the checkpoint
re-evaluation is what updated the result block; any other kind there is
refused as inconsistent).

Latency values on checkpoint records written before `paused_at_utc` existed
measure from admission and are not comparable with post-fix records.

`actor` is the deciding human's stable handle: one canonical, whitespace-free
identifier per person, fleet-wide (for example `alice` — not a machine
username, not the generic `human`), so outcomes join across receivers. The
value is free-form but must be non-null whenever a human decided; the
recorder warns when the anonymous default `human` ships.

Grant handling is separate so task approval never silently creates a standing
grant:

- `--grant-decision not_requested` (default): no valid grant request or grant
  decision was involved; a valid recorded request requires an explicit grant
  decision, while a malformed request is preserved as `request_error`;
- `approved`: approve the requested grant scope, or an explicit scope supplied
  with `--grant-scope-file`;
- `modified`: require an explicit replacement scope file;
- `denied`: record that the task may proceed or decline without granting a
  standing continuation.

An approved or modified standing grant is valid only when the task decision is
also approved or modified. A declined task cannot approve a grant. A malformed
grant request never prevents recording the task-level outcome; the recorder
sets `grant.request_error` and requires an explicit replacement scope before
that malformed request can be approved or modified.

### Pinned reason-code taxonomy

Evaluator implementations must reject unpinned reason codes. The canonical
families are:

- integrity/config: `config_malformed`, `mode_always_pause`,
  `message_invalid`, `message_expired`, `message_replayed`,
  `task_profile_missing`, `task_profile_unparsable`,
  `risk_obvious_no_profile`, `envelope_compile_error`;
- declaration/threshold: `declaration_error`,
  `estimated_minutes_exceeds_threshold`,
  `expected_files_touched_exceeds_threshold`, `destructive_ops_pause`,
  `auth_config_or_secrets_pause`, `dependency_changes_pause`,
  `public_visibility_pause`, `external_side_effects_pause`,
  `external_side_effects_not_pr_artifact`, and the granular side-effect pause
  codes;
- classification: `hard_stop_destructive_command`,
  `hard_stop_external_side_effect`, `hard_stop_sensitive_scope`,
  `hard_stop_content_sensitivity`, `file_scope_ambiguous`, and
  `lexical_advisory`;
- continuation/checkpoint and success codes pinned by the executable fixtures
  under `tests/conformance/autonomy/`: `continuation_grant_accepted`,
  `continuation_grant_denied`, `continuation_grant_ignored_disabled`,
  `continuation_grant_missing_approval`,
  `continuation_grant_missing_scope`, `continuation_grant_missing_thread`,
  `continuation_grant_scope_exceeded`, `threshold_checkpoint_breached`,
  `message_valid`, `message_not_expired`, `message_hash_recorded`,
  `task_profile_present`, `task_profile_not_required`, `task_type_allowed`,
  `risk_threshold_passed`, `hard_stops_clear`, and
  `workspace_check_required`.

## State Transition Metadata

Auto-acceptance preserves the `received -> accepted` transition and records why:

```yaml
transition: received_to_accepted
accepted_by: autonomy_policy
human_confirmed: false
autonomy_mode: auto_review
policy_ref: agents/codex/config.yaml
policy_hash: sha256:...
reason_codes:
  - task_profile_present
  - risk_threshold_passed
```

## Mental Model

`auto_review` is OACP's analogue to Claude Code's `acceptEdits` mode: class-based
pre-approval within a local trust domain, bounded by bright-line hard stops.

The analogy is about user contract, not mechanism. OACP decides pre-execution
from message content and declared `task_profile`; runtime tools still enforce
their own permissions at action time.

## Worked Example

A receiver configured with `default_mode: auto_review` receives:

```markdown
## Task
Clean up the build directory: `rm -rf dist/ && rebuild`.

task_profile:
  estimated_minutes: 5
  expected_files_touched: 1
  destructive_ops: false
```

Decision trace:

- Gate 1 passes: schema valid, not expired, hash recorded.
- Gate 2 passes: profile present and within thresholds.
- Gate 3 fails: body matches `rm -rf`.
- Decision: `paused`.
- Reason codes: `hard_stop_destructive_command`.
- Audit event includes `matched_pattern: "rm -rf"`.

The receiver must pause before any action runs. No autonomy mode can override
the hard stop.

## Threshold-Exceeded Checkpoint

Receivers must evaluate a threshold checkpoint if work expands beyond the
declared scope envelope after acceptance:

- `Blocked: autonomy threshold exceeded — files_touched expected 3, now 12`
- `Blocked: autonomy threshold exceeded — prompt was docs-only, now requires credential access`
- `Blocked: autonomy threshold exceeded — task expanded into untyped/unconfigured capability`

The audit result records:

```yaml
result:
  final_state: paused
  completion_kind: checkpoint_paused
  actual_minutes: 25
  actual_files_touched: 4
  predicted_risk_materialized: true
  completed_at_utc: "2026-05-12T13:48:25Z"
  threshold_checkpoint:
    evaluated: true
    actual_minutes: 25
    actual_files_touched: 4
    side_effects_actual:
      creates_or_updates_pr: true
      comments_on_github: true
      commits_changes: true
    breached: true
    breached_fields:
      - actual_files_touched
    breach_basis: realized
    paused_at_utc: "2026-05-12T13:48:25Z"
    action: paused_for_reauthorization
```

If an undeclared side effect materializes, the receiver pauses with
`declaration_error`; `threshold_checkpoint.declaration_errors` identifies the
actual side-effect field. This checkpoint is mandatory before performing any
newly discovered capability or outward action.

A breached checkpoint stamps two fields beyond the breach itself:

- `paused_at_utc` — when the checkpoint fired. Receivers pass the actual
  pause moment (for example the sender-notification timestamp) in the
  checkpoint actuals; the evaluator stamps the evaluation time only as a
  fallback. This is the timestamp human-decision latency measures from.
- `breach_basis` — `realized` when the actuals record work that already
  happened (the default), `declared_intent` when the checkpoint fired
  prospectively: the undeclared action was caught **before** it
  materialized, per the mandatory pre-action rule above. A
  `declared_intent` record legitimately combines `breached: true` with
  all-false realized effects and low actual counts — the sender's
  under-declaration was caught, not an executed drift.

A prospective correction is expressed through its own checkpoint input,
never by marking a realized effect true (that would assert an outward
action that never happened). The receiver passes the declared-profile
field paths the correction invalidated:

```yaml
actuals:
  actual_minutes: 1
  actual_files_touched: 0
  declared_intent_fields:
    - task_profile.merges_pr
  paused_at_utc: "2026-05-12T13:48:25Z"
```

Each entry must name a monotone risky capability boolean
(`task_profile.<field>`); the restrictive `sends_oacp_reply_only` is
excluded — a false-to-true flip on it cannot represent a risky
correction. The listed paths land in `breached_fields` and
`declaration_errors` directly, `breached` becomes true with every
`side_effects_actual` key still false, `breach_basis` is stamped
`declared_intent`, and `predicted_risk_materialized` is pinned false
(caught before materialization by definition — an explicit true is
rejected). The two shapes are mutually exclusive by validation, on
derived sources as well as the explicit basis: `declared_intent_fields`
combined with a realized breach source (a numeric overrun or an
undeclared realized effect), with any true `side_effects_actual` key, or
naming a capability already authorized (declared true in the envelope,
or covered by an accepted continuation grant) is rejected — as is an
explicit `breach_basis` inconsistent with the input (`realized` alongside
intent fields, `declared_intent` without them). A mixed situation records
the realized breach on its own, then re-evaluates the correction.

## Envelope Compilation (Phase 2)

Phase 2 turns the declared `task_profile` from reviewed intent into enforced
runtime constraints. After a message is admitted (auto-accepted, or paused
and then human-approved), the receiver compiles the profile plus its own
autonomy config into a runtime envelope:

```
oacp envelope compile <message.yaml> --receiver <agent>
```

The envelope is written to
`agents/<receiver>/state/active_envelope.json`:

```json
{
  "envelope_version": 1,
  "spec_version": "0.4.0",
  "compiler": "envelope_compiler.py",
  "compiled_at_utc": "2026-07-12T02:00:00Z",
  "project": "my-project",
  "receiver": "claude",
  "message_id": "msg-...",
  "message_sha256": "...",
  "constraints": {
    "estimated_minutes": 30,
    "expected_files_touched": 4,
    "risk_tier": "P2",
    "target_repo": "example-org/private-repo",
    "destructive_ops": false,
    "external_side_effects": true,
    "creates_or_updates_pr": true,
    "comments_on_github": false,
    "commits_changes": true,
    "merges_pr": false,
    "files_issues": false,
    "sends_oacp_reply_only": false,
    "touches_auth_config_or_secrets": false,
    "touches_dependencies": false,
    "public_visibility": false,
    "private_repo_allowlist": ["example-org/private-repo"]
  },
  "counters": {"files_touched": []},
  "enforcement": "hooks"
}
```

Compilation rules:

- **Fail closed.** A missing, unparsable, or invalid profile — or a malformed
  receiver config — fails compilation, and the receiver pauses the task with
  reason code `envelope_compile_error` instead of executing unenforced.
- The compiler reuses the gate evaluator's normalization and pattern
  constants directly, so admission spec and runtime enforcement cannot drift.
- The receiver-side `private_repo_allowlist` is embedded at compile time;
  runtime enforcement never trusts sender declarations alone.
- Granular side-effect fields absent from a legacy profile compile to
  `false`. `counters` are runtime state and always start empty.

### Delivery: static shim, dynamic envelope

Runtime adapters enforce the envelope at the tool-call layer. The Claude
adapter is a PreToolUse hook (`oacp-envelope-hook`, matcher
`Bash|Edit|Write|NotebookEdit`) registered **once** by `oacp setup claude`.
Per-task constraints live only in the compiled envelope file — no per-task
settings mutation, effective mid-session, and a strict no-op while no
envelope is active. The receiver compiles the envelope at task pickup and
clears it (`oacp envelope clear`) at completion; the adapter sanctions that
completion clear against the task's audit record (see below), so the
enforcement window can be exited from inside the session exactly once the
task lifecycle is over.

Runtime decisions:

- **deny** — the call breaches a declared-false capability (destructive
  tokens, undeclared commits/pushes/PR mutations, undeclared merges or
  issue filing, secret-class or dependency-manifest writes — including
  determinable Bash write targets such as redirects and common writer
  programs), targets a repo outside the embedded allowlist or pinned
  `target_repo`, pushes to a protected branch or with bulk-ref flags
  (`--mirror`, `--all`, `--delete`), is a GitHub mutation outside every
  allow class (releases, repo/gist/secret mutations, non-create label
  management), or attempts envelope self-modification (`oacp envelope
  compile` from inside the enveloped session — always; `oacp envelope
  clear` until the task's audit record shows a terminal outcome).
  The GitHub allow classes mirror the granulars: `gh pr merge` requires a
  declared `merges_pr`; `gh issue create/edit/close` and `gh label create`
  require a declared `files_issues`; both remain subject to the repo
  allowlist/visibility gate. That gate judges the repository gh will
  actually mutate: every repository selector on the command — each
  `-R/--repo` occurrence, any URL-shaped positional, and a `GH_REPO`
  environment assignment — must agree on a single repository before it is
  gated, so a positional URL, a repeated flag, or an inline assignment
  cannot retarget an approved command at another repository. `GH_REPO` is
  judged as the *effective* environment the Bash child inherits — ambient
  hook-process values seeded first, inline assignments applied over them
  with shell precedence — so a selector exported before the session
  started is gated too. Any effective `GH_HOST` escalates outright, and
  the allow classes are judged only as standalone simple commands —
  inside a compound command an earlier segment (`cd`, `export`, an
  assignment) could retarget the mutation after validation, so those
  escalate.
- **ask** — the adapter cannot confidently classify the call (shell
  indirection like `bash -c`, wrapper flags, unknown GitHub or oacp
  mutations, implicit `gh api` writes, unresolvable or conflicting
  repository selectors). The exact command is escalated for just-in-time
  review; unenforceable never silently degrades to allowed.
- **allow** — emitted as *no output*: the envelope can only narrow the
  harness's own permission surface, never widen or bypass it.
- `oacp send` is never denied; it is the checkpoint notification pipe. The
  exemption is exactly that wide: read-only oacp subcommands pass, all other
  oacp mutations are classified.
- Determinable Bash write targets feed the same distinct-file counter as
  Edit/Write calls (`/dev/*` excluded), so shell writes cannot bypass
  `expected_files_touched`.
- Protocol bookkeeping never consumes the file budget. The receiver's own
  `audit/`, `inbox/`, and `outbox/` directories and the runtime scratchpad
  (reply/body-file composition) are the enforcement layer's instrumentation
  surfaces, not task scope: writes there skip the counter entirely. The
  exemption is receiver-scoped (a peer agent's directories are task scope),
  containment is judged on resolved filesystem targets (a symlink planted
  under an exempt root that points into ordinary task scope stays counted),
  and it applies only to the counter — the secret-class and
  dependency-manifest gates still fire on exempt paths, and the receiver's
  `config.yaml` sits outside the exempt directories. Without this class, a
  tightly declared task is guaranteed to trip the ceiling at close-out on
  its own mandatory audit write.
- Two receiver surfaces are the opposite of exempt. The receiver's `state/`
  directory holds the active envelope itself: writing, removing,
  relocating, or copying anything under it from inside the session is
  denied as envelope self-modification, categorically — filesystem-mutator
  operands (`rm`, `unlink`, `mv`, `cp`, …) are gated by resolved path
  regardless of source/destination role, not just write targets. Operands
  are judged as the utility parses them (GNU target-directory spellings and
  `--` included), and mutator operands or Bash write targets bearing shell
  expansion syntax escalate to **ask** — the shell expands patterns after
  classification, so a literal spelling proves nothing about the effective
  target. Trust
  roots (the receiver's `trust/` pins and the project trust catalog) are
  authority-bearing auth configuration: writes and mutations are denied
  unless the envelope declares `touches_auth_config_or_secrets`, and even
  then they count as ordinary task scope. CLI-mediated updates
  (`oacp trust import`) are classified as commands and escalate for review
  rather than touching the counter.

### Completion clear

The documented completion step — `oacp envelope clear` — is validated, not
blanket-denied. The adapter scans the receiver's `audit/autonomy_decisions/`
directory and selects the newest record whose **content** identity matches
the active envelope — `message_id` and `receiver` fields in the record
itself; filenames are never trusted, and the envelope's message id (pinned
to a safe-id grammar at compile time) never reaches a filesystem glob:

- `result.final_state: done | error` — the lifecycle is over; the clear is
  allowed and the enforcement window closes from inside the session.
- `pending` or `paused` (or no matching record at all) — the clear is
  denied: an open task keeps its envelope, and a checkpoint-paused task
  re-authorizes via `oacp envelope compile --extend` after human review,
  never by clearing its own constraints.
- The validation judges the same effective target the CLI will use: the
  clear must be a **standalone simple command** (a compound command's
  earlier segments — `export …;`, `cd …;` — could retarget the clear after
  validation, so any compound form escalates), flag values use
  last-occurrence (argparse) semantics, and relative `--oacp-dir` resolves
  against the call's working directory. A clear that targets a different
  project, receiver, or OACP home than the active envelope, carries an
  `OACP_HOME=` override on the command, or trips over an unreadable audit
  record cannot be validated in-session and escalates to **ask**.

The ordering this creates is deliberate: finish the task, update the audit
record's `result` block (a bookkeeping write, exempt from the counter),
then clear. `oacp envelope compile` from inside the session stays denied
unconditionally — completion sanctions the exit, never recompilation.

### Envelope drift

A tool call that would exceed `expected_files_touched` is denied with the
canonical checkpoint opener (`Blocked: autonomy threshold exceeded —
files_touched expected N, now M`), which forces the Threshold-Exceeded
Checkpoint protocol above: the deny fires once, the session stops, notifies
the sender, and awaits re-authorization. A revised profile is recompiled
with `oacp envelope compile --extend`, which preserves accumulated counters.

### Enforcement recording

The audit `result` block records `envelope_enforcement: hooks | none`.
Receivers set `hooks` after a successful compile on an adapter-equipped
runtime; `none` means pickup-gate-only enforcement (no adapter for the
runtime yet). Degradation must never be silent.

Enforcement boundary: hooks constrain every tool call inside the session,
including subagent tool calls, and fire before sandbox/permission
evaluation. They do not constrain the human operator, and a runtime with the
shim stripped from settings is unenforced — provisioning integrity is an ops
concern, verifiable via the settings entry. Envelope discovery is scoped to
tool calls whose working directory resolves into the project workspace;
session-keyed multi-envelope concurrency and non-Claude adapters are v0.4
extensions. The compilation contract is pinned by the executable fixtures
under `tests/conformance/envelope/`.

## Continuation Grants

`continuation_grants` are default-off. Receivers ignore grants unless their
config explicitly enables them:

```yaml
autonomy:
  continuation_grants:
    enabled: true
```

The supported request kind is `approved_thread_continuation` under
`task_profile.continuation_grants`:

```yaml
task_profile:
  estimated_minutes: 20
  expected_files_touched: 1
  external_side_effects: true
  creates_or_updates_pr: true
  comments_on_github: true
  commits_changes: true
  continuation_grants:
    approved_thread_continuation:
      scope:
        max_actual_minutes: 30
        max_actual_files_touched: 3
        creates_or_updates_pr: true
        comments_on_github: true
        commits_changes: true
```

A sender-declared block is a grant request, not proof of approval. A standing
grant may be honored only when:

- receiver config enables continuation grants;
- the message has same-thread evidence via `parent_message_id` or
  `conversation_id`;
- a prior schema-v2 audit in that thread records a human task decision of
  `approved` or `modified` plus a grant decision of `approved` or `modified`;
- the follow-up sender matches the sender recorded by that prior audit;
- the current declared minutes, files, and side-effect classes stay inside the
  prior audit's `granted_scope`;
- actual work stays inside that same granted scope at the checkpoint.

The most recent explicit grant decision in the matching thread is
authoritative. A later denial revokes the standing grant for subsequent
follow-ups. A self-declared grant with no prior human approval pauses with
`continuation_grant_missing_approval`. A follow-up whose declared scope exceeds
the prior grant pauses with `continuation_grant_scope_exceeded`; actual drift
after acceptance pauses with `threshold_checkpoint_breached`. If the feature is
disabled, receivers log `continuation_grant_ignored_disabled` and evaluate the
message under normal policy.

`parent_message_id` is sender-declared protocol metadata. For compatibility,
an immediate-parent match remains acceptable same-thread evidence when a
shared `conversation_id` is unavailable or does not match. Sender binding
prevents cross-agent reuse, but until authenticated message/thread identity is
added (a banked message-signing proposal), same-sender parent-ID reuse is an
accepted residual
trust boundary; it does not bypass hard stops or the grant's declared/actual
scope checks.

## Taxonomy Pin

Audit `result.final_state` is limited to:

- `done`
- `paused`
- `blocked`
- `superseded`
- `error`

`result.completion_kind` is separately pinned to the evaluation-shape enum
above (see "Pinned completion_kind taxonomy"). Missing-profile messages that
obviously request PR/GitHub/commit/push/public work pause with
`risk_obvious_no_profile`, not the generic `task_profile_missing`.

The canonical fixture set lives in `tests/conformance/autonomy/`.
