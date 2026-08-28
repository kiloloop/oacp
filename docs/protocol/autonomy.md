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

When config is absent, receivers behave as `always_pause`. A present config
that omits the `autonomy` block entirely, such as a signing-only config,
resolves the same way: `always_pause`, not `config_malformed`. An explicit but
malformed `autonomy` value still causes a pause and should be surfaced by
`oacp doctor`.

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

`expected_files_touched` counts distinct deliverable files only: files created
or modified as part of the requested outcome or a review-required correction.
Exclude receiver-local inbox, outbox, audit, memory, cache, and scratch writes
used for protocol bookkeeping or temporary work. For example, a change to one
protocol document, one template, and one test declares `3` even when the
receiver also writes an audit record, reply body, and cache entries; a
reply-only analysis that uses scratch files but produces no file deliverable
declares `0`.

### Default scope envelope (profileless admissions)

`allow_without_task_profile` is an **admission-only exemption**: the sender
needn't author a profile, but the bound always exists — a grant removes
friction, never the bound. A profileless admitted request receives the
documented default envelope instead of running unbounded:

- `estimated_minutes: 25`, `expected_files_touched: 2`, reply-only
  (`sends_oacp_reply_only: true`); every other capability and risk flag
  `false`; `target_repo` empty; no continuation grants.
- `risk_tier` mirrors the message's own declared `priority` when it is a
  valid tier (`P0`–`P3`) — it is the sender's severity claim — else `P2`.

The default envelope binds the post-accept threshold checkpoint exactly
like a declared one: a genuinely long profileless task may
checkpoint-pause against it — that is the bound existing, by design, not a
regression. A sender that legitimately needs more attaches a **voluntary
`task_profile` on the exempt type** — a documented, supported path; the
profile's envelope then replaces the default entirely (and is the only way
an exempt type reaches continuation-grant evaluation, since a default
envelope declares nothing).

Every decision names its envelope's origin in `scope_envelope_source`
(`task_profile` or `default_profileless`; null only on a pause taken
before envelope construction). No admitted message type is
envelope-exempt — the exempt set is empty — and `scope_envelope: null` on
an admitted record is a **schema violation**: the audit writer refuses to
persist it rather than recording an unbounded admission.

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
records every matching span in `matched_patterns` and logs the term classes as
`lexical_advisory`; fenced text is never invisible to the audit. Destructive
commands, direct main pushes, credential
rotation, dependency installation, public-repository text, memory SSOT text,
and pricing/commercial content are scanned across the raw body and remain hard
even inside the fence (the reply-only carve-out for pricing/commercial content,
under Gate 3, keys on the declared profile shape, not on fencing). An unclosed
or differently labeled fence is not skipped.

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
   - Evaluates every envelope-derived axis before any early return and
     records the result as the admission ledger (see "Admission ledger"
     under Audit Events); the verdict is still the first failing axis.
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
     demotable matches in clauses headed by `no`, `not`, `never`, `do not`,
     `does not`, `don't`, `doesn't`, `out of scope`, `exclude`/`excluded`,
     `excludes`, `avoid`, `refrain from`, `prohibited`, `forbidden`, `skip`, or
     `without`. A classic negation or scope-oriented form in a Markdown heading
     or colon-terminated heading also scopes over the following block;
     `skip` and `without` remain same-clause only because their ordinary prose
     uses are ambiguous. Heading scope ends at the first blank line or next
     heading, so the first governed line must follow the heading directly.
     Direct-main pushes, credential rotation, memory-SSOT scope, and
     pricing/commercial content remain hard even when they appear in such a
     clause or block. Dependency-install and public-repository matches receive
     this context handling only for the proven negated/out-of-scope forms;
     affirmative forms remain hard.
   - With a complete profile, demote side-effect or sensitive-scope lexical
     matches to a logged `lexical_advisory` when the corresponding declaration
     is `false`. Missing/unparsable profiles and contradictory declarations do
     not receive this demotion.
   - When policy explicitly uses `external_side_effects: allow`, declared
     ordinary external side-effect verbs are also advisory; non-demotable hard
     stops remain hard.
   - Treat the repository-setting phrase `merge method` and descriptive
     dependency-introspection wording such as `what pip install pulls` as
     reference-only advisories. This is a narrow contextual classification,
     not removal or weakening of the merge or dependency-install classes;
     affirmative action wording follows the existing hard-stop or granular
     declaration path.
   - Pause when the body touches declared auth/config/secrets/credentials or
     public-repository scope, or any memory SSOT scope.
   - Keep `commercial`, `pricing`, affirmative public-repository text, and
     memory SSOT text hard. Pricing/commercial matches receive no fence or
     negation demotion and are reported separately as
     `hard_stop_content_sensitivity` rather than action risk.
   - Reply-only carve-out for pricing/commercial content. The category was
     ruled always-hard — "`commercial`/`pricing` pauses stay HARD, relabeled a
     *content-sensitivity* category (out of the action-risk FP stats)" — and
     that ruling is amended for exactly one profile shape: a complete task
     profile that declares `sends_oacp_reply_only: true` **and** every other
     side-effect flag false (`external_side_effects`, `creates_or_updates_pr`,
     `comments_on_github`, `commits_changes`, `merges_pr`, `files_issues`).
     For that shape a pricing/commercial match does not pause: every matching
     term is recorded as a `lexical_advisory_reply_only` note carrying the
     term and as structured span provenance whose `demotion_basis` is
     `reply_only_advisory`, recorded ahead of the other Gate-3 classes so the
     advisory survives whichever hard stop or axis governs the verdict, and
     the verdict comes from the remaining axes. Any
     other shape keeps the hard stop: a side-effect flag `true`, a profile that
     omits `sends_oacp_reply_only`, a missing, unparsable, or contradictory
     profile, and the profileless default envelope (reply-only by bound, but it
     declares nothing). Fencing and negation still never demote the category.
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
affirmative dependency installation, memory SSOT scope, and
pricing/commercial content —
the last with the single declared reply-only profile-shape carve-out described
under Gate 3, where it records as an advisory instead.
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
spec_version: "0.4.3"
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
admission_axes:
  evaluated: true
  thresholds: []
  declared_risk: []
  declaration: []
  side_effects: []
  continuation_grant: []
  declaration_errors: []
