# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-07-17

### Added

- Task-profile granulars `merges_pr` and `files_issues`: senders can now
  declare PR-landing authority and issue-filing (issue create/edit/close
  plus label creation) as first-class capabilities. The envelope compiler
  embeds both as constraints, and the Claude adapter's gh classifier
  consults them before its deny classes — `gh pr merge` requires a
  declared `merges_pr`, `gh issue create/edit/close` and `gh label
  create` require a declared `files_issues`, both still subject to the
  repo allowlist/visibility gate. That gate judges the repository gh will
  actually mutate: every repository selector on the command (each
  `-R/--repo` occurrence, any URL-shaped positional, and the *effective*
  `GH_REPO` environment — ambient hook-process values seeded first,
  inline assignments applied with shell precedence) must agree on a
  single repository, any effective `GH_HOST` escalates, and the allow
  classes are judged only as standalone simple commands (an earlier
  compound segment could retarget them) — so neither a positional URL, a
  repeated flag, an inline or pre-session environment assignment, nor
  prior shell state can retarget an approved command.
  Under the `allow_pr_artifacts` policy,
  `files_issues` joins the private-repository artifact class (an
  issue-filing deliverable on an allowlisted repo can auto-accept), while
  a declared `merges_pr` always pauses at admission (`merges_pr_pause`)
  so merge authority passes a human at least once; a human-approved
  continuation grant may cover it — and a profile that declares
  `merges_pr` routes the lexical `merge` verb to that granular path
  instead of the blanket hard stop (undeclared merge wording still hard
  stops). The declarable keys, the grant-coverable keys, and the
  checkpoint's `side_effects_actual` keys are now the same set by
  construction — the checkpoint can no longer observe a capability the
  schema cannot declare.
- `oacp trust revoke <kid> --project <name> (--agent <receiver> |
  --all-receivers)`: revocation is no longer a hand-edit of
  `allowed_signers.yaml`. The revoke validates the kid's canonical
  spelling up front, loads pins through the full integrity check, flips
  the entry to `status: revoked` while keeping the key material in place
  (the reader re-validates retained jwks, so a later manual reactivation
  cannot smuggle swapped material), and re-emits the file canonically
  under the project trust lock. An unknown kid is refused — revoke never
  creates entries — and re-revoking reports `unchanged` so fleet
  compromise response is safely re-runnable. `--all-receivers` is
  all-or-nothing: receivers are enumerated under the lock and every
  target is loaded and integrity-validated before any pin is written, so
  a malformed receiver file fails the whole command with zero pins
  changed.
- Checkpoint pause instrumentation: a breached `threshold_checkpoint` now
  stamps `paused_at_utc` (when the checkpoint fired — receivers pass the
  actual pause moment in the checkpoint actuals, with the evaluation time
  as fallback) and `breach_basis: declared_intent | realized`, so
  prospective declaration-correction pauses (caught before the undeclared
  action materialized) are distinguishable from realized drift. The
  prospective shape has its own validated input — receivers pass the
  invalidated declaration paths in `actuals.declared_intent_fields`,
  which breach the checkpoint directly while every realized effect stays
  false, with `predicted_risk_materialized` pinned false (caught before
  materialization by definition). The vocabulary is the monotone risky
  capability booleans (the restrictive `sends_oacp_reply_only` is
  excluded), and the two shapes are mutually exclusive on derived sources
  as well as the explicit basis: intent fields mixed with a realized
  breach source, with any true realized effect, or naming a capability
  already authorized by the envelope or an accepted continuation grant
  are rejected before an ambiguous record can be written.
- Audit records gain `co_occurring_reason_codes`: numeric Gate-2
  thresholds are evaluated before any early-out, so a pause taken for
  another reason (most commonly a lexical hard stop) still records a
  co-occurring threshold breach. Silence in a pause record now means the
  thresholds passed, never that they went unevaluated — previously a
  hard-stopped dispatch with a breached declaration was invisible to
  threshold-calibration analytics.
- `oacp init --oacp-dir`: the workspace initializer accepts the same
  explicit home override as its sibling subcommands (key/trust/verify/
  doctor/send/envelope), making scripted scratch-workspace setups
  stateless.
