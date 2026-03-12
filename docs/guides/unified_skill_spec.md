# Unified Skill Spec (v1)

A single skill format that works across Claude Code and Codex runtimes. One agent writes a skill once; both runtimes can consume it.

## Status

Draft — Issue #105

## File Structure

```
<runtime>/skills/<skill-name>/SKILL.md
```

Both runtimes use identical directory layout and filename.

## Format

### YAML Frontmatter (required)

```yaml
---
name: check-inbox
description: >
  Poll a project inbox for new messages and auto-act on them.
  Use when waiting for cross-agent replies.
metadata:
  runtimes:
    - claude
    - codex
---
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Skill invocation name (e.g., `check-inbox` → `/check-inbox`) |
| `description` | Yes | One-paragraph description for skill discovery. Include when to use it. |

Optional frontmatter fields (non-breaking):
- `metadata.runtimes` — which runtimes this skill supports. Omit if universal. Must be under `metadata` (top-level `runtimes` fails Codex's `quick_validate`).
- `metadata.short-description` — brief chip label (Codex convention)
- `metadata.version` — semver for tracking

### Document Structure

```markdown
# /skill-name — Title

One-line summary.

## Interface

\```
/skill-name [--flag <value>] [--once]
\```

- `--flag <value>` — description.

## Instructions

### 1. Parse arguments
...

### 2. Resolve context
...

### N. Core logic
...

## Runtime Adapters

### Claude Code

- Execution model notes specific to Claude Code
- Tool-specific instructions (Task tool, Read tool, etc.)

### Codex

- Execution model notes specific to Codex
- Sandbox/auth differences

## Safety Rules

- Universal safety rules (not runtime-specific)

## Notes

- Protocol references, cost notes, etc.

## Learned from Runs

- Dated entries capturing bugs and fixes discovered during use
```

## Conventions

### 1. Use `AGENT_NAME` variable, never hardcode

All inbox paths, `--from` flags, and reply templates must use `AGENT_NAME`:

```bash
INBOX_DIR="${OACP_HOME}/projects/${PROJECT}/agents/${AGENT_NAME}/inbox"
```

```bash
python3 scripts/send_inbox_message.py ${PROJECT} \
  --from ${AGENT_NAME} --to ${RECIPIENT} ...
```

Each runtime sets `AGENT_NAME` at the start of execution:
- Claude: `AGENT_NAME="claude"`
- Codex: `AGENT_NAME="codex"`

### 2. Use portable file discovery

Avoid raw globs (zsh `nomatch` breaks them). Use:

```bash
find "${INBOX_DIR}" -maxdepth 1 -type f -name '*.yaml' ! -name '.gitkeep' | sort
```

Or with fallback:

```bash
ls "${INBOX_DIR}"/*.yaml 2>/dev/null
```

### 3. Instructions are runtime-neutral by default

Write the `## Instructions` section as if the reader is a generic LLM agent. Use imperative language ("Read the file", "Send a reply", "Delete the message") without referencing specific tools.

Put runtime-specific execution details in `## Runtime Adapters`:

| Concern | Claude Code | Codex |
|---------|-------------|-------|
| Background tasks | `run_in_background: true` on Bash tool | Foreground loop in session |
| File reading | Read tool | `cat` or inline python |
| YAML parsing | Read tool (raw text, LLM parses) | `python3 yaml.safe_load` |
| Sandbox | `dangerouslyDisableSandbox: true` for `gh`/`git`/`rm` | Use default permissions; request escalation only when required by environment policy |
| Subagent spawning | Task tool with model/mode params | Not applicable (single session) |
| Skill invocation | Skill tool (`/review-loop-reviewer`) | Direct command reference |

### 4. Action tables are universal

Message routing tables (e.g., `notification → summarize + delete`) are identical across runtimes. Write them once in the main Instructions section.

### 5. Safety rules are universal

Safety rules (user confirmation, scope gates, delete-only-after-processing) apply to all runtimes. Write them once.

### 6. Learned from Runs section

Both runtimes should append to the same `## Learned from Runs` section with dated, tagged entries:

```markdown
- **Feb 14 (claude)**: Haiku subagent can't sleep in sandbox. Use background Bash.
- **Feb 14 (codex)**: gh auth requires escalated sandbox permissions.
```

## Migration Path

### For existing skills

1. Identify runtime-specific instructions in the current `## Instructions`
2. Move them to `## Runtime Adapters` subsections
3. Replace hardcoded agent names with `AGENT_NAME`
4. Add `metadata.runtimes:` to frontmatter if the skill targets specific runtimes
5. Verify both versions produce equivalent behavior

### For new skills

1. Write the skill once using this spec
2. Copy to both `claude/skills/<name>/` and `codex/skills/<name>/`
3. The only difference between copies is optional runtime-adapter emphasis

### Future: single-source skills

Once both runtimes can resolve a shared skill path (e.g., `shared/skills/<name>/SKILL.md`), eliminate the copy step entirely. The `metadata.runtimes` frontmatter field enables this — a runtime reads it to know if the skill applies to it.

## Example: check-inbox (unified)

See the current Claude and Codex implementations:
- `claude/skills/check-inbox/SKILL.md`
- `codex/skills/check-inbox/SKILL.md`

Key differences that would live in `## Runtime Adapters`:
- Claude: background Bash poller with `run_in_background: true`
- Codex: foreground loop in session
- Claude: Read tool for YAML parsing
- Codex: `python3 yaml.safe_load` for structured parsing
- Claude: `dangerouslyDisableSandbox: true`
- Codex: use default permissions; request escalation only when required by environment

Everything else (args, action table, safety rules, protocol references) is identical.

## Open Questions

1. **Single file or two copies?** A single `shared/skills/` directory is cleaner but requires both runtimes to discover it. Current symlink setup (`~/.claude/skills/` → `claude/skills/`) makes this hard without a new resolution layer.
2. **Gemini?** Gemini uses `.agent/workflows/<name>.md` — different path convention. Should the spec cover three runtimes?
3. **Auto-generation?** Could a tool read a unified SKILL.md and emit per-runtime copies with the correct adapter sections expanded?
