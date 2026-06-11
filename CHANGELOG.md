# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