- Trust-root drift: `oacp doctor` surfaces cataloged-but-unpinned
  identities as the advisory code `catalog-not-pinned`, escalating to warn
  when the identity is live (another receiver pins it, or the receiver's
  inbox holds traffic from that agent). Pinning stays optional — the
  advisory makes the verification-enablement gap visible instead of
  reporting a clean trust root beside `signed-unknown-kid` annotations.
- Autonomy gate: the evaluator self-stamps provenance (`source`,
  `content_sha256` of its own file bytes, best-effort `git_sha`,
  `executed: true`) into every decision it returns; receivers copy the
  block verbatim into audit records instead of composing an `evaluator`
  field by hand.
- Signing conformance: new `tamper_kid_alias` vector pins that a
  non-canonical kid spelling in the protected header fails verification
  as `signed-INVALID` (framing), and the `tamper_kid_unknown` vector now
  uses a canonical unknown-kid spelling.
- Receiver audit stamping documented: `oacp verify --attach-audit` is the
  one supported path for recording `message_auth` into autonomy audit
  records; the canonical `result.message_auth` location and block shape
  are pinned in the signing protocol doc (hand-written blocks are the
  anti-pattern).

- Message signing, sender half: signed messages carry a single final-line detached-JWS
  `auth` trailer over the exact raw prefix bytes (no canonicalization,
  EdDSA-only locked JOSE profile, `crit:oacp`, RFC 7638 `kid`, agent +
  machine-instance URN identity, 1-8 signature rotation overlap).
  `oacp key gen`/`oacp key list` manage dedicated Ed25519 keys under
  `$OACP_HOME/keys/` (0700/0600, never synced) with public catalog stubs
  for the upcoming trust-import flow. `oacp send` signs when the sender's
  `signing.sign_messages` config knob or `--sign` is set (default off;
  unsigned fleets are unaffected), via render-unsigned-once →
  append-once → atomic write. The message validator accepts the `auth`
  field with strict structural framing checks (final-physical-line rule,
  bounded container, locked header profile). Signing requires the new
  optional `crypto` extra (`pip install 'oacp-cli[crypto]'`).
- Message verification, receiver half: `oacp verify` and the `message_verify` library implement
  verify-before-parse — strict last-line trailer extraction from raw bytes,
  bounded structural checks, EdDSA verification over the exact raw prefix
  against **receiver-local pins only** (no network at verify time), then an
  identity cross-check on the verified prefix. Warn mode records identity
  and grants no authority: unsigned / signed-unknown-kid / signed-verified /
  signed-INVALID each produce an annotation and a `message_auth` audit
  block (additive to schema v2, recorded like `human_outcome`), never a
  rejection. Byte-tamper cases write a no-clobber evidence copy aside into
  `dead_letter/` without touching the original message. The
  `signing.verify_mode: off|warn` receiver knob defaults off; receivers
  without it behave exactly as today. The pin reader is a minimal stub of
  the `allowed_signers.yaml` format (versioned, `domain`/`instance`
  columns reserved from day one) and stays swappable. Verification requires
  the optional `crypto` extra.
- Trust root:
  `oacp trust import` records a `<kid>.pub.json` stub from `oacp key gen`
  in the project's **zero-authority** distribution catalog
  (`projects/<p>/trust/catalog.yaml` — identity recorded, nothing granted)
  and pins it `active` in the receiver's own `allowed_signers.yaml`, the
  only file consulted at verify time. Import is integrity-checked (`kid`
  must be the RFC 7638 thumbprint of its `jwk`; `x` must strictly decode
  to a 32-byte Ed25519 public key; private components refused;
  same-`kid` conflicts refused; a `revoked` pin is never reactivated),
  validates project and receiver names with containment checks so trust
  files can never be written outside the selected project, serializes the
  whole catalog-then-pins transaction on a stable project trust lock so
  concurrent imports cannot drop each other's entries, upgrades
  same-identity entries that predate the reserved columns in place, and
  works without the `crypto` extra. `oacp trust list` inspects both
  files. `oacp doctor --project <name>` gains a Trust Root category:
  pinned-but-not-cataloged identities warn, unreadable or
  integrity-failing trust files error, unpinned catalog identities are
  the normal resting state. The `allowed_signers.yaml` reader is
  finalized as the canonical format-v1 reader and is itself the verify
  boundary: it enforces the strict public-only JWK profile and recomputes
  the `kid` thumbprint, so bad or private key material makes the file
  unusable rather than silently trusted (reserved `domain`/`instance`
  columns shape-checked, semantics deferred) — still the single swap
  point.
  Non-normative warn-mode seam + key-management docs land in
  `docs/protocol/message_signing.md` (same-UID caveat stated; keystore
  hardening — OS keychain / vault backends behind the same `kid` seam —
  is planned v0.4.1+ follow-up).
