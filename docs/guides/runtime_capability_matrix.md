# Cross-Runtime Parity Matrix

**Date**: 2026-03-16

This is a capability comparison across 3 agent runtimes (Claude Code, Codex, Gemini), compiled from each runtime's self-report. Use this as a reference when deciding which agent to assign for a given task.

---

## 1. Core Capability Matrix

| Capability             | Claude (Claude Code CLI)                                                   | Codex (Desktop App)                                                                            | Gemini                                                                |
| ---------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Spawn background tasks | Yes — Task tool + Bash `run_in_background`                                 | Yes — shell background processes                                                               | Yes — `run_command` async mode                                        |
| Spawn subagents        | Yes — typed agents (Explore, Plan, general-purpose, code-reviewer, etc.)   | Yes — native `spawn_agent` lifecycle with `default`, `explorer`, and `worker` agents          | Partial — `browser_subagent` only                                     |
| Parallel agent teams   | Yes — TeamCreate, task lists, SendMessage, broadcast                       | Partial — parallel spawned agents are supported, but there is no team/task-list primitive      | No — parallel tool calls but no independent agent instances           |
| MCP tools              | Yes — extensible via MCP servers                                           | Partial — APIs available, no servers configured                                                | Yes — MCP server support                                              |
| Web search             | Yes — native WebSearch tool                                                | Yes — web search/fetch tools                                                                   | Yes — native `search_web` tool                                        |
| Browser interaction    | Partial — WebFetch (read-only, HTML→markdown)                              | Partial — web fetch/open/click flow, not full automation                                       | Yes — full browser control (click, type, navigate, screenshot, video) |
| File system access     | Sandboxed — configurable read/write allowlists                             | Policy-dependent per session                                                                   | Full — unrestricted                                                   |
| Git operations         | Yes — via Bash (may need sandbox configuration)                            | Yes — native                                                                                   | Yes — via shell                                                       |
| GitHub CLI (gh)        | Yes — via Bash (may need sandbox configuration)                            | Yes — authenticated                                                                            | Yes — native                                                          |
| Session memory         | Strong — auto-loaded MEMORY.md + optional MCP memory                       | Partial — conversation context + manual file-based memory; no built-in persistent memory layer | Partial — Knowledge Items (not directly writable), conversation logs  |
| Interactive mode       | Yes — CLI chat with permissions, plan mode                                 | Yes — desktop app                                                                              | Yes — chat with task UI, artifacts                                    |
| Context window         | ~200k (auto-compaction extends indefinitely)                               | Not directly exposed; practically finite                                                       | ~1M tokens                                                            |
| Cost model             | Token-based, visible in statusline                                         | Not surfaced in runtime                                                                        | Token-based                                                           |
| Sandbox restrictions   | Yes — configurable allowlists                                              | Session-dependent                                                                              | None — full system access                                             |

---

## 2. Unique Capabilities (Only One Runtime Has)

| Capability                       | Runtime       | Details                                                                      |
| -------------------------------- | ------------- | ---------------------------------------------------------------------------- |
| Typed subagent orchestration     | Claude        | Multiple agent types with scoped tools and model selection                   |
| Team coordination primitive      | Claude        | TeamCreate + task lists + assignment + broadcast + shutdown                  |
| Plan mode                        | Claude        | Structured explore → plan → approve → implement workflow                     |
| Auto-compaction                  | Claude        | Context auto-compresses, enabling unlimited session length                   |
| Cross-session semantic search    | Claude        | MCP-based searchable memory (optional)                                       |
| Browser automation (full)        | Gemini        | Click, type, navigate, screenshot, WebP video recording                      |
| Image generation                 | Gemini        | Native `generate_image` tool                                                 |
| URL content reading (no browser) | Gemini        | `read_url_content` fetches HTML→markdown or PDF directly                     |
| Code outline navigation          | Gemini        | `view_file_outline`, `view_code_item` for structured exploration             |
| PTY / terminal stdin             | Codex, Gemini | Codex: native PTY; Gemini: `send_command_input` (Claude lacks stdin support) |
| `apply_patch` editing            | Codex         | Grammar-based file edits                                                     |
| Automation scheduling            | Codex         | Desktop app can schedule tasks (with user request)                           |

---

## 3. Installed Skills/Workflows Comparison

| Skill                  | Claude  | Codex    | Gemini               |
| ---------------------- | ------- | -------- | -------------------- |
| `review-loop-reviewer` | Working | Working  | Working              |
| `review-loop-author`   | Working | Untested | Working              |
| `check-inbox`          | Working | Working  | Working (convention) |
| `debrief`              | Working | Working  | Working              |
| `sync`                 | Working | Untested | N/A                  |
| `blitz`                | Working | N/A      | N/A                  |
| `team-stats`           | Working | N/A      | N/A                  |
| `worktree-workflow`    | Working | N/A      | N/A                  |
| `send-message`         | Working | N/A      | N/A                  |
| `claude-mem` skills    | Working | N/A      | N/A                  |
| `gh-address-comments`  | N/A     | Untested | N/A                  |
| `lint-and-validate`    | N/A     | Untested | N/A                  |
| `beads`                | N/A     | Untested | N/A                  |

