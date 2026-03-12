# OACP — Open Agent Coordination Protocol

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**Empowering solo founders to coordinate AI agents, with human-in-the-loop for control.**

A file-based coordination protocol for multi-agent engineering workflows. OACP defines the message formats, state machines, review processes, and safety rules that enable AI agents on different runtimes to collaborate asynchronously through a shared filesystem.

**OACP is not a framework or SDK.** It is a set of conventions, YAML schemas, and shell scripts that any agent runtime can implement — Claude, Codex, Gemini, or your own.

### Features

- **Inbox/outbox messaging** — async YAML-based communication with threading, broadcast, and expiry
- **Structured review loop** — severity-graded findings, quality gates, and multi-round review
- **Durable shared memory** — project facts, decisions, and open threads that persist across sessions
- **Dispatch state machine** — full task lifecycle tracking from assignment to merge
- **Agent safety defaults** — baseline rules for git, credentials, staging, and scope discipline
- **Runtime-agnostic** — works with any agent runtime that can read/write files

## Why OACP?

When multiple AI agents work on the same codebase, they need a way to:

- **Communicate** — send task requests, review feedback, and handoffs without shared memory
- **Review each other's work** — structured review loops with quality gates and severity-based findings
- **Stay in sync** — durable memory files that persist decisions across sessions and runtimes
- **Stay safe** — baseline safety rules for git operations, credential scoping, and scope discipline

OACP solves this with a filesystem-based protocol that requires no server, no database, and no vendor lock-in. Agents read and write YAML files in a shared directory — that's it.

## Install

```bash
uv tool install oacp-cli
```

```bash
pipx install oacp-cli
```

```bash
uvx --from oacp-cli oacp doctor
```

<details>
<summary>From source</summary>

```bash
git clone https://github.com/kiloloop/oacp.git
cd oacp
uv tool install .
```

</details>

## Commands

- `oacp init` creates a project workspace under `$OACP_HOME/projects/`
- `oacp send` sends a protocol-compliant inbox message
- `oacp doctor` checks environment and workspace health
- `oacp validate` validates an inbox/outbox YAML message

If `OACP_HOME` is unset, workspace commands default to `~/oacp` (underscore).

## Key Concepts

| Concept | Description |
|---------|-------------|
| **Inbox/Outbox** | Async messaging between agents via YAML files in `agents/<name>/inbox/` |
| **Review Loop** | Structured code review: `review_request` → `review_feedback` → `review_addressed` → `review_lgtm` |
| **Quality Gate** | Merge-readiness criteria: no unresolved P0/P1 findings, deferred nits tracked |
| **Durable Memory** | Shared `memory/` directory with project facts, decisions, and open threads |
| **Dispatch States** | Task lifecycle: `received` → `accepted` → `working` → `pr_opened` → `in_review` → `done` |
| **Safety Defaults** | Baseline rules all agents follow: no force push, no secrets in commits, stage hygiene |

## Project Structure

```
oacp/
├── docs/
│   ├── protocol/       # Canonical protocol specifications (13 specs)
│   └── guides/         # Setup, adoption, versioning
├── scripts/            # 13 kernel scripts (Python + shell)
├── templates/          # Packet, role, and guardrail templates (19)
├── tests/              # Test suite
├── Makefile            # Task runner (make help for all targets)
└── SPEC.md             # Full protocol specification
```

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/kiloloop/oacp.git
cd oacp

# 2. Install the CLI
uv tool install oacp-cli

# 3. Initialize a project workspace
export OACP_HOME="$HOME/oacp"
oacp init my-project

# 4. Send your first message
oacp send my-project \
  --from alice --to bob --type task_request \
  --subject "Implement feature X" \
  --body "Details here..."

# 5. Check environment health
oacp doctor
```

See [QUICKSTART.md](QUICKSTART.md) for a complete 5-minute walkthrough.

## Scripts

OACP ships 13 scripts — the key ones you'll use most:

- **`init_project_workspace.sh`** — create a new project workspace (the first script you run)
- **`send_inbox_message.py`** — send protocol-compliant messages between agents
- **`preflight.py`** — unified quality checks (CI runs this on every PR)
- **`oacp_doctor.py`** — environment and workspace health check

Run `make help` to see all available Makefile targets, or see [SPEC.md](SPEC.md) for the full script inventory.

## Prerequisites

- Python 3.9+
- Bash 3.2+ (macOS default is fine)
- `gh` CLI (for GitHub operations)
- `pyyaml` (`pip install pyyaml`)

## Protocol Specification

The full protocol is documented in [SPEC.md](SPEC.md), covering:

1. **Inbox/Outbox Messaging** — message format, types, lifecycle, threading, broadcast
2. **Dispatch State Machine** — task lifecycle from delivery to completion
3. **Review Loop** — packet-based and inbox-based review with quality gates
4. **Cross-Runtime Sync** — durable memory, handoff context, session init
5. **Safety Defaults** — git safety, staging hygiene, credential scoping

Individual protocol specs live in [`docs/protocol/`](docs/protocol/).

## Workspace Layout

When you initialize a project, OACP creates this structure:

```
$OACP_HOME/projects/<project>/
├── agents/
│   ├── <agent-a>/
│   │   ├── inbox/          # Other agents write here
│   │   ├── outbox/         # Sent messages (copies)
│   │   ├── status.yaml     # Dynamic agent state
│   │   └── agent_card.yaml # Static agent identity
│   └── <agent-b>/
│       └── ...
├── memory/                  # Shared durable memory
│   ├── project_facts.md
│   ├── decision_log.md
│   └── open_threads.md
├── packets/                 # Review/findings artifacts
└── workspace.json           # Project metadata
```

## Development

```bash
make test
make preflight
```

## Documentation

- [SPEC.md](SPEC.md) — Full protocol specification
- [QUICKSTART.md](QUICKSTART.md) — 5-minute getting started guide
- [docs/guides/setup.md](docs/guides/setup.md) — Detailed setup guide
- [docs/guides/adoption.md](docs/guides/adoption.md) — Adoption guide (minimum → full)
- [docs/protocol/](docs/protocol/) — Individual protocol specs
- [CONTRIBUTING.md](CONTRIBUTING.md) — How to contribute

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