matched_patterns: []
runtime:
  agent: codex
  model: gpt-5            # serving model, normalized at the writer; null only with a reason
  model_source: "env:OACP_RUNTIME_MODEL"
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
  work_started_at_utc: null
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
    breach_sub_basis: null
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
parsed policy (excluding any `auth` trailer key), so comments, YAML
formatting, and signing state do not produce false drift. Records written
through the gate CLI additionally carry a `policy_auth` block —
`{status: unsigned | verified | invalid | unsupported, signer_agent,
signer_kid, reason}` — the policy-file authorization outcome (see
`message_signing.md` → "Policy-file signing"): together with
`policy_sha256` the record commits to an *authorized* policy identity,
not just which bytes ran. An `invalid` status fails closed with reason
code `policy_auth_invalid` before any gate consumes the config.
`spec_version: "0.4.3"` preserves every autonomy field and rule pinned by
0.4.2 — Gate 1 integrity
enforcement, the recalibrated Gate 2/3 policy, full task-profile capture, the
explicit `breached` list, the outcome block shown above, session-scoped
envelope enforcement, the enforce-mode trust-pin completeness gate, and
preserved `always_pause` defaults for configs without an `autonomy` block —
plus intake verification as mechanism (`verify_mode: enforce` rejects at
the gate), the `policy_auth` authorized-policy block, the default scope
envelope for profileless admissions (with `scope_envelope_source` and the
null-on-admitted writer refusal), and the recorded none-by-rule
enforcement branch for approved public-visibility tasks. The spec version
advances because 0.4.3 adds the project-wide message-retention policy and the
processed-inbound archive convention; autonomy semantics are unchanged. Audit
`schema_version: 2` adds thread identity and
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

`runtime.model` is resolved **at the writer and never backfilled**. The
serving model resolves caller-first (an explicit `runtime.model` already on
the decision), then from the `OACP_RUNTIME_MODEL` environment variable the
invoking session exports — the variable names the model actually serving
that session, not the model a configuration requested. Values are
normalized at write time: case variants fold to lowercase, and a
`[context]` suffix (same weights, different serving context window) splits
into the base id plus a separate `model_context` field so per-model
grouping never divides one model across suffix variants. `model_source`
names the provenance (`caller` or `env:OACP_RUNTIME_MODEL`) and
`model_raw` preserves any input the normalization changed. A record with
no signal carries an explicit unknown — `model: null` plus a
`model_unknown_reason` — never a silent null. The *requested* model
(harness configuration, settings files) is deliberately never consulted,
and historical records are never rewritten: a request can be silently
served by a different model, alias, or context variant, and filling the
field from it would reintroduce exactly the confound the field exists to
remove.

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

### Lexical provenance

`matched_patterns` is a source-ordered list of every autonomy lexical match,
including the profileless-risk vocabulary and every Gate-3 class, not only the
match that drove the verdict. Each entry has this additive shape:

```yaml
matched_patterns:
  - pattern: merge
    category: side_effect
    span: {start: 48, end: 53}
    demotion_basis: reference_only
```

`span` is a zero-based, end-exclusive Unicode-code-point range in the original
message body. `demotion_basis` records why the hit was demoted or why it stayed
operative: `negated`, `reference_only`, `guardrails_fence`, `profile_false`,
`profile_true`, `policy_allowed`, `reply_only_advisory`, `affirmative`,
`profileless_type`, `profileless_risk`, or `non_demotable`.
Contextual demotion is occurrence-scoped: negated or descriptive wording that
demotes one match does not demote a later affirmative match in the same clause,
and a match that spans a clause boundary remains non-demotable. A later
dependency-install or public-repository occurrence requires its own negation
after the preceding class cue; the evaluator does not infer scope from a list
of contrast words. Dependency-install matching also stops at another install
verb, and an otherwise demotable match containing its own negation stays hard
as ambiguous. The first occurrence is demoted only when a narrow recognized
negation form demonstrably governs that occurrence (including the proven
direct dependency-install, direct public-repository action, and out-of-scope
public-repository forms); an unrelated negation earlier in the clause is not a
basis, and unrecognized governance fails closed. A recognized governance form
also fails closed when another negation term precedes its proving term in the
same governance prefix; this deliberately keeps both inverting and reinforcing
stacked-negation constructions hard because their polarity is ambiguous.
Pricing/commercial matches therefore
distinguish the declared reply-only advisory shape from the otherwise
non-demotable content-sensitivity class without changing the class policy.

The legacy singular `matched_pattern` remains on a paused decision and still
names the first blocking match. `logged_notes` remains the compatibility
surface for advisory reason codes. New readers use `matched_patterns` for
complete forensic evidence and must not infer that a missing singular field
means no lexical term matched.

### Admission ledger

`admission_axes` is the structured record of every envelope-derived
admission axis, evaluated in full before any early return — the Gate-3
lexical returns and every Gate-2 return alike — so a pause taken for one
reason never leaves another axis unrecorded. Each axis lists the pinned
reason codes that held (empty means passed), in evaluation order:

| Axis | Reason codes |
|---|---|
| `thresholds` | `estimated_minutes_exceeds_threshold`, `expected_files_touched_exceeds_threshold` |
| `declared_risk` | `destructive_ops_pause`, `auth_config_or_secrets_pause`, `dependency_changes_pause`, `public_visibility_pause` |
| `declaration` | `declaration_error` |
| `side_effects` | `merges_pr_pause`, `external_side_effects_not_pr_artifact`, `external_side_effects_pause`, and the granular `<capability>_pause` codes |
| `continuation_grant` | the `continuation_grant_*` codes, including `continuation_grant_scope_exceeded` |