- Signing conformance corpus (`tests/conformance/signing/`): byte-exact
  golden fixtures pin the auth-trailer boundary, the signed prefix bytes,
  and the RFC 7515 signing inputs across both parse paths (framing
  splitter and receiver tri-state classifier, plus the validator
  cross-check), with published fixture keys and receiver pins. A
  tamper-detection suite covers payload byte flips, trailer transplant
  between messages, signature corruption, kid substitution (unknown,
  cross-agent, and re-encoded-header flavors), whitespace/EOL edges
  at the trailer boundary (CRLF, trailing blank line, missing final LF,
  trailing space, padded base64url, indented lookalike), and encoding
  aliases (pad-bit spellings of the signature member and the outer auth
  value, non-compact container emit). Goldens
  regenerate deterministically (`regen_fixtures.py`, README documents
  that a golden change is a wire-format change requiring a ruling), and
  the runner re-signs every golden and regenerates the whole corpus in
  CI so behavior drift against the committed contract fails loudly.
- Canonical trailer encodings: the auth trailer value and each
  `signatures[].signature` member must be the canonical unpadded
  base64url spelling of their bytes (a canonical round-trip check at the
  shared framing boundary — the same single-choke-point pattern as the
  trust-file JWK `x` validator, now shared as
  `b64url_decode_canonical`), and the auth container JSON must be the
  signer's compact sorted-key emit. Encoding aliases (non-zero unused
  pad bits) and container formatting variants previously verified as
  byte-different artifacts of the same signed message; they now fail
  verification as `signed-INVALID`. Canonical signer output is
  unaffected — one signed message has exactly one wire spelling.

### Changed

- `result.completion_kind` is now a pinned four-value enum naming the
  terminal shape of the evaluation only — `auto_accepted`,
  `admission_paused`, `checkpoint_paused`, `config_malformed` — with the
  pause cause left to `reason_codes`, the run state to
  `result.final_state`, and human decisions to `result.human_outcome`.
  The evaluator rejects unpinned kinds, declared-risk-flag pauses no
  longer stamp the misleading `hard_stop` kind, and receivers copy the
  evaluator's value verbatim (receiver-composed kinds are
  non-conforming). Records written before this pin carry mixed
  cause/event/state values and cannot be bucketed against the enum.
- `workspace.json` now stamps `spec_version` (the protocol version the
  workspace was initialized against) at init and update. The retired
  `standards_version` field — a stamp from a VERSION file that no longer
  exists, producing meaningless values — is no longer written; existing
  values are left in place for readers that have not migrated.
- `oacp autonomy-outcome` pins the `human_outcome.actor` convention: one
  canonical, whitespace-free handle per human, fleet-wide. The recorder
  rejects whitespace-bearing actors and warns when the anonymous default
  `human` ships, so cross-receiver outcome analytics can attribute
  decisions.
- The canonical `$OACP_HOME/.gitignore` written by `oacp memory init` (and
  checked by the doctor `root-gitignore` drift check) now carries an
  explicit `keys/` deny line so private key material can never be swept
  into memory sync, even by a future allowlist widening. The push-side
  allowlist already structurally excluded `keys/`; a regression test now
  pins that.

- `render_yaml()` now loudly rejects `auth`/`sig_*` input instead of
  silently dropping unknown fields for authentication content — signed
  bytes are never re-rendered.
