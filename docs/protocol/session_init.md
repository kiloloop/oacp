# Session Init Protocol

Standardize the steps an agent performs at the start of each session. This protocol is runtime-agnostic — it defines the **what** and **order**. Per-runtime implementations (Claude Code, Codex, Gemini/Antigravity) live in their respective config files.

## Problem

Each agent runtime loads configuration differently. Without a standard init sequence:
- Agents miss project context and repeat prior decisions
- Memory files are read inconsistently (or not at all)
- Inbox messages pile up unprocessed
- Status files go stale, breaking dispatch routing
- New runtimes have no reference for what "ready" means

## Init Sequence

The init sequence has 6 steps in a fixed order. Each step is classified as **required** (must complete before the agent begins work) or **optional** (can be deferred or skipped based on runtime capabilities).

```
┌─────────────────────────────────────────────┐
│ 1. Load global rules            [required]  │
│ 2. Load project rules           [required]  │
│ 3. Load durable memory          [required]  │
│ 4. Check inbox                  [optional]  │
│ 5. Load skills / tools          [optional]  │
│ 6. Report status                [required]  │
└─────────────────────────────────────────────┘
```

### Step 1: Load Global Rules

**Required.** Load the agent's global instruction file — rules that apply across all projects.

| Runtime | File | Mechanism |
|---------|------|-----------|
| Claude | `~/.claude/CLAUDE.md` | Auto-loaded by Claude Code |
| Codex | Global `AGENTS.md` or system prompt | Configured in Codex settings |
| Gemini | System prompt or `.agent/rules/` | Configured per workspace |

**What belongs here:** Safety defaults, tool preferences, workflow conventions, sandbox workarounds, credential rules. These rarely change between projects.

**Cross-reference:** Global rules should reference `agent_safety_defaults.md` rather than restating its contents.

### Step 2: Load Project Rules

**Required.** Load project-scoped instructions — rules specific to this repository or workspace.

| Runtime | File | Mechanism |
|---------|------|-----------|
| Claude | `<repo>/CLAUDE.md` | Auto-loaded by Claude Code |
| Codex | `<repo>/AGENTS.md` | Auto-loaded by Codex |
| Gemini | `<repo>/GEMINI.md` or `.agent/rules/` | Loaded from workspace config |

**What belongs here:** Repo structure, key commands, build/test instructions, project-specific conventions, protocol references.

**Conflict resolution:** When global and project rules conflict, project rules win. Project rules are closer to the work and reflect repo-specific decisions. Exception: safety defaults from `agent_safety_defaults.md` cannot be relaxed by project rules — they can only be made stricter.

### Step 3: Load Durable Memory

**Required.** Read the project's shared memory files to restore cross-session and cross-runtime context.

**Location:** `$OACP_HOME/projects/<project>/memory/`

**Loading order:**

1. `project_facts.md` — agent roles, repo structure, architecture, conventions. Read first because it provides the mental model for everything else.
2. `decision_log.md` — timestamped decisions with rationale. Read second to understand what has been decided and why.
3. `open_threads.md` — unresolved issues, blocked epics, cross-agent coordination. Read last because it builds on the context from facts and decisions.

**Conflict resolution:** If memory files contradict project rules (e.g., `project_facts.md` says "use pytest" but `CLAUDE.md` says "use make test"), project rules win. Memory files may be stale; project rules are maintained alongside the code.