The ledger is evidence, never verdict: `reason_codes` keep the pinned
first-failure shape, and every other axis the ledger holds surfaces
through `co_occurring_reason_codes`, which is therefore complete for the
envelope-derived axes — silence there means passed. `evaluated: false`
with every axis `null` marks a pause taken before a scope envelope
existed (malformed config, `always_pause` mode, message-integrity
failures, a missing or unparsable profile, review-lifecycle admissions):
nothing was evaluated, and the record says so instead of reading as "all
passed". Lexical classification is body-derived: `matched_patterns` records
every hit, while the compatibility field `matched_pattern` names the first
blocking hit. Neither is a ledger axis.

`declaration_errors` carries the cause of an admission-time
`declaration_error`: the contradicted field, its declared value, and the
declared fields it conflicts with — for example
`task_profile.external_side_effects` declared `false` against
`task_profile.commits_changes`. The field paths also land in `breached`,
as before.

Admission and checkpoint evidence never overwrite each other. The ledger
and `co_occurring_reason_codes` describe admission only and are left
untouched by every later write; checkpoint outcomes live in
`result.threshold_checkpoint`. A receiver-side checkpoint update that
replaces `reason_codes` with a checkpoint reason discards the admission
history — the finalizer (`oacp autonomy-finalize --checkpoint`) is the
write path precisely because it leaves the admission fields alone.

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
non-conforming, and the audit writer enforces the pin at write time: a
decision whose `result.completion_kind` is missing or outside the enum is
refused rather than persisted. Records written before this pin carry mixed
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

### Terminal finalization and audit integrity

Every receiver-side write to an audit record after admission goes through
one lock-aware code path:

```bash
# mid-task §E checkpoint (same points on every receiver)
oacp autonomy-finalize <audit.yaml> --checkpoint --actuals <actuals.yaml>

# terminal update
oacp autonomy-finalize <audit.yaml> --final-state done \
  --actual-minutes 30 --actual-files-touched 3 \
  --realized creates_or_updates_pr --realized commits_changes \
  --reply-message-id <msg-id>
```

Hand-edited terminal blocks are what produced off-enum vocabulary,
duplicate live evaluations, and terminal records still carrying a paused
checkpoint action; the finalizer enforces the pinned enums and
cross-field invariants at write time, under the shared audit lock.

**Run-state vocabulary.** `result.final_state` splits by lifecycle phase:
`pending`, `paused`, and `blocked` are live states; `done`, `superseded`,
and `error` are terminal. Only terminal states can be finalized.
`pending` is receiver-written (an admission record picked up for
execution) — the evaluator itself never writes it.

**Invariants (terminal ⇒ not paused).** Finalizing `done` requires: a
recorded human outcome when the admission decision was `paused`; a
resolved checkpoint (a `resumed` re-authorization disposition, or a
post-pause human outcome) when the record's checkpoint breached; and no
live sibling evaluation of the same logical message. A still-paused
checkpoint `action` reconciles to `resumed_after_reauthorization` at
finalization once its re-authorization resolved — but an answered pause
covers only what was paused and granted: every final axis is still
compared against the resolved scope (the envelope, an accepted grant,
the recorded re-authorization scope, and the answered pause's own
realized effects), and any new expansion — an uncovered realized effect,
a numeric beyond a scoped re-authorization budget, or a numeric beyond
the pause-time extent a scope-less approval cleared — refuses terminal
reconciliation and routes through a fresh `--checkpoint` with
re-authorization input. Realized effects are monotonic evidence: a
`true` checkpoint value carries forward when terminal actuals omit it,
and an explicit terminal `false` against a recorded `true` is refused as
contradictory under-reporting. A mid-task `--checkpoint` refuses a closed
record outright (completion evidence present, or superseded): a
checkpoint never reopens terminal state. `completion_kind` is never
receiver-composed: a record whose kind or state is off-enum is refused
for `done`/`error` — it is closed via supersession, never edited in
place.

**Terminal checkpoint parity.** Finalizing `done` on an envelope-bearing
record always evaluates the §E threshold checkpoint against the final
actuals, whichever receiver writes it. Within the envelope, the
checkpoint records `within_declared_envelope` (or
`continued_with_grant`) and the terminal write proceeds; a breach leaves
the record checkpoint-paused (exit code 4) and the §E re-authorization
flow applies before any terminal state can be written. Realized effects
use the canonical axis names only: `actual_minutes`,
`actual_files_touched`, `side_effects_actual.<capability>`, and
`task_profile.<field>` for declared-intent corrections.

