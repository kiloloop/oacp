# Runtime Capabilities Protocol

## Purpose

Define a standard schema for declaring runtime capabilities and dynamic agent status, enabling:
- **Capability discovery** — agents and tools can query what a runtime supports before dispatching work
- **Dynamic status** — agents publish their current state so coordinators can route tasks to available agents
- **Health validation** — `hub doctor` checks environment, workspace, and agent status consistency

## Static Capabilities

Each runtime declares a fixed set of capabilities. These do not change during a session.

### Canonical Capability Keys

| Key | Description |
|-----|-------------|
| `headless` | Can run without interactive prompts (CLI, API, background) |
| `mcp_tools` | Has MCP tool servers configured and usable |
| `shell_access` | Can execute shell commands |
| `git_ops` | Can run git operations (clone, commit, push, etc.) |
| `github_cli` | Has `gh` CLI authenticated and available |
| `subagents` | Can spawn typed sub-agents for parallel work |
| `parallel_teams` | Can orchestrate multi-agent teams with task lists |
| `web_search` | Can search the web for information |
| `browser` | Can interact with web pages (click, type, navigate) |
| `session_memory` | Has persistent memory across sessions |
| `notifications` | Can send/receive async notifications to other agents |
| `async_tasks` | Can run background tasks and check results later |
| `image_generation` | Can generate images from text prompts |

### Per-Runtime Defaults

These are reference defaults. Actual capabilities may vary by configuration.

| Capability | Claude | Codex | Gemini |
|------------|--------|-------|--------|
| `headless` | yes | yes | yes |
| `mcp_tools` | yes | partial | yes |
| `shell_access` | yes | yes | yes |
| `git_ops` | yes | yes | yes |
| `github_cli` | yes | yes | yes |
| `subagents` | yes | no | partial |
| `parallel_teams` | yes | no | no |
| `web_search` | yes | yes | yes |
| `browser` | partial | partial | yes |
| `session_memory` | yes | partial | partial |
| `notifications` | yes | yes | yes |
| `async_tasks` | yes | yes | yes |
| `image_generation` | no | no | yes |

See `docs/guides/runtime_capability_matrix.md` for the full parity matrix with details.

## Dynamic Status Schema

Agents publish a `status.yaml` file that reflects their current state. This file is written at session boundaries and may be updated during long-running work.

### Location

```
projects/<project>/agents/<agent_name>/status.yaml
```

### Schema

```yaml
runtime: claude                         # Runtime identifier: claude | codex | gemini | human
model: claude-opus-4-6                  # Model version string
status: available                       # available | busy | offline
current_task: ""                        # Free-text description of current work (empty if idle)
capabilities:                           # List of canonical capability keys
  - headless
  - shell_access
  - git_ops
updated_at: "2026-02-16T20:00:00Z"     # ISO 8601 UTC timestamp of last update
```

### Field Definitions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `runtime` | string | yes | One of: `claude`, `codex`, `gemini`, `human`, `unknown` |
| `model` | string | no | Model version identifier |
| `status` | string | yes | One of: `available`, `busy`, `offline` |
| `current_task` | string | no | What the agent is currently doing |
| `capabilities` | list[string] | yes | Canonical capability keys this agent supports |
| `updated_at` | string | yes | ISO 8601 UTC timestamp |

### Status Values

| Status | Meaning |
|--------|---------|
| `available` | Agent is idle and can accept new work |
| `busy` | Agent is actively working on a task |
| `offline` | Agent session has ended or is unreachable |

### Update Triggers

Agents should update `status.yaml` on:
1. **Session init** — set `status: available` (or `busy` if starting with a task)
2. **Task start** — set `status: busy`, populate `current_task`
3. **Task complete** — set `status: available`, clear `current_task`
4. **Session close** — set `status: offline`

### Staleness Threshold

A `status.yaml` file is considered **stale** if `updated_at` is more than **1 hour** old. Stale status files suggest the agent session ended without a clean close. Health checks flag stale status as a warning.

## Mode Resolution

When dispatching work to an agent, use capability-based mode resolution:

```
if agent.has("headless") and agent.has("shell_access"):
    → headless execution (CLI / API)
elif agent.has("shell_access"):
    → tool-assisted execution (interactive with shell)
else:
    → manual execution (human-in-the-loop)
```

For review tasks, prefer agents with `github_cli` capability. For parallel work, require `subagents` or `parallel_teams`.

## Health Check Contract

The `hub doctor` command validates environment and workspace health. Every check produces a result with one of four severities:

### Severity Levels

| Severity | Symbol | Meaning |
|----------|--------|---------|
| `ok` | `[+]` | Check passed |
| `warn` | `[!]` | Non-blocking issue detected |
| `error` | `[x]` | Blocking issue — must be fixed |
| `skip` | `[-]` | Check skipped (tool not available) |

### Exit Codes

- **0** — no `error` results (warnings alone do not fail)
- **1** — one or more `error` results

### Check Categories

