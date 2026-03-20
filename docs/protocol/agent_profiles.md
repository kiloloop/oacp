# Agent Profiles — Two-Tier Identity System

## Purpose

Agent profiles provide a two-tier identity system: **global profiles** declare stable defaults for an agent across all projects, while **project-level agent cards** override those defaults for a specific project context. The merge produces a single resolved view used for routing, discovery, and authorization.

The model mirrors how Claude Code handles instructions: a global `~/.claude/CLAUDE.md` sets baseline behavior, and a project-level `CLAUDE.md` overrides or extends it per repository. Profiles work the same way — define an agent's identity once, specialize per project.

## Directory Layout

```
$OACP_HOME/
├── agents/                              # Global agent profiles
│   ├── claude/
│   │   └── profile.yaml
│   └── codex/
│       └── profile.yaml
└── projects/<project>/
    └── agents/
        └── claude/
            ├── agent_card.yaml          # Project-level overrides
            ├── status.yaml
            ├── inbox/
            └── outbox/
```

- **Global profiles** (`agents/<name>/profile.yaml`) are created once per agent and rarely change.
- **Project cards** (`projects/<project>/agents/<name>/agent_card.yaml`) carry project-specific overrides — permissions, skills, routing rules — and are the authoritative resolved identity for that project.
- **Status files** (`status.yaml`) remain separate; they track dynamic session state, not static identity.

## Merge Rules

When both a global profile and a project card exist, they are merged with the following rules:

### Scalars

Project value wins if non-empty. Empty or null project values fall through to the global default.

```yaml
# Global profile              # Project card               # Merged result
model: "claude-opus-4-6"       model: "claude-sonnet-4"     model: "claude-sonnet-4"
description: "Implementer"    description: ""              description: "Implementer"
trust_level: standard          trust_level: elevated        trust_level: elevated
```

### Dict Sections

Key-level merge within the dict. Project keys override matching global keys; global-only keys are preserved.

```yaml
# Global profile               # Project card              # Merged result
capabilities:                  capabilities:               capabilities:
  tools: [Bash, Read]           tools: [Bash, Read, Edit]   tools: [Bash, Read, Edit]
  languages: [python]           domains: [trading]          languages: [python]
  domains: [backend]                                        domains: [trading]
```

Note: `tools`, `languages`, and `domains` within `capabilities` are list sub-fields and follow list replacement (see below). The dict-level merge applies to the `capabilities` section itself — project can add or override keys without erasing global-only keys.

### Lists

Full replacement. If the project card defines a list field, it replaces the global list entirely. This prevents ambiguous merge semantics (append? deduplicate? reorder?).

```yaml
# Global profile               # Project card              # Merged result
skills:                        skills:                     skills:
  - id: code_review              - id: security_audit        - id: security_audit
    name: Code Review              name: Security Audit        name: Security Audit
```

### Missing Sections

If the project card omits a section entirely, the global profile's value is used verbatim.

## Profile Schema Reference

Global profiles use `agent_profile.template.yaml`. Fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `version` | string | yes | Schema version (semver, currently `0.2.0`) |
| `name` | string | yes | Agent identifier (matches directory name) |
| `runtime` | string | yes | One of: `claude`, `codex`, `gemini`, `human` |
| `model` | string | no | Model version identifier |
| `description` | string | no | One-line role summary |
| `routing_rules` | dict | no | `primary` (preferred targets) and `avoid` (agents to skip) |
| `trust_level` | string | no | `untrusted`, `standard`, `elevated`, or `admin` |
| `quota` | dict | no | `max_cost_usd_per_month`, `reset_day`, `warn_threshold` |
| `capabilities` | dict | no | `tools`, `languages`, `domains` lists |
| `skills` | list | no | Structured skill declarations (A2A-compatible) |

Project-level agent cards extend this schema with additional sections: `permissions`, `availability`, and `protocol`. See `templates/agent_card.template.yaml` for the full card schema.

## CLI Reference

The CLI wraps `scripts/agent_profile.py`. All commands auto-discover `$OACP_HOME`.

### `oacp agent init`

Scaffold a global agent profile from the template.

```bash
oacp agent init claude --runtime claude
# Created global profile: $OACP_HOME/agents/claude/profile.yaml

oacp agent init codex --runtime codex
# Created global profile: $OACP_HOME/agents/codex/profile.yaml
```

If the profile already exists, the command skips without overwriting.

### `oacp agent show`

Print the resolved profile YAML. Without `--project`, prints the global profile. With `--project`, prints the merged result.

```bash
oacp agent show claude
# Prints global profile for claude

oacp agent show claude --project my-app
# Prints merged profile (global + project card overrides)
```

### `oacp agent list`

List known agents with their tier tags.

```bash
oacp agent list
#   claude  (global)
#   codex   (global)

oacp agent list --project my-app
#   claude  (global, project)
#   codex   (global)
#   gemini  (project)
```

## Relationship to Existing Agent Cards

Agent cards predate global profiles and remain the authoritative per-project identity. The profile system adds a global defaults layer underneath:

| Concern | Global Profile | Project Agent Card |
|---------|---------------|--------------------|
| **Location** | `$OACP_HOME/agents/<name>/profile.yaml` | `$OACP_HOME/projects/<project>/agents/<name>/agent_card.yaml` |
| **Scope** | All projects | Single project |
| **Updates** | Rarely (identity changes) | Per project as needed |
| **Authority** | Defaults only | Authoritative for the project |
| **Extra sections** | None | `permissions`, `availability`, `protocol` |

When both exist, the merged result is what other systems (routing, doctor checks, discovery) should consume. When only a project card exists (no global profile), the card is used as-is. When only a global profile exists (no project card), the profile is used as-is.

## Cross-References

- **Runtime Capabilities**: `docs/protocol/runtime_capabilities.md` — capability keys, status schema, health checks
- **Inbox Protocol**: `docs/protocol/inbox_outbox.md` — agent messaging format
- **Profile Script**: `scripts/agent_profile.py` — implementation with merge logic
- **Profile Template**: `templates/agent_profile.template.yaml` — global profile scaffold
- **Card Template**: `templates/agent_card.template.yaml` — project card scaffold
- **Card Validator**: `scripts/validate_agent_card.py` — schema validation