**Evaluation identity and supersession.** Every persisted evaluation
carries an `evaluation_id` derived from receiver, message id, message
bytes, and evaluation time. Re-evaluating a message the evaluator
already recorded adopts the existing record when nothing changed (same
bytes, same policy, same verdict — no duplicate is written) and
supersedes it otherwise: the new record references its predecessor via
`supersedes_evaluation_id`, and the predecessor closes with
`final_state: superseded` plus `superseded_by_evaluation_id` — a state
update, never a rewrite of what it said. The prior scan, the
adopt-or-write choice, and predecessor supersession run as one
transaction under a stable lock keyed by (receiver, message id), so
concurrent evaluations of the same logical message serialize instead of
both staying live; adoption also self-heals a crashed predecessor
transaction by superseding any other live prior it finds, stamping the
adopted survivor's `evaluation_id` and `superseded_evaluation_ids` so
every healed predecessor stays resolvable. When one successor closes
several live priors, it lists all of them in
`superseded_evaluation_ids` alongside the single-valued
`supersedes_evaluation_id` (the newest). The transaction fails closed
and is rollback-capable for every failure class, validation and
filesystem alike: every live predecessor is strict-load preflighted
before anything is written, so ambiguous predecessor evidence
(duplicate-key YAML) fails the whole evaluation with nothing persisted
and the malformed bytes untouched; any later failure restores every
already-mutated predecessor byte-for-byte (read-back verified) and
removes a just-written successor before raising — never a partial
success that leaves two live evaluations or a predecessor linked to a
successor that no longer exists. Every superseded record must name its
successor, and resolution is strict: `--superseded-by` is mandatory on
the supersession transition and must resolve to exactly one other
evaluation in the same audit directory carrying the same receiver and
message id and referencing the predecessor back through
`supersedes_evaluation_id` or `superseded_evaluation_ids` — a record
merely carrying the id (unrelated identity, ambiguous holders, no
back-reference) is refused, only strict duplicate-key-refusing bytes
serve as successor evidence, and the sweep flags dangling,
self-referential, ambiguous, or unrelated-identity successor chains.
The transaction pre-acquires every affected record's per-file audit
lock in deterministic order and holds them across capture, mutation,
and restore — the newly created successor included, its lock entered
before publication and held until the transaction commits or rolls
back — so conforming per-record writers (human-outcome recording,
`message_auth` attachment) serialize with it rather than losing a
committed update to its rollback.
The supersession transition is also the documented
repair for legacy off-enum records — it bypasses the enum guards,
preserves the off-enum `completion_kind`, and stashes an off-enum run
state as `result.legacy_final_state` before overwriting it. Superseded
records are transparent to envelope-clear validation, excluded from live
duplicate detection, and skipped by the off-enum sweep checks (their
history is closed); exactly one evaluation per logical message stays
live.

**Legacy vocabulary.** Records written before this rail carry off-enum
values; the canonical mapping is documentation for collectors, not a
rewrite instruction — `executed` and `review_round_delivered` map to
`auto_accepted`, `human_approved_completed` and
`completed_after_human_approval` to `admission_paused`, and
`final_state: completed` to `done`. `superseded` itself is pinned, not
legacy.

**Validation.** `oacp autonomy-finalize <record> --validate [--sweep]`
reports pinned finding codes (`off_enum_completion_kind`,
`off_enum_final_state`, `duplicate_logical_id`, `duplicate_yaml_key`,
`paused_terminal_checkpoint_action`, `paused_terminal_completed`,
`terminal_paused_without_outcome`, `superseded_missing_successor`,
`breached_empty_fields`, `invalid_human_outcome`,
`decision_kind_incoherent`, `off_enum_breach_basis`,
`breach_basis_incoherent`, `record_unparsable`; advisory:
`noncanonical_checkpoint_axis`, `terminal_missing_actuals`), and
`oacp doctor` sweeps every receiver's audit directory with the same
checks. Duplicate YAML keys are refused
outright — plain YAML loading silently keeps the later value. The
fixture set lives in `tests/conformance/autonomy/records/`.

### Pinned reason-code taxonomy

Evaluator implementations must reject unpinned reason codes. The canonical
families are:

- integrity/config: `config_malformed`, `policy_auth_invalid` (the
  receiver config carries a policy signature that fails verification —
  the gate refuses to evaluate a tampered policy, distinguishably from a
  merely malformed or absent one), `mode_always_pause`,
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
  `checkpoint_reauthorized`, `checkpoint_reauthorization_stale`,
  `message_valid`, `message_not_expired`, `message_hash_recorded`,
  `task_profile_present`, `task_profile_not_required`, `task_type_allowed`,
  `risk_threshold_passed`, `hard_stops_clear`, and
  `workspace_check_required`;
- review-loop continuation codes (see "Review-loop continuation"):
  `review_continuation_accepted`,
  `review_continuation_confirmation_required`,
  `review_continuation_context_only`, `review_continuation_expired`,
  `review_continuation_head_mismatch`,
  `review_continuation_ignored_disabled`,
  `review_continuation_missing_approval`, `review_continuation_revoked`,
  `review_continuation_round_exceeded`,
  `review_continuation_scope_exceeded`, and `review_loop_invalid`.

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
- Audit event includes legacy `matched_pattern: "rm -rf"` plus a
  `matched_patterns` entry with its exact source span and
  `demotion_basis: non_demotable`.

The receiver must pause before any action runs. No autonomy mode can override
the hard stop.

## Threshold-Exceeded Checkpoint

`result.work_started_at_utc` is the receiver-stamped time of its first action
on that task. `actual_minutes` is active wall-clock time from that stamp to
completion or the current checkpoint, rounded up to a whole minute, excluding
each represented re-authorization pause from `paused_at_utc` through
`cleared_paused_at_utc`. If a terminal outcome leaves that pause uncleared,
active work ends at `paused_at_utc`; the subsequent paused wait does not count.
The current record shape represents at most its current pause interval.
Admission-to-start idle time never counts; time waiting on a peer reviewer does
count because it is part of that task's review loop. Serialized tasks admitted
together therefore stamp and measure their own starts independently.

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
    breach_sub_basis: null
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
- `breach_sub_basis` — optional refinement of a `realized` breach on the
  time axis (`actual_minutes` in `breached_fields`). The only pinned
  value is `waiting_on_peer`: the clock overran while the receiver was
  waiting on a peer reply (a review round, a re-authorization answer)
  rather than working, so the ledger can separate review latency from
  working time when calibrating the time axis. Receivers pass it in the
  checkpoint actuals (`actuals.breach_sub_basis`); it is `null` on every
  other checkpoint. An off-vocabulary value, or the value on an
  unbreached checkpoint, on a `declared_intent` breach, or on a breach
  that does not include the time axis, is rejected rather than recorded —
  and the same grammar is enforced on read-back: the audit validator
  (`oacp autonomy-finalize --validate`, `oacp doctor`, and the finalizer's
  own residual check) reports `off_enum_breach_basis` /
  `breach_basis_incoherent` on a persisted `breach_basis` or
  `breach_sub_basis` that the evaluator would have refused — an
  off-vocabulary value, a basis on an unbreached checkpoint, a sub-basis
  off the realized time axis, or an enum-valid basis on the opposite
  breach source (`declared_intent` over realized axes, realized effects,
  or materialized risk; `realized` over `task_profile.*` corrections) —
  so stored ledger evidence cannot drift off the grammar by hand-editing.

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