- Operational note: audit-record stamping (`oacp autonomy-outcome`,
  `oacp verify --attach-audit`) serializes read-modify-write cycles on a
  stable, empty `<record>.yaml.lock` sibling file next to each audit
  record. These lock files are expected and harmless — leave them in
  place; the lock file is deliberately never replaced, because swapping
  it would let two concurrent writers hold different locks and silently
  drop each other's updates.

### Fixed

- `oacp autonomy-outcome` now accepts checkpoint-paused records — an
  auto-accepted admission whose in-place threshold checkpoint breached —
  and measures decision latency from the checkpoint's `paused_at_utc`
  rather than refusing outright (which drove hand-written outcome blocks)
  or measuring from admission time (which counted the whole execution
  window as human latency). For paused records the pinned
  `completion_kind` is the phase discriminator: an admission pause keeps
  measuring from admission even when actuals attached to the evaluation
  happen to breach the checkpoint. The reason-code fallback survives only
  for genuinely pre-enum records (schema v1 without a pinned kind); a
  current-schema record whose kind is missing or out of vocabulary is
  refused loudly as malformed — including the breached in-place
  auto-accepted shape, which must itself carry `checkpoint_paused`. A
  checkpoint record without a pause stamp is refused loudly; latencies on
  checkpoint records written before the stamp existed are not comparable
  with post-fix values.
- Message signing: `validate_kid` requires the canonical base64url
  spelling (round-trip check), closing the encoding-alias gap for kid
  values that arrive without key material — most notably a revoked,
  jwk-less pin entry whose alias-spelled kid could never match at pin
  lookup, silently missing the revocation. Alias kids now fail the pins
  load loudly, and a trust-root load failure stays visible in the
  `signed-unknown-kid` annotation reason instead of being masked.
- Envelope enforcement: the documented completion step (`oacp envelope
  clear`) now executes from inside the enforced session. The Claude adapter
  validates the clear against the task's newest audit record, matched by
  record content (`message_id` + `receiver` — filenames are never trusted,
  and the message id is pinned to a safe-id grammar at compile time so it
  can never act as a glob). A terminal `result.final_state`
  (`done`/`error`) sanctions the clear; `pending`/`paused`/no matching
  record still deny. The validation judges the CLI's effective target: the
  clear must be a standalone simple command (compound commands whose
  earlier segments could retarget it escalate), flag values use
  last-occurrence semantics, relative `--oacp-dir` resolves against the
  call's cwd, and `OACP_HOME=` overrides, mismatched
  project/receiver/home, or unreadable audit records escalate. Previously the hook blanket-denied the
  clear as self-modification, stranding every envelope active after task
  completion until a human cleared it. `oacp envelope compile` from inside
  the session remains denied unconditionally.
- Envelope enforcement: protocol-mandated bookkeeping no longer consumes
  the `expected_files_touched` budget. Writes to the receiver's own
  `audit/`, `inbox/`, and `outbox/` directories and the runtime scratchpad
  skip the file counter; containment is judged on resolved filesystem
  targets, so symlinks planted under exempt roots cannot smuggle task
  scope past the counter, and secret-class/dependency-manifest gates still
  apply. Previously the completion audit write and reply body-file counted
  against the declared budget, so a tightly declared task was guaranteed a
  mid-run deny at close-out. Two receiver surfaces gained explicit
  protection in the same change: any filesystem-mutator operand
  (write/`rm`/`unlink`/`mv`/`cp`/`truncate`/…, source or destination)
  under the receiver's `state/` directory (the active envelope itself) is
  denied as envelope self-modification, and trust roots (receiver pins,
  project catalog) are treated as authority-bearing auth config gated by
  `touches_auth_config_or_secrets` for writes and mutations alike. Mutator
  operands are parsed the way the utility parses them (GNU
  target-directory spellings, `--`), and mutator operands or Bash write
  targets carrying shell expansion syntax (globs, braces) escalate instead
  of being judged by their literal spelling; `find` with action predicates
  (`-exec`, `-delete`, …) escalates as unclassifiable.

## [0.3.5] - 2026-07-11

### Added

- Receiver autonomy supports sender-marked `oacp-guardrails` fences, negation
  fallback, declaration-aware lexical advisories, and a pinned reason-code
  taxonomy backed by executable conformance fixtures.
