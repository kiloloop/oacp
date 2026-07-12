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
| `allow_pr_artifacts` | Allow PR creation/update, review comments, and issue comments only when `target_repo` appears in the receiver-controlled `private_repo_allowlist`; direct main pushes, merges, deploys, and publishes still pause. |
| `allow` | Allow declared ordinary external side effects; non-demotable hard stops still pause. |

All other policy actions remain `pause`. A private PR-artifact profile must set
`target_repo: owner/repo`, `public_visibility: false`,
`external_side_effects: true`, and at least one of `creates_or_updates_pr` or
`comments_on_github` to `true`. The sender declaration is necessary but not
sufficient: `target_repo` must also match the receiver's independent
`private_repo_allowlist`. Branch commits and pushes needed to create that
artifact are folded into `external_side_effects`; direct pushes to `main` are
not.

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
that declares a PR/comment/commit while declaring no external side effects
pauses with `declaration_error`.

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
     artifact class when `target_repo` also appears in the receiver-controlled
     allowlist; public or unlisted repository artifacts and every other
     external side-effect class pause.
   - Pauses contradictory profile fields with `declaration_error`.
3. **Receiver classification**
   - Pause unconditionally on destructive command tokens: `rm -rf`, `--force`,
     `--no-verify`, `--dangerously-skip-permissions`.
   - For task-like messages, pause when the body asks for deploy, push to main,
     merge, publish, credential rotation, or dependency install.
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
spec_version: "0.3.5"
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
  sends_oacp_reply_only: true
  continuation_grants: {}
breached: []
runtime:
  agent: codex
  model: gpt-5
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
`spec_version: "0.3.5"` pins Gate 1 integrity enforcement plus the recalibrated
Gate 2/3 policy, full task-profile capture, explicit `breached` list, and the
outcome block shown above. Audit `schema_version: 2` adds thread identity and
the structured `result.human_outcome` block. Recorders may upgrade a v1 audit
to v2 when the first human outcome is written; standing grants trust only v2
records.

`breached` is always an ordered list, but its entries intentionally reflect the
evaluation phase. Admission-time pauses record pinned gate reason codes (for
example `estimated_minutes_exceeds_threshold`); post-accept checkpoint pauses
record the concrete declared/actual field paths that exceeded the envelope
(for example `side_effects_actual.creates_or_updates_pr`). `completion_kind`
distinguishes those two phases; `reason_codes` remains the canonical taxonomy
for the decision itself.

### Human approval and decline outcomes

When a paused task is approved, modified, or declined, record the decision in
the same audit file:

```bash
oacp autonomy-outcome <audit.yaml> \
  --decision approved \
  --decided-at 2026-05-12T13:25:00Z
```

The recorder copies the pause reason codes, computes decision latency from the
audit's `created_at_utc`, and locks the full read-modify-write sequence before
an atomic replacement. It refuses to overwrite a recorded outcome unless
`--replace` is explicit. `decision` is `approved`, `modified`, or `declined`.
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
  completion_kind: threshold_checkpoint_breached
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
    action: paused_for_reauthorization
```

If an undeclared side effect materializes, the receiver pauses with
`declaration_error`; `threshold_checkpoint.declaration_errors` identifies the
actual side-effect field. This checkpoint is mandatory before performing any
newly discovered capability or outward action.

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
  "spec_version": "0.3.5",
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
clears it (`oacp envelope clear`) at completion.

Runtime decisions:

- **deny** — the call breaches a declared-false capability (destructive
  tokens, undeclared commits/pushes/PR mutations, secret-class or
  dependency-manifest writes — including determinable Bash write targets
  such as redirects and common writer programs), targets a repo outside the
  embedded allowlist or pinned `target_repo`, pushes to a protected branch
  or with bulk-ref flags (`--mirror`, `--all`, `--delete`), is a GitHub
  mutation outside every allow class (merges, releases, issue creation), or
  attempts envelope self-modification (`oacp envelope compile|clear` from
  inside the enveloped session).
- **ask** — the adapter cannot confidently classify the call (shell
  indirection like `bash -c`, wrapper flags, unknown GitHub or oacp
  mutations, implicit `gh api` writes, unresolvable repository). The exact
  command is escalated for just-in-time review; unenforceable never
  silently degrades to allowed.
- **allow** — emitted as *no output*: the envelope can only narrow the
  harness's own permission surface, never widen or bypass it.
- `oacp send` is never denied; it is the checkpoint notification pipe. The
  exemption is exactly that wide: read-only oacp subcommands pass, all other
  oacp mutations are classified.
- Determinable Bash write targets feed the same distinct-file counter as
  Edit/Write calls (`/dev/*` excluded), so shell writes cannot bypass
  `expected_files_touched`.

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

Detailed terminal meaning belongs in `result.completion_kind`. Missing-profile
messages that obviously request PR/GitHub/commit/push/public work pause with
`risk_obvious_no_profile`, not the generic `task_profile_missing`.

The canonical fixture set lives in `tests/conformance/autonomy/`.