### Re-authorization channels and arbitration

A paused checkpoint can be answered from more than one channel. Three are
defined, highest precedence first:

| Channel | Surface | Authority |
|---------|---------|-----------|
| `receiver_human` | The receiver-side human decision recorded on the audit record (`oacp autonomy-outcome`) | Authoritative |
| `sender_reply` | A signature-verified sender message threaded to the checkpoint notification | Bounded by the receiver's admission policy |
| `gh_comment` | A comment on the related PR or issue | Advisory only — never authoritative |

Consultation order follows precedence: the receiver consults its own audit
record first, then the checkpoint thread in its inbox, then the related
PR/issue. Provenance verification for each channel — the signature and
thread binding of a sender reply, the recorded outcome on the audit record —
happens at the receiver before an answer may be presented for arbitration;
the arbitration input is a decision surface, not a trust boundary.

Arbitration rules:

- The **governing answer** is the one on the highest-precedence channel that
  carries an answer. Precedence is by channel rank, never arrival order: a
  lower-ranked answer arriving after a higher-ranked ruling is recorded as
  advisory and never re-opens the decision — in both directions. A
  receiver-side human approval stands against a later sender decline, and a
  receiver-side human decline stands against any sender approval.
- **Sender authority is bounded by the receiver's own admission policy.** A
  sender re-authorization can never authorize more than the receiver's
  policy would have auto-accepted at admission: numeric extensions are
  honored only up to the receiver's caps, and a boundary action is granted
  only when an envelope declaring that capability would itself auto-accept
  under the receiver's external-side-effect policy — for
  `allow_pr_artifacts`, an artifact-class anchor (`creates_or_updates_pr`,
  `comments_on_github`, or `files_issues`) on a private target present in
  the receiver's `private_repo_allowlist`; a standalone `commits_changes`
  or an unlisted/public target stays paused. Merge authority is never
  sender-grantable, whatever the policy (merge always passes the
  receiver-side human, mirroring the admission rule), and neither is any
  field outside the coverable boundary-action vocabulary: the legacy risk
  fields (destructive ops, auth/config/secrets, dependency changes, public
  visibility) are receiver-side authority only, on the scoped and
  scope-less paths alike. The party whose under-declaration caused the
  breach cannot self-serve unlimited scope. Without a resolvable receiver
  policy the sender channel extends nothing (fail closed).
- **GH comments never clear a checkpoint.** They sit outside the protocol's
  identity and verification boundary; any decision they carry is recorded as
  advisory, and a checkpoint answered only by a GH comment stays paused.
- A sender withdrawal of the task is a lifecycle event (`superseded`), not a
  checkpoint answer; it is not arbitrated here.

The receiver presents the answers it has observed as checkpoint input under
`actuals.reauthorization`, and the evaluation records the arbitration under
`threshold_checkpoint.reauthorization`:

```yaml
actuals:
  actual_minutes: 25
  actual_files_touched: 1
  paused_at_utc: "2026-05-12T12:30:00Z"
  reauthorization:
    receiver_human:
      decision: approved            # approved | modified | declined
      decided_at_utc: "2026-05-12T12:50:00Z"
      actor: alice
      scope:                        # optional: budget extension and/or
        max_actual_minutes: 30      # boundary-action grants
    sender_reply:
      decision: approved
      decided_at_utc: "2026-05-12T12:45:00Z"
      source_message_id: msg-20260512124500-sender-reauth1
    gh_comment:
      decision: approved            # advisory only; cannot carry scope
      author: driveby-collaborator
```

```yaml
threshold_checkpoint:
  action: resumed_after_reauthorization
  reauthorization:
    presented: true
    channel: receiver_human         # governing channel
    decision: approved
    decided_at_utc: "2026-05-12T12:50:00Z"
    actor: alice
    requested_scope:                # the raw request, kept as provenance
      max_actual_minutes: 30
    scope:                          # the effective grant — policy-capped;
      max_actual_minutes: 30        # the durable value later checkpoints
    disposition: resumed            # and envelope recompiles consume
    cleared_paused_at_utc: "2026-05-12T12:50:00Z"
    advisory:
      - channel: sender_reply
        decision: approved
        reason: overridden_by_receiver_human
      - channel: gh_comment
        decision: approved
        reason: never_authoritative
```

`requested_scope` preserves the answer exactly as asked; `scope` records
what was actually granted after channel bounds — sender numerics capped at
the receiver's thresholds, sender booleans filtered to the admission
predicate. The two diverge exactly when a sender asked past the receiver's
policy, and only `scope` may ever be consumed downstream.

`disposition` is pinned to: `resumed` (breach cleared — the accept records
`checkpoint_reauthorized` and the action `resumed_after_reauthorization`),
`declined` (governing channel declined — action `reauthorization_declined`),
`stale` (the reuse rejection below — adds `checkpoint_reauthorization_stale`),
`insufficient` (a current answer that does not cover the breach),
`advisory_only` (only non-authoritative answers exist), `unanswered`
(breach with no input), and `not_required` (answers presented but nothing
breached). `modified` behaves as approved with a replacement scope and
requires an explicit `scope`, matching the outcome recorder.

### Consumption

What one answer clears is pinned by its scope shape:

- **A scope-less approval** authorizes exactly the extent recorded at the
  pause it answers (`decided_at_utc` at or after that pause's
  `paused_at_utc`) — the numeric actuals and the boundary fields of that
  pause alike, within the answering channel's grant bounds. It creates
  nothing durable (`scope` stays null) and is consumed by that checkpoint:
  presented against any later pause, it clears nothing.