- `external_side_effects: allow_pr_artifacts` permits declared PR, review, and
  issue-comment artifacts only for receiver-allowlisted private repositories,
  while retaining pre-approval for public/unlisted artifacts, direct main
  pushes, merges, deploys, and publishes.
- Autonomy audit output records the full declared task profile, an explicit
  breach list, a semantic policy hash, and a shared outcome block including
  completion time and materialized-risk telemetry.
- Default-off standing continuation grants now resolve from an explicit prior
  human approval in the same conversation thread; sender-declared grant data
  alone cannot authorize follow-up scope.
- `oacp autonomy-outcome` atomically records structured approval, modification,
  decline, latency, and grant-decision telemetry in schema-v2 autonomy audits.
  The subcommand is exposed by the installed CLI starting with this release.
- Envelope compilation: `oacp envelope compile|show|clear`
  turns an admitted message's `task_profile` plus receiver config into a
  runtime envelope (`active_envelope.json`), enforced at the tool-call layer
  by a static Claude PreToolUse hook (`oacp-envelope-hook`, registered once
  by `oacp setup claude`). Compilation is fail-closed
  (`envelope_compile_error`), envelope drift denies with the canonical
  threshold-checkpoint opener, unclassifiable calls escalate to `ask`, and
  the audit outcome block records `envelope_enforcement: hooks | none`.
  Contract pinned by executable fixtures under `tests/conformance/envelope/`.
- The security policy documents the Tier-1.5 trust ceiling for single-OS-user
  hosts: `from` fields are unauthenticated traceability today, future optional
  signing would add tamper-evidence and provenance but not same-host
  anti-impersonation, and hard isolation requires separate OS users,
  containers, or hosts.

### Changed

- The versioning guide documents the staged release pipeline end to end,
  replacing the retired single-repo flow.
- Documentation examples and conformance fixtures use neutral example
  repository slugs.
- The standard `auto_review` estimate cap is 45 minutes; estimates above 45
  still pause and the five-file cap remains unchanged.
- Pricing and commercial matches remain hard pauses but now use the separate
  `hard_stop_content_sensitivity` category.
- Follow-up declarations outside a standing grant's time, file, or side-effect
  scope re-pause before work begins.

### Fixed

- `doctor --memory` now forwards bounded network timeouts to remote Git fetches
  without breaking custom runners that implement the original command interface.
- The MCP stdio coordinator now converts unexpected request exceptions into
  structured `-32603` responses instead of crashing the server.
- Undeclared runtime side effects now produce `declaration_error` at the
  threshold checkpoint instead of silently completing outside the profile.
- Message validation now accepts and type-checks the six documented
  review-loop telemetry fields: `model`, `turns`, `input_tokens`,
  `output_tokens`, `wall_time_s`, and `est_cost_usd`.
- Receiver config and doctor validation share one repository-slug regex instead
  of maintaining duplicate definitions.

## [0.3.4] - 2026-06-11

### Fixed

- Receiver autonomy Gate 1 now performs real message schema validation, expiry
  comparison, SHA-256 hash recording, and same-receiver replay detection before
  auto-accepting work.
- Receiver autonomy Gate 3 now pauses on credentials, pricing, commercial
  content, and anchored config-sensitive scope, and evaluates external
  side-effect hard stops before sensitive-scope hard stops to match the
  protocol.

## [0.3.3] - 2026-06-11

### Added

- `oacp watch --state-id <id>` for per-subscriber cursor files, allowing
  concurrent watchers of the same agent inbox to receive the same new-message
  events without sharing a cursor.

### Changed

- Docs: refreshed the runtime capability matrix and prompt-caching guidance
  for current runtime releases.

### Fixed

- `oacp send --oacp-dir` and `oacp inbox --oacp-dir` now expand `~` through the
  shared OACP home resolver instead of treating it as a literal path component.
- Inbox and outbox delivery writes now use same-directory temp files plus
  atomic replace so readers do not observe partial `.yaml` messages.
- Memory archive tests now isolate git config while preserving test identities,
  so local commit-signing settings do not break the suite.

## [0.3.2] - 2026-05-26

### Added