---

## 4. Strengths Summary

| Dimension       | Claude                                                                 | Codex                                                                   | Gemini                                                               |
| --------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Best at         | Orchestration, multi-agent teams, persistent memory, plan-then-execute | Terminal-native execution, fast iterative patching, protocol discipline | Web research, browser automation, visual verification, large context |
| Ideal task type | Team coordination, complex multi-file refactors, long-running sessions | Shell-heavy workflows, targeted file edits, deterministic scripts       | External research, UI testing, document review, MCP integrations     |
| Cost profile    | Flexible (haiku subagents for cheap tasks, opus for complex)           | Not directly visible                                                    | Token-based, web search has additional costs                         |

---

## 5. Known Limitations Summary

| Limitation                    | Claude                        | Codex                                                          | Gemini                 |
| ----------------------------- | ----------------------------- | -------------------------------------------------------------- | ---------------------- |
| No subagents                  | —                             | Yes                                                            | Partial (browser only) |
| No browser automation         | Yes (read-only)               | Partial                                                        | —                      |
| No image generation           | Yes                           | Yes                                                            | —                      |
| No persistent writable memory | —                             | Partial (no built-in persistent memory, file-based workaround) | Yes                    |
| Sandbox friction              | Yes (configurable)            | Session-dependent                                              | —                      |
| No team primitive             | —                             | Yes                                                            | Yes                    |
| Context limits                | Auto-compaction mitigates     | Yes (no compaction)                                            | Large but finite       |
| No terminal stdin             | Yes                           | —                                                              | —                      |
| Cost not surfaced             | —                             | Yes                                                            | —                      |

---

## 6. Parity Gaps — Actionable Items

These are the highest-impact gaps where one runtime's limitation blocks effective collaboration:

| Gap                         | Affected Runtime(s)                | Impact                                                             | Proposed Fix                                                                   |
| --------------------------- | ---------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| No team orchestration       | Codex, Gemini                      | Cannot run parallel agent teams                                    | Agent cards — let runtimes discover and delegate to capable peers              |
| Memory asymmetry            | Codex (partial), Gemini (KIs only) | Cross-session context degrades without MEMORY.md equivalent        | Standardize memory protocol; each runtime implements its own persistence layer |
| Sandbox blocks git/gh       | Claude                             | Every git/gh call needs sandbox configuration                      | Configure sandbox allowlists or disable sandbox for specific commands          |
| No browser for Claude/Codex | Claude, Codex                      | Cannot visually verify UIs or do browser-based testing             | Delegate browser tasks to Gemini; or add MCP browser server                    |
| Reviewer cost               | All (especially Claude)            | High cost for single PR review with polling pattern                | Stateless reviewer rounds — one round per invocation                           |
| Skill parity                | Codex, Gemini                      | Many skills are Claude-only (blitz, team-stats, worktree-workflow) | Unified skill spec + install guide per runtime                                 |

---

## 7. Additional Dimensions

| Dimension                    | Claude                           | Codex                                      | Gemini                                        |
| ---------------------------- | -------------------------------- | ------------------------------------------ | --------------------------------------------- |
| Max parallel tool calls      | ~10+                             | Yes (parallel independent calls)           | ~10 (practical)                               |
| Hooks system                 | Yes (pre/post tool call hooks)   | No                                         | No                                            |
| Notebook editing             | Yes (NotebookEdit tool)          | No                                         | No                                            |
| PDF reading                  | Yes (max 20 pages/request)       | No native tool                             | Via `read_url_content`                        |
| Image reading (multimodal)   | Yes                              | Yes (desktop app local image/view support) | Yes                                           |
| Artifact system              | No                               | No                                         | Yes (task.md, implementation plans)           |
| Video recording              | No                               | No                                         | Yes (WebP via browser)                        |
| Multi-file editing primitive | Edit tool (one file at a time)   | `apply_patch` (one file)                   | `multi_replace_file_content` (non-contiguous) |
| Workflow file format         | SKILL.md with YAML frontmatter   | SKILL.md with YAML frontmatter             | Markdown with YAML frontmatter                |
| Policy visibility at runtime | Partial (sandbox config visible) | Yes (session policy in system context)     | Yes (`SafeToAutoRun` flags)                   |
| Long-running shell sessions  | Bash tool (no stdin)             | Yes (PTY + stdin)                          | Yes (`send_command_input`)                    |

---

*Each runtime should update only its own column. Discrepancies should be resolved by the runtime owner.*