1. **Environment** — required and optional CLI tools (git, gh, python3, ruff, shellcheck, pyyaml)
2. **Workspace** — `workspace.json` existence and validity, agent and packet directories
3. **Inbox Health** — per-agent inbox directories, stale messages (>24h)
4. **Schemas** — YAML file validity in `packets/`, `status.yaml` validation
5. **Agent Status** — `status.yaml` presence per agent, staleness (<1h)

### Check Details

| Category | Check | Required | Severity if missing |
|----------|-------|----------|---------------------|
| Environment | git | yes | error |
| Environment | python3 | yes | error |
| Environment | gh | yes | error |
| Environment | ruff | no | warn |
| Environment | shellcheck | no | warn |
| Environment | pyyaml importable | no | warn |
| Workspace | workspace.json exists | yes | error |
| Workspace | workspace.json valid JSON | yes | error |
| Workspace | agents/ dir exists | yes | error |
| Inbox | per-agent inbox/ dir | yes | warn |
| Inbox | stale messages (>24h) | — | warn |
| Schemas | YAML files in packets/ valid | — | error |
| Schemas | status.yaml valid if present | — | error |
| Agent Status | status.yaml present per agent | — | warn |
| Agent Status | status.yaml not stale (<1h) | — | warn |

## Integration

### Session Lifecycle Hooks

`scripts/session_lifecycle_hooks.py` should write `status.yaml` during `init_session` and `close_session`:
- `init_session` → create/update `status.yaml` with `status: available` (or `busy` if `--packet-id` provided)
- `close_session` → update `status.yaml` with `status: offline`

### Hub Doctor

Run `hub doctor` to validate the full stack:

```bash
make doctor                                        # environment checks only
make doctor ARGS="--project <name>"                # full checks including workspace
make doctor ARGS="--json"                          # machine-readable JSON output
```

## Agent Cards

Agent cards are static identity files that declare an agent's skills, capabilities, permissions, and protocol bindings. They complement `status.yaml` (dynamic state) with stable metadata for discovery and routing.

### Location

```
projects/<project>/agents/<agent_name>/agent_card.yaml
```

### Schema (v0.2.0)

```yaml
version: "0.2.0"                       # Card schema version (semver)
name: claude                           # Agent name (matches inbox/outbox directory)
runtime: claude                        # claude | codex | gemini | human
model: claude-opus-4-6                 # Model version string
description: "Primary implementer"     # One-line role summary

skills:                                # Structured skill declarations (A2A-compatible)
  - id: code_review                    # Unique within agent
    name: Code Review                  # Human-readable name
    description: "Reviews PRs..."      # What this skill does
    tags: [review, qa]                 # Discovery tags

capabilities:
  tools: [Bash, Read, Edit]            # Available tools
  languages: [python, bash]            # Programming languages
  domains: [backend, infra]            # Domain expertise

permissions:
  allowed_dirs: []                     # Filesystem access
  denied_dirs: []
  github_operations: [pr_comment]      # GitHub operations

availability:
  schedule: "always"                   # "always" | cron | "manual"
  max_concurrent_tasks: 1
  timezone: "UTC"

protocol:
  inbox_path: agents/claude/inbox/
  outbox_path: agents/claude/outbox/
  supported_message_types:             # Message types this agent handles
    - task_request
    - review_request
```

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | Card schema version (semver) |
| `name` | string | Agent identifier (1-64 chars, alphanumeric/dots/hyphens/underscores) |
| `runtime` | string | One of: `claude`, `codex`, `gemini`, `human`, `unknown` |
| `description` | string | One-line role summary |

### Agent Card vs status.yaml

| Concern | Agent Card | status.yaml |
|---------|-----------|-------------|
| **Content** | Static identity, skills, permissions | Dynamic state, current task |
| **Updates** | Rarely (when capabilities change) | Every session boundary |
| **Purpose** | Discovery, routing, authorization | Health checks, availability |
| **A2A mapping** | Maps to A2A AgentCard | Maps to A2A task state |

### Validation

```bash
make validate-card ARGS="path/to/agent_card.yaml"
make validate-card ARGS="--dir path/to/agents/"
```

### A2A Compatibility

The card schema is inspired by Google's A2A Agent Card spec (v0.3.0). Key differences:
- **No HTTP endpoint** — Hub uses file-based inbox/outbox, not HTTP transport
- **Static/dynamic split** — A2A merges identity and state; Hub keeps them separate
- **Skills** — Hub's `skills[]` array matches A2A's `AgentSkill` shape (id, name, description, tags)
- **Permissions** — Hub adds filesystem and command permissions not present in A2A
- **Scheduling** — Hub adds availability and concurrency limits

## Cross-References

- **Parity Matrix**: `docs/guides/runtime_capability_matrix.md` — detailed per-runtime capability comparison
- **Inbox Protocol**: `docs/protocol/inbox_outbox.md` — agent messaging format
- **Session Lifecycle**: `scripts/session_lifecycle_hooks.py` (reference implementation) — session boundary hooks
- **Workspace Setup**: `scripts/init_project_workspace.sh` — project initialization
- **Agent Card Template**: `templates/agent_card.template.yaml` — card template
- **Card Validator**: `scripts/validate_agent_card.py` — schema validation