- Receiver autonomy scope-envelope evaluator with side-effect booleans,
  threshold-checkpoint instrumentation, taxonomy pinning, and default-off
  continuation grants.
- `cursor` runtime support for agent profiles, agent cards, status validation, sender inference via `OACP_RUNTIME=cursor`, `oacp add-agent --runtime cursor`, and `oacp setup cursor`.
- `oacp setup cursor --project <project>` now provisions the project-side Cursor agent directory and writes a repo-local `.cursor/rules/oacp.todo.mdc` placeholder while Cursor-owned rules and memory hooks remain deferred.

### Changed

- `oacp init` now defaults to `claude,codex,cursor`; Gemini remains supported through `--agents` and `oacp setup gemini`.
- Receiver autonomy docs and templates now use the current
  `agents/<receiver>/config.yaml` schema.
- Docs: added an asynchronous `claude -p` on-ramp guide and refreshed the
  quickstart for the current CLI surface.

## [0.3.1] - 2026-05-12

### Added

- `auto_review` autonomy mode — an opt-in receiver-side autonomy profile that classifies inbound messages into auto-accept, pause, or hard-stop bands using clean / ambiguous / hard-stop trigger predicates. Configured via `receiver_config.autonomy_mode: auto_review` with `auto_review_profile` selecting `standard` or `tight`. Off by default; existing receivers continue to behave as `always_pause` unless they opt in. Ships with a conformance fixture suite under `tests/conformance/autonomy/` covering clean tasks, ambiguous scope, hard-stop triggers, and malformed config handling.

### Changed

- README: refreshed with a hero image and three screenshots illustrating inbox flow, doctor output, and the multi-agent workspace layout. Hub README copy, install path, and command table aligned with the current CLI surface.
- Docs: link to companion oacp-skills repo from the README and onboarding pages so readers can find the skill library that pairs with the protocol.

## [0.3.0] - 2026-04-29

### Added

- `oacp memory init|clone|pull|push|disable` subcommands for opt-in cross-machine sync of `$OACP_HOME/org-memory/**` and `$OACP_HOME/projects/*/memory/**` via a plain git repo rooted at `$OACP_HOME`. Three-state activation model: Disabled (no marker), Local-only (`init` without `--remote`), and Synced (`init --remote <url>` or `clone <url>`).
- `oacp doctor --memory` advisory checks (10 checks covering marker presence, allowlist coverage, remote configuration, fetch/divergence state, and signing setup).
- `oacp setup claude` now installs memory pull/push hook scripts and registers them in `.claude/settings.json` for Claude SessionStart and SessionEnd lifecycle events. Hooks are marker-gated and no-op silently unless `$OACP_HOME/.oacp-memory-repo` is present, so existing workflows are unaffected on machines that have not opted in.

## [0.2.3] - 2026-04-26

### Changed

- `oacp watch` defaults are now notification-friendly: existing inbox messages are no longer replayed on the first scan for a target, and `message_archived` events are suppressed by default. Pass `--since=epoch` to restore replay and `--show-archived` to re-enable archive events. Reduces noise for `Monitor` and `oacp watch` consumers.

### Added

- `oacp watch --since=<spec>` — controls the first-run baseline cutoff. Accepts `now` (default), `epoch`, relative durations (`30s`, `5m`, `2h`, `7d`), or ISO 8601 timestamps. Only applies on first run for a target (no state file yet).
- `oacp watch --show-archived` — opt-in flag to emit `message_archived` events. Useful for observer agents tracking another agent's inbox; disabled by default because the watching agent's own deletes are self-loops.

## [0.2.2] - 2026-04-17

### Added

- `oacp watch` — monitor-friendly inbox/outbox watcher with structured output and partial-progress preservation on errors

### Changed

- Docs: refreshed cross-runtime capability matrix and public-skill parity framing

## [0.2.1] - 2026-03-22

### Fixed