- **A scoped numeric budget** authorizes up to the granted budget for the
  remainder of the task. A later checkpoint whose actuals stay within the
  budget is cleared by the same standing answer — nothing new is consumed —
  while growth beyond the budget is a new breach requiring a fresh answer.
- **A boundary-action grant** (scope boolean) is durable for the remainder
  of the task; see below.
- **No answer shape clears the whole task unbounded.** Nothing waives future
  checkpoints wholesale.

An answer that neither postdates the current pause nor covers the breach
through a standing granted scope is spent. Presenting it against a new
breach is a reuse attempt and fails with `checkpoint_reauthorization_stale`.

Consumption state lives on the audit record: the
`threshold_checkpoint.reauthorization` block records when the governing
answer cleared the current `paused_at_utc` interval in
`cleared_paused_at_utc` and preserves the granted scope for later checkpoints.
A new checkpoint re-stamps `paused_at_utc`; whether prior answers still cover
it is decided by the arbitration above, never by implicit trust.

### Boundary-action grants

A re-authorization may be scoped to a specific boundary action instead of —
or in addition to — numeric budgets: a scope boolean naming one declarable
granular capability (`creates_or_updates_pr`, `comments_on_github`,
`commits_changes`, `merges_pr`, `files_issues`).

- A granted boundary action behaves as if declared true for the remainder of
  the task: the same action class does not re-fire the checkpoint, and an
  envelope recompile picks it up. It is not consumed per use; it expires at
  task completion.
- Numeric budgets stay per-checkpoint as above: granting an action never
  extends time or file budgets, and vice versa.
- Channel bounds apply. The receiver-side human may grant any action; a
  sender re-authorization may grant only an action the receiver's admission
  policy would itself auto-accept (the exact `allow_pr_artifacts`
  conditions above — never a standalone `commits_changes`, never an
  unlisted or public target), and never `merges_pr`.
- Standing authority across tasks is out of scope for a boundary-action
  grant — that is the continuation-grants mechanism (see Continuation
  Grants), which resolves only from prior human-approved audits in the same
  thread.

## Envelope Compilation (Phase 2)

Phase 2 turns the declared `task_profile` from reviewed intent into enforced
runtime constraints. After a message is admitted (auto-accepted, or paused
and then human-approved), the receiver compiles the profile plus its own
autonomy config into a runtime envelope:

```
oacp envelope compile <message.yaml> --receiver <agent>
```

**Admitted public-visibility tasks are the one explicit exception.** A
compiled `public_visibility: true` envelope denies the entire chain the
human just approved — the runtime adapter has no post-approval carve-out —
so for a public task whose admission audit records a human outcome of
`approved`/`modified`, the receiver passes that record to the compiler:

```
oacp envelope compile <message.yaml> --receiver <agent> \
  --audit <admission-record.yaml>
```

and the compiler takes the **none-by-rule branch**: it does **not**
compile, and it stamps `result.envelope_enforcement: none` plus
`result.envelope_enforcement_reason: public_visibility_admission_approved`
into the audit record under the audit lock. Degradation is the documented
mode, not silence — the human admission approval plus live supervision is
the named control, and the record says so.

The approval record is authorization, so eligibility is strict: the
`--audit` path must resolve inside the receiver's canonical admission
audit directory (`agents/<receiver>/audit/autonomy_decisions/` — an
arbitrary readable YAML never qualifies), and the record must be an
admission-**paused** record carrying a `schema_version`, content-matched
on `message_id` + `receiver` (in the record itself, never the filename),
bound to the exact verified message snapshot via `message_sha256`, with
a recorded human outcome of `approved` or `modified`. Eligibility and
the marker write consume ONE locked read of the record. A none-by-rule
result must also MEAN none: the branch holds the envelope lock, fails
closed if any envelope is active for the receiver (its normal lifecycle
clears it — never a silent delete, never a false `none`), and consumes
the compiling session's pending claim so a deliberate no-envelope
success cannot bind a later, unrelated compile.

This is a deliberate, recorded exception to "a grant removes friction,
never the bound": for approved public work the **human is the bound**,
and the exception retires when a post-approval envelope path (compile
consuming the recorded approval into a workable public envelope) ships.
Private tasks, and public tasks without a matching eligible record, are
entirely unaffected: the fail-closed compile below still runs, and an
unapproved public envelope still denies at the hook.

The envelope is written to
`agents/<receiver>/state/active_envelope.json`:

```json
{
  "envelope_version": 1,
  "spec_version": "0.4.3",
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
  "enforcement": "hooks",
  "session_id": "sess-…"
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
- **Session binding.** The envelope records which harness session it
  belongs to. The runtime hook — the only party that sees the harness
  session id (it is not present in the command environment) — records a
  short-lived session claim when it observes the compile command; the
  compiler consumes the claim (matching it against the message file it
  actually compiles, within a freshness window) and stamps `session_id`.
  A recompile for the same task — including a post-re-authorization
  `--extend` run from outside the bound session — inherits the existing
  binding. When no valid claim exists (an un-hooked runtime compiled, or
  the harness supplied no session id) the envelope compiles unbound
  (`session_id: null`) and enforcement keeps the historical
  (project, agent) scope for every session.

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

Enforcement is **session-scoped** when the envelope carries a session
binding: a tool call from a different harness session gets pre-envelope
behavior — no classification, no `files_touched` accounting — so a
concurrent interactive session in the same repository neither inherits the
dispatched task's constraints nor consumes its file budget. The scope never
silently narrows: an unbound envelope enforces every session in the
(project, agent) workspace exactly as before, and a caller the harness gave
no session id is enforced even under a bound envelope (it cannot be proven
foreign). A foreign session still cannot mutate the shared envelope state;
only provably read-only inspection is exempt. The `oacp envelope show`
inspection grammar recognizes argparse-equivalent value options in both
`--option value` and `--option=value` forms, plus unambiguous long-option
abbreviations and help flags, while unknown options continue to fail closed.

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
- Every emitted **ask** or **deny** reason starts with the stable
  `[oacp-envelope]` source tag. When an active envelope supplies a message id,
  the reason also includes `[task <message-id>]`; operators can therefore
  distinguish an envelope decision from the harness's native permission
  prompt and identify the task that caused it.
- Compound Bash classification is quote-aware: separators inside quoted
  arguments remain argument data, backslash-newline continuations stay in the
  same segment, and top-level or command-substitution segments are still
  inspected independently so a read-only head cannot conceal a later
  mutation. If argv parsing degrades, only a small known read-only command set
  with no shell control, redirection, expansion, or substitution syntax can
  pass; everything else escalates to **ask**.
- `oacp send` is never denied; it is the checkpoint notification pipe. The
  exemption is exactly that wide: read-only oacp subcommands pass, all other
  oacp mutations are classified.
- Determinable Bash write targets feed the same distinct-file counter as
  Edit/Write calls (`/dev/*` excluded), so shell writes cannot bypass
  `expected_files_touched`.
- A `pyproject.toml` Edit or Write under `touches_dependencies: false` is
  exempt from the dependency-manifest denial only when the hook can
  reconstruct both complete documents and prove that every byte outside the
  sole `[project] version` string literal is unchanged. The file still counts
  toward `expected_files_touched`. Dependency, optional-dependency, and
  build-system changes remain denied, as do mixed edits, ambiguous or
  unsupported file-tool inputs, and every Bash-side manifest write whose
  resulting content cannot be proven before execution.
- Protocol bookkeeping never consumes the file budget. The receiver's own
  `audit/`, `inbox/`, and `outbox/` directories and the runtime scratchpad
  (reply/body-file composition) are the enforcement layer's instrumentation
  surfaces, not task scope: writes there skip the counter entirely.
  `audit/autonomy_decisions/` is the exception carved out of `audit/` — it
  is authority, not instrumentation (see below). The
  exemption is receiver-scoped (a peer agent's directories are task scope),
  containment is judged on resolved filesystem targets (a symlink planted
  under an exempt root that points into ordinary task scope stays counted),
  and it applies only to the counter — the secret-class and
  dependency-manifest gates still fire on exempt paths, and the receiver's
  `config.yaml` sits outside the exempt directories. Without this class, a
  tightly declared task is guaranteed to trip the ceiling at close-out on
  its own mandatory audit write.
- Two receiver surfaces are **authority**: the opposite of exempt, and
  governed by one boundary rather than two. The receiver's `state/`
  directory holds the active envelope itself; its
  `audit/autonomy_decisions/` directory holds the records whose recorded
  re-authorization can widen that envelope's live file bound. Writing,
  removing, relocating, or copying anything under either from inside the
  session is denied as authority self-modification, categorically —
  filesystem-mutator operands (`rm`, `unlink`, `mv`, `cp`, …) are gated by
  resolved path regardless of source/destination role, not just write
  targets, because deleting or relocating a record changes which record
  governs just as surely as rewriting one. Operands are judged as the
  utility parses them (GNU target-directory spellings and `--` included),
  and mutator operands or Bash write targets bearing shell expansion syntax
  escalate to **ask** — the shell expands patterns after classification, so
  a literal spelling proves nothing about the effective target. The
  canonical audit writers (`oacp autonomy-finalize`,
  `oacp autonomy-outcome`, `oacp verify --attach-audit`) reach the record
  as classified commands that escalate for review, never as file writes.
  The boundary is deliberately scoped to the session the envelope binds: it
  is the *self*-authorization path that must close, and a concurrent
  session is already outside that envelope's reach.
- Trust roots (the receiver's `trust/` pins and the project trust catalog)
  are authority-bearing auth configuration: writes and mutations are denied
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
record's `result` block through the canonical writer (`oacp
autonomy-finalize`; a direct write to the record is denied as authority
self-modification), then clear. `oacp envelope compile` from inside the
session stays denied unconditionally — completion sanctions the exit, never
recompilation.

### Envelope drift

A tool call that would exceed `expected_files_touched` is denied with the
canonical tagged checkpoint opener (`[oacp-envelope] Blocked: autonomy
threshold exceeded — files_touched expected N, now M`), followed by the
active task id when available. This forces the Threshold-Exceeded Checkpoint
protocol above: the deny fires once, the session stops, notifies the sender,
and awaits re-authorization. A revised profile is recompiled with `oacp
envelope compile --extend`, which preserves accumulated counters.

A granted re-authorization takes effect without that recompile, which is
denied from inside the bound session: the adapter reads the governing audit
record's `threshold_checkpoint.reauthorization.scope` at enforcement time and
overlays it on the compiled envelope, so the effective ceiling is
`expected_files_touched` widened to the granted `max_actual_files_touched` —
never past it, never below it, and never from a scope-less approval, which
grants nothing durable. The envelope itself stays immutable; only `scope`
(the policy-capped value) is consulted, never `requested_scope`.

### Enforcement recording

The audit `result` block records `envelope_enforcement: hooks | none`.
Receivers set `hooks` after a successful compile on an adapter-equipped
runtime; `none` means pickup-gate-only enforcement. Degradation must never
be silent: when `none` is a **rule** rather than a runtime gap, the record
carries the named reason alongside it —
`envelope_enforcement_reason: public_visibility_admission_approved` for
the admitted-public branch above, stamped by the compiler itself. A bare
`none` with no reason means an adapterless runtime (the historical
pickup-gate-only state), distinguishable from the rule-based mode.

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

### Review-loop continuation

Review lifecycle messages (`review_request`, `review_feedback`,
`review_addressed`, `review_lgtm`) carry no `task_profile` and do not run
the four task gates. Without a grant, every reviewer invocation requires
explicit human confirmation — that default is unchanged. A human may
instead grant a bounded, revocable continuation for **one PR review
thread**, so follow-up rounds in that thread auto-invoke the reviewer.

The grant authorizes *running* a review round, never its verdict: the
reviewer still fetches and validates the live PR head, runs the quality
gate, and independently chooses `review_feedback` or `review_lgtm` (see
`review_loop.md` → "Exact-Head Validation").

The grant is the same `approved_thread_continuation` kind, extended with a
`review_loop` block inside its scope. A review-only grant may omit the
task budget keys — they default to `0`, so the grant carries no
task-continuation authority:

```yaml
grant:
  decision: approved
  granted_scope:
    review_loop:
      repository: example-org/widget       # pinned repo slug
      pr_number: 88                        # pinned pull request
      allowed_types:                       # inbound types that may trigger
        - review_request                   #   a round (review_addressed only
      max_round: 3                         #   if listed explicitly)
      expires_at_utc: "2026-06-30T00:00:00Z"
      permitted_side_effects:
        writes_findings_packet: true
        sends_oacp_reply: true
        comments_on_github: false
        submits_github_review: false
```

Recognition and authority are separate: receiver config
(`autonomy.continuation_grants.enabled: true`) only enables grant
*recognition*. Authority exists only in a prior schema-v2 audit whose
`human_outcome` records a task decision of `approved` or `modified` plus a
grant decision of `approved` or `modified` with a valid
`granted_scope.review_loop`. A sender-declared grant claim is a request,
never proof — with no matching prior human decision it pauses with
`review_continuation_missing_approval`.

A fresh `review_request` auto-continues only when **all** of these hold,
checked in pinned order with early-out:

1. the receiver recognizes grants (`enabled: true`; else
   `review_continuation_ignored_disabled`) and the message has same-thread
   evidence (`conversation_id` or `parent_message_id`) matching the grant
   audit's sender and thread — cross-sender, cross-thread, and
   cross-receiver requests fall back to the explicit-confirmation default
   (`review_continuation_confirmation_required`);
2. the message type is listed in `allowed_types` — `review_addressed`
   stays context-only unless listed (the manual-continuation shape), and
   reviewer-output types (`review_feedback`, `review_lgtm`) are always
   context-only (`review_continuation_context_only`);
3. the declared `repo` and `pr` match the pinned `repository` and
   `pr_number`, the declared fields parse, and the round's requested side
   effects stay inside `permitted_side_effects` — any excess or
   unverifiable declaration pauses with
   `review_continuation_scope_exceeded` (fail closed: a request that does
   not declare its repository cannot be confirmed in-scope);
4. the effective round stays within `max_round`
   (`review_continuation_round_exceeded` otherwise). The effective round
   is `max(declared round, 1 + prior round-consuming audits in the
   thread)`, where an audit consumes one unit only when its invocation
   actually ran: an `auto_accepted` continuation round, or a pause whose
   recorded human outcome authorized the manual round
   (`approved`/`modified`). Declined, unanswered, and context-only
   records consume nothing — a dead ask can never exhaust the budget a
   later re-grant promises. The receiver's own audit trail is the floor,
   so a sender cannot under-declare the round number to stay inside the
   ceiling, and grant-listed `review_addressed` invocations cannot
   repeat unbounded;
5. the grant is unexpired on **both clocks**: neither the message's
   `created_at_utc` nor the evaluation time may pass `expires_at_utc`
   (`review_continuation_expired` otherwise). `created_at_utc` is
   sender-controlled, so the evaluation-time check is the binding one —
   a request queued before expiry does not run after it.

Authorization and revocation arbitrate on different clocks. An approval
governs only requests created after it — authority is never retroactive.
A denial takes effect the moment it is recorded: a denial decided before
*evaluation* revokes queued work even when the request predates it
(`review_continuation_revoked`), and on a tie the denial wins. A denial
is not a tombstone — a newer approval that still predates the request
re-establishes standing continuation.

The decision records a `review_continuation` block alongside
`continuation_grant`: the governing scope, the declared request context,
the `effective_round`, `exceeded_fields` on drift, the grant's
`source_audit`/`source_message_id`, and a `head_check`
(`declared_head`, `live_head`, `status:
match|mismatch|undeclared|unverified`). A head mismatch — including a
declared value that only shares a prefix with the live head, or a
truncated declaration — is recorded with
`review_continuation_head_mismatch` and never blocks the round: the live
head is authoritative, and the reviewer must resolve the declared value
against a live fetch before any terminal verdict. Head drift after
approval still invalidates completion per the review-loop protocol.

`permitted_side_effects` binds execution, not just admission: the
dispatching runtime passes the accepted scope's permitted set to the
reviewer invocation as its bound, and the reviewer withholds any outward
action whose entry is `false` (recording what was withheld) — a granted
round never performs a GitHub comment or review submission the grant did
not permit. A round that cannot complete without a forbidden effect
pauses instead of performing it.

Lexical hard-stop scanning deliberately does not run on review lifecycle
bodies: a granted reviewer invocation executes a pinned workflow whose
side effects are bounded by `permitted_side_effects`, not by sender prose,
and review bodies quote diffs and commands by design. Admission here is
scope matching against a human-granted bound, not body classification.
An admitted review decision carries no task `scope_envelope`; its bound is
the accepted `review_continuation.scope`.

## Taxonomy Pin

Audit `result.final_state` is limited to:

- `pending` (live; receiver-written at pickup — the evaluator never writes it)
- `paused` (live)
- `blocked` (live)
- `done` (terminal)
- `superseded` (terminal; see "Terminal finalization and audit integrity")
- `error` (terminal)

`result.completion_kind` is separately pinned to the evaluation-shape enum
above (see "Pinned completion_kind taxonomy"). Missing-profile messages that
obviously request PR/GitHub/commit/push/public work pause with
`risk_obvious_no_profile`, not the generic `task_profile_missing`.

The canonical fixture set lives in `tests/conformance/autonomy/`.