**Runtime-specific memory:** Some runtimes maintain additional memory (e.g., Claude's `~/.claude/projects/<hash>/memory/MEMORY.md`). Runtime-specific memory supplements but does not override shared durable memory. Load runtime-specific memory after shared memory.

**When files don't exist:** If the memory directory or files don't exist, skip gracefully. Not every project has been initialized with `init_project_workspace.sh`. The agent should note the absence but not fail.

### Step 4: Check Inbox

**Optional.** Check for pending messages in the agent's inbox.

**Location:** `$OACP_HOME/projects/<project>/agents/<agent_name>/inbox/`

**Action:**
- List YAML files in the inbox directory (excluding `.gitkeep` and `processed/` subdirectory)
- If messages exist, summarize count and types to the user
- Do NOT auto-process messages during init — let the user decide when to act on them

**Why optional:** Not all sessions involve multi-agent coordination. Solo work sessions (debugging, exploration, one-off questions) don't need inbox checks. Agents should check inbox when:
- The session involves cross-agent work
- The user explicitly asks (`/check-inbox`)
- A dispatcher or orchestrator has sent the agent a task

**Deferral behavior:** If skipped at init, the agent can check inbox at any point during the session. The inbox is durable — messages persist until explicitly deleted.

### Step 5: Load Skills / Tools

**Optional.** Load available skills, MCP servers, tools, or workflows.

| Runtime | Mechanism |
|---------|-----------|
| Claude | Skills from `~/.claude/skills/`, MCP servers from settings |
| Codex | Tools from workspace config |
| Gemini | Workflows from `.agent/workflows/`, tools from config |

**Why optional:** Skills are loaded on-demand in most runtimes. Claude Code discovers skills at startup automatically. Codex and Gemini may need explicit loading steps depending on configuration.

**Validation:** If a skill or tool fails to load (missing dependency, broken config), log a warning but don't block init. The agent can function without optional tools.

### Step 6: Report Status

**Required.** Update `status.yaml` to signal that the agent is online and ready.

**Location:** `$OACP_HOME/projects/<project>/agents/<agent_name>/status.yaml`

**Fields to set:**

```yaml
runtime: <runtime>           # claude | codex | gemini
model: <model_id>            # e.g., claude-opus-4-6
status: available             # or "busy" if starting with an assigned task
current_task: ""              # populated if starting with a task
capabilities:                 # from runtime_capabilities.md
  - headless
  - shell_access
  # ... (runtime-specific list)
updated_at: "<ISO 8601 UTC>"  # current timestamp
```

**When starting with a task:** If the agent was dispatched with a specific task (e.g., via inbox task_request or orchestrator), set `status: busy` and populate `current_task` immediately.

**Cross-reference:** See `runtime_capabilities.md` for the full status schema and canonical capability keys.

## Init Failure Handling

If a **required** step fails:

| Step | Failure mode | Action |
|------|-------------|--------|
| Global rules | File missing or unreadable | Warn user, continue with defaults (safety defaults still apply) |
| Project rules | File missing or unreadable | Warn user, continue — project may not have agent config |
| Durable memory | Directory or files missing | Log absence, continue — project may not be initialized |
| Report status | Cannot write status.yaml | Warn user — dispatch routing will be degraded but work can proceed |

No init failure should prevent the agent from starting. All failures are **degraded mode**, not hard blocks. The agent reports what failed and continues.

## Runtime-Specific Implementation Notes

This section provides guidance for per-runtime implementations.

### Claude Code

Steps 1-2 are handled automatically by Claude Code (CLAUDE.md loading). Step 3 requires explicit file reads or auto-memory. Step 4 uses `/check-inbox`. Step 5 is automatic (skill discovery). Step 6 requires a startup hook or explicit script call.

**Key gap:** Step 6 (status reporting) is not automated in Claude Code today. Options: (a) startup hook in `.claude/hooks/`, (b) `scripts/session_lifecycle_hooks.py init_session` at conversation start, (c) CLAUDE.md instruction to update status.yaml.

Example Claude Code hook config:

```json
{ "hooks": { "session-start": [{ "command": "python3 scripts/session_lifecycle_hooks.py --hub-dir \"$OACP_HOME\" init_session <project> --agent claude" }] } }
```

This example assumes `$OACP_HOME` is set in the Claude runtime environment.

### Codex

Steps 1-2 are handled by AGENTS.md loading. Steps 3 and 6 can be handled by `scripts/codex_session_init.py`, which loads protocol docs + durable memory and creates/updates `status.yaml`.

Run at session start:

```bash
python3 scripts/codex_session_init.py --project <project>
```

The script emits a deterministic acknowledgement payload suitable for first-response confirmation:

```text
SESSION_INIT_ACK: project=<project>;protocol=...;memory=...;status_yaml=...
```

**Current limitation:** `/check-inbox` integration is intentionally deferred so high-frequency inbox behavior does not change midstream. Session-start init is the required baseline.

### Gemini

Steps 1-2 are handled by system prompts and `.agent/rules/`. Steps 3-6 can be implemented via Antigravity workflows or startup scripts.

**Key gap:** Gemini's memory model is session-scoped. Durable memory reads (Step 3) must be explicit — there's no auto-loading mechanism.

## Anti-Patterns

| Anti-Pattern | Why It Fails | Do Instead |
|--------------|-------------|------------|
| Skipping memory reads to "save time" | Agent contradicts prior decisions, duplicates work | Always read memory files — they're small and fast |
| Auto-processing inbox during init | Surprises the user with unseen actions | Summarize inbox count, let user trigger processing |
| Writing status.yaml only at session close | Dispatch router thinks agent is offline during work | Write at init AND close (and task boundaries) |
| Loading all skills eagerly | Slows init, wastes resources for unused tools | Load on-demand; only validate critical tools at init |
| Hardcoding runtime-specific steps in protocol | Breaks when new runtimes are added | Keep protocol abstract; put runtime details in per-runtime docs |

## Cross-References

- **Safety Defaults**: `docs/protocol/agent_safety_defaults.md` — baseline safety rules loaded at Step 1
- **Inbox Protocol**: `docs/protocol/inbox_outbox.md` — message format for Step 4
- **Runtime Capabilities**: `docs/protocol/runtime_capabilities.md` — status schema and capability keys for Step 6
- **Cross-Runtime Sync**: `docs/protocol/cross_runtime_sync.md` — durable memory files loaded at Step 3
- **Session Lifecycle**: `scripts/session_lifecycle_hooks.py` — reference script for init/close hooks