- `oacp doctor` no longer fails when `gh` CLI is not installed — `gh` is now optional (#78)
- `oacp send` relaxed handoff body schema validation to accept freeform content (#78)
- `oacp write-event --related` now handles JSON arrays correctly (#76)

### Changed

- README: refreshed command table with all v0.2.0 CLI commands, updated workspace layout diagram (#75, #77, #84)
- SPEC.md: synced with v0.2.0 — version header, org-memory section, kernel inventory with exposure column (#85)
- Onboarding docs: setup.md uses `pip install` as primary install, QUICKSTART.md adds `--agents`/`--repo` flags, CHANGELOG.md fixes `oacp memory archive` command name (#86)

## [0.2.0] - 2026-03-20

### Added

- `oacp inbox` command for listing agent inboxes with table and `--json` output
- Sender inference for `oacp send` — `--from` is now optional when `OACP_AGENT`, `AGENT_NAME`, or agent card runtime can identify the sender

### Changed

- Consolidated shared script constants into `_oacp_constants.py` — canonical `AGENT_RE`, runtime tuples, timestamp/template helpers
- Agent name validation now requires an alphanumeric first character (names starting with `_`, `.`, `-` are rejected)
- Message ID and filename suffixes use `secrets.token_hex` instead of `random.choices`

## [0.1.9] - 2026-03-20

### Added

- Memory archive layer with `oacp memory archive` CLI command for active/archive split (#62, #11)
- Declarative agent profiles with YAML schema and `oacp agent init|show|list` CLI commands (#52, #48)
- `known_debt.md` as standard memory file for tracking technical debt (#53, #32)

## [0.1.2] - 2026-03-18

### Added

- `oacp add-agent` command to add agents to existing workspaces (#43)
- `oacp setup` command to generate runtime-specific config files (#43)
- Org-level memory spec with `init_org_memory.py` and `write_event.py` scripts (#44)
- `oacp doctor --fix` flag for auto-fixing missing inbox dirs, missing/stale status files (#50)
- ACP (Agent Communication Protocol) to protocol comparison docs (#47)
- Doctor command exposed as marketing hook for onboarding (#49)

### Fixed

- `--fix` now derives correct runtime per agent instead of hardcoding `claude` (#50)

## [0.1.1] - 2026-03-16

### Added

- Quickstart example and protocol comparison table (#33)
- PyPI, runtime, and PRs Welcome badges to README (#31)

### Changed

- Version bump for post-release maintenance (#36)

## [0.1.0] - 2026-03-15

### Added

- Initial public release of `oacp-cli` on PyPI
- Core CLI commands: `oacp init`, `oacp send`, `oacp doctor`, `oacp validate`
- File-based inbox/outbox messaging protocol
- Project workspace initialization with agent directories
- Message validation against OACP schema
- Doctor command for environment and workspace health checks
- GitHub Actions release pipeline with PyPI Trusted Publishing
- Protocol specs: inbox/outbox, multi-agent shared workspace, credential scoping, cross-runtime sync
- Templates for review packets, agent roles, guardrails
- Shell and Python kernel scripts for workspace operations
- Apache 2.0 license, CONTRIBUTING guide, community health files

### Changed

- Renamed `$AGENT_HUB` to `$OACP_HOME` across codebase (#8)
- Removed legacy Antigravity workflow/policy system (#5)

## [0.1.0-rc1] - 2026-03-12

### Added

- Pre-release candidate for initial validation
- Tagline: empowering solo founders with HITL control (#18)

### Fixed

- Release workflow re-tag safety (#27)
- Checkout step in github-release workflow job (#19)
- Pre-release audit fixes: SHA-pinned actions, dangling doc refs (#15, #16)

[0.4.0]: https://github.com/kiloloop/oacp/compare/v0.3.5...v0.4.0
[0.3.5]: https://github.com/kiloloop/oacp/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/kiloloop/oacp/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/kiloloop/oacp/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/kiloloop/oacp/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/kiloloop/oacp/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/kiloloop/oacp/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/kiloloop/oacp/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/kiloloop/oacp/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/kiloloop/oacp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kiloloop/oacp/compare/v0.1.9...v0.2.0
[0.1.9]: https://github.com/kiloloop/oacp/compare/v0.1.2...v0.1.9
[0.1.2]: https://github.com/kiloloop/oacp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/kiloloop/oacp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kiloloop/oacp/releases/tag/v0.1.0
[0.1.0-rc1]: https://github.com/kiloloop/oacp/releases/tag/v0.1.0-rc1
