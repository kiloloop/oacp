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
    max_estimated_minutes: 30
    max_expected_files_touched: 5
    destructive_ops: pause
    external_side_effects: pause
    auth_config_or_secrets: pause
    dependency_changes: pause
    public_visibility: pause
    git_push_or_deploy: pause
  allow_without_task_profile:
    - brainstorm_request
  continuation_grants:
    enabled: false
```

When config is absent, receivers behave as `always_pause`. Malformed config
causes a pause and should be surfaced by `oacp doctor`.

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
   - Cross-checks `estimated_minutes`, `expected_files_touched`, destructive
     scope, sensitive scope, and side-effect scope against receiver policy.
3. **Receiver classification**
   - Pause unconditionally on destructive command tokens: `rm -rf`, `--force`,
     `--no-verify`, `--dangerously-skip-permissions`.
   - For task-like messages, pause when the body asks for deploy, push to main,
     merge, publish, credential rotation, or dependency install.
   - For types listed in `allow_without_task_profile`, side-effect verbs such
     as deploy/publish/merge are logged as notes instead of hard stops.
     Destructive tokens still pause.
   - Path-like tokens such as `packets/deploy/` are not deploy verbs.
   - Pause when the body touches auth, config, secrets, dependencies, public
     repos, pricing/commercial content, or memory SSOT.
   - Pause when file scope is ambiguous or broader than the declared profile.
4. **Runtime/workspace**
   - Worktree is clean or the task can be isolated to a fresh branch.
   - No conflicting active task exists on the same repo.
   - Required tools are available.

LLM judgment may reduce false positives after deterministic gates pass. It
cannot override hard stops.

## Hard-Stop Override

Regardless of autonomy mode, receivers must pause on any of: destructive command
tokens (`rm -rf`, `--force`, `--no-verify`,
`--dangerously-skip-permissions`), external side effects
(push/deploy/merge/publish/rotate/install), or modifications to auth, config,
secrets, dependencies, public repos, pricing/commercial content, or memory SSOT,
unless explicitly authorized by a separate safety-default exception.

Continuation grants do not override destructive tokens, auth/secrets,
dependency, public-scope, pricing/commercial, or memory-SSOT hard stops.
When explicitly enabled, a valid continuation grant may cover declared external
side effects only for the scoped PR, GitHub comment, or commit continuation
fields that the grant marks true.

## Audit Events

Every autonomy decision writes one YAML file:

`agents/<receiver>/audit/autonomy_decisions/YYYYMMDDTHHMMSSZ_<message-id>.yaml`

```yaml
schema_version: 1
spec_version: "0.3.0"
created_at_utc: "2026-05-12T13:23:25Z"
receiver: codex
sender: iris
message_id: msg-20260512132325-iris-de62
message_type: task_request
message_subject: "Small docs cleanup"
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
  max_estimated_minutes: 30
  max_expected_files_touched: 5
task_profile:
  estimated_minutes: 20
  expected_files_touched: 3
  destructive_ops: false
runtime:
  agent: codex
  model: gpt-5
result:
  final_state: done
  completion_kind: auto_accepted
  actual_minutes: null
  actual_files_touched: null
  predicted_risk_materialized: false
  threshold_checkpoint:
    evaluated: false
    actual_minutes: null
    actual_files_touched: null
    side_effects_actual: {}
    breached: false
    action: not_evaluated
  reply_message_id: msg-...
  artifacts: []
```

`policy_path` and `policy_sha256` may be null when the pause is caused by
missing or malformed config. `sender` is logged only for traceability.

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
  threshold_checkpoint:
    evaluated: true
    actual_minutes: 25
    actual_files_touched: 4
    side_effects_actual:
      creates_or_updates_pr: true
      comments_on_github: true
      commits_changes: true
    breached: true
    action: paused_for_reauthorization
```

## Continuation Grants

`continuation_grants` are default-off. Receivers ignore grants unless their
config explicitly enables them:

```yaml
autonomy:
  continuation_grants:
    enabled: true
```

The supported grant kind is `approved_thread_continuation` under
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

A grant may be considered only when:

- receiver config enables continuation grants;
- the message has same-thread evidence via `parent_message_id` or
  `conversation_id`;
- the grant includes an explicit `scope`;
- actual work remains inside the granted scope.

If disabled, receivers log `continuation_grant_ignored_disabled` and evaluate
the message as normal. If enabled but actual work drifts outside the grant,
receivers pause with `threshold_checkpoint_breached`.

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
