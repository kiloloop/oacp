# Cross-Runtime Knowledge Sync Protocol

## Problem

Multi-agent workflows span different runtimes — Claude, Codex, Gemini — each with its own context window, memory mechanism, and conversation state. Without an explicit sync protocol, agents lose context at handoff boundaries, duplicate decisions, or contradict prior work.

Key challenges:

- **Context window isolation**: Each agent session starts fresh. Prior decisions exist only in external artifacts.
- **Heterogeneous memory**: Claude uses `CLAUDE.md` + memory files; Codex uses `AGENTS.md` + memory; Gemini relies on conversation history and system prompts.
- **Handoff gaps**: When Agent A hands off to Agent B, critical context (why a decision was made, what was tried and failed) is often lost.

## Sync Mechanisms

The protocol defines three complementary sync mechanisms, ordered from most durable to most ephemeral.

### 1. Durable Memory Files

**Location**: `projects/<project>/memory/`

| File | Purpose | Updated by |
|------|---------|------------|
| `project_facts.md` | Agent roles, repo structure, architecture, conventions | Any agent via the project's durable-memory promotion flow |
| `decision_log.md` | Timestamped decisions with rationale | Any agent via the project's durable-memory promotion flow |
| `open_threads.md` | Unresolved issues, blocked epics, cross-agent coordination | Any agent via the project's durable-memory promotion flow |

These files are the **source of truth** for stable project knowledge. All runtimes read them at session start. Only verified, stable outcomes should be written here.

**Promotion flow**: Merge decisions contain a "Durable Memory Updates" section. Each implementation should provide a promotion mechanism that extracts approved entries from merge artifacts and appends them to the appropriate memory file, deduplicating against existing content.

### 2. Handoff Messages with Context Keys

**Location**: `agents/<agent>/inbox/`

When handing off work between agents (especially across runtimes), the sender includes `context_keys` in the handoff message — a concise summary of decisions made, artifacts produced, and open questions.

See [Conversation Threading](inbox_outbox.md#conversation-threading) for field details.

Context keys bridge the gap between the sender's rich conversational context and the recipient's cold start. They should include:

- Decisions made and their rationale
- Artifacts produced (PRs, files, packets)
- Open questions or blockers
- What was tried and did not work

### 3. Packet-Based Review Artifacts

**Location**: `packets/review/`, `packets/findings/`, `merges/`

Review packets, findings packets, and merge decisions form a structured audit trail. Agents entering a review cycle can read the packet history to understand what was reviewed, what issues were found, and how they were resolved.

These artifacts are especially useful for cross-runtime sync because they follow a fixed schema that any runtime can parse.

## Sync Points

Agents synchronize knowledge at well-defined points in the workflow:

| Sync Point | Action | Direction |
|------------|--------|-----------|
| **Session start** | Read `memory/project_facts.md`, `decision_log.md`, `open_threads.md` | Memory -> Agent |
| **Task completion** | Write stable outcomes to memory via merge decision + durable-memory promotion flow | Agent -> Memory |
| **Handoff** | Include `conversation_id` + `context_keys` in handoff message | Agent -> Agent |
| **Review cycle start** | Read relevant packet history | Packets -> Agent |
| **PR merge** | Update memory files if the change affects project conventions or architecture | Agent -> Memory |

## Runtime-Specific Notes

### Claude

- **Reads**: `CLAUDE.md` (project-level instructions, auto-loaded), `memory/` files (read at session start or on demand)
- **Writes**: Memory files via merge decisions + durable-memory promotion, handoff messages, review packets
- **Context mechanism**: `CLAUDE.md` is injected into every conversation. Memory files are read explicitly.
- **Tip**: Keep `CLAUDE.md` under 200 lines. Move detailed notes to memory files and reference them.

### Codex

- **Reads**: `AGENTS.md` (similar role to `CLAUDE.md`), `memory/` files
- **Writes**: Same artifacts as Claude
- **Context mechanism**: `AGENTS.md` is loaded at session start. Codex sessions are more ephemeral — handoff context keys are especially important.
- **Tip**: Codex works best with explicit, self-contained task descriptions. Include all necessary context in the handoff message rather than referencing external files.

### Gemini

- **Reads**: Conversation history (persistent within a session), memory files, system prompts
- **Writes**: Same artifacts as Claude and Codex
- **Context mechanism**: Gemini maintains richer in-session history but loses it across sessions. Handoff messages with context keys compensate.
- **Tip**: When Gemini hands off to another runtime, it should export conversation highlights into `context_keys` rather than assuming the recipient can access Gemini's conversation history.

## Anti-Patterns

Avoid these common mistakes when syncing knowledge across runtimes:

| Anti-Pattern | Why It Fails | Do Instead |
|--------------|-------------|------------|
| Syncing raw conversation transcripts | Too verbose, runtime-specific formatting, wastes context window | Distill into `context_keys` or memory entries |
| Syncing ephemeral state (temp files, debug logs, partial results) | Clutters memory, confuses future agents | Only promote stable, verified outcomes |
| Assuming shared context | Agent B cannot read Agent A's conversation history | Always include context in handoff messages |
| Writing to memory too eagerly | Unverified or in-progress work pollutes the knowledge base | Wait until merge decision to promote |
| Skipping memory reads at session start | Agent makes decisions contradicting prior work | Always read memory files before starting work |

## Integration with durable-memory promotion

Each project should provide a durable-memory promotion mechanism that:

1. Scans merge decisions or equivalent terminal artifacts
2. Extracts entries from the "Durable Memory Updates" section
3. Appends new entries to `decision_log.md`, `open_threads.md`, or `project_facts.md`
4. Deduplicates against existing content

This ensures that knowledge flows from ephemeral review artifacts into durable memory that persists across sessions and runtimes, without requiring a specific helper script name.
