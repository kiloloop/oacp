# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.2]: https://github.com/kiloloop/oacp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/kiloloop/oacp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kiloloop/oacp/releases/tag/v0.1.0
[0.1.0-rc1]: https://github.com/kiloloop/oacp/releases/tag/v0.1.0-rc1
