# Cross-Runtime Parity Matrix

**Date**: 2026-04-17

This is a capability comparison across 3 agent runtimes (Claude Code, Codex, Gemini), compiled from each runtime's self-report and current runtime changelogs. Use this as a reference when deciding which agent to assign for a given task.

Codex was last checked against the official Codex changelog entries for app update `26.415` (2026-04-16) and `Codex CLI 0.121.0` (2026-04-15).

---

## 1. Core Capability Matrix

| Capability             | Claude (Claude Code CLI)                                                   | Codex (Desktop App)                                                                            | Gemini                                                                |
| ---------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Spawn background tasks | Yes — Task tool + Bash `run_in_background`                                 | Yes — shell background processes                                                               | Yes — `run_command` async mode                                        |
| Spawn subagents        | Yes — typed agents (Explore, Plan, general-purpose, code-reviewer, etc.)   | Yes — native `spawn_agent` lifecycle with `default`, `explorer`, and `worker` agents          | Partial — `browser_subagent` only                                     |
| Parallel agent teams   | Yes — TeamCreate, task lists, SendMessage, broadcast                       | Partial — parallel spawned agents are supported, but there is no team/task-list primitive      | No — parallel tool calls but no independent agent instances           |
| MCP tools              | Yes — extensible via MCP servers                                           | Yes — MCP/plugin support depends on configured servers and marketplaces                        | Yes — MCP server support                                              |
| Web search             | Yes — native WebSearch tool                                                | Yes — web search/fetch tools                                                                   | Yes — native `search_web` tool                                        |
| Browser interaction    | Partial — WebFetch (read-only, HTML→markdown)                              | Partial — web tools plus early in-app browser for local/public pages without sign-in           | Yes — full browser control (click, type, navigate, screenshot, video) |
| File system access     | Sandboxed — configurable read/write allowlists                             | Policy-dependent per session                                                                   | Full — unrestricted                                                   |
| Git operations         | Yes — via Bash (may need sandbox configuration)                            | Yes — native                                                                                   | Yes — via shell                                                       |
| GitHub CLI (gh)        | Yes — via Bash (may need sandbox configuration)                            | Yes — authenticated                                                                            | Yes — native                                                          |
| Session memory         | Strong — auto-loaded MEMORY.md + optional MCP memory                       | Partial — app memories where available plus OACP file memory; app memories are not protocol SSOT | Partial — Knowledge Items (not directly writable), conversation logs  |
| Interactive mode       | Yes — CLI chat with permissions, plan mode                                 | Yes — desktop app                                                                              | Yes — chat with task UI, artifacts                                    |
| Context window         | ~1M with Opus 4.7 (auto-compaction extends indefinitely)                   | Not directly exposed; practically finite                                                       | ~1M tokens                                                            |
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
| App-level computer use           | Codex         | macOS app, simulator, and GUI-only workflows; unavailable in EEA, UK, and Switzerland at launch |
| App-level artifact review        | Codex         | Sidebar preview for generated PDFs, spreadsheets, documents, and presentations |
| App-level PR review              | Codex         | PR sidebar can inspect changed files, review comments, and follow-up fixes   |

---

## 3. Public OACP Skills Coverage

Scope: skills shipped in [`kiloloop/oacp-skills`](https://github.com/kiloloop/oacp-skills). Private/local skills (debrief, sync, blitz, team-stats, worktree-workflow, send-message, etc.) are intentionally not tracked here — this table is meant as a cross-runtime parity signal for distributable skills only.

| Skill                  | Claude  | Codex    | Gemini       |
| ---------------------- | ------- | -------- | ------------ |
| `check-inbox`          | Working | Working  | Not packaged |
| `doctor`               | Working | Working  | Not packaged |
| `review-loop-reviewer` | Working | Working  | Not packaged |
| `review-loop-author`   | Working | Working  | Not packaged |
| `self-improve`         | Working | Working  | Not packaged |

---

## 4. Strengths Summary

| Dimension       | Claude                                                                 | Codex                                                                   | Gemini                                                               |
| --------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Best at         | Orchestration, multi-agent teams, persistent memory, plan-then-execute | Terminal-native execution, fast iterative patching, app-assisted PR/artifact review, protocol discipline | Web research, browser automation, visual verification, large context |
| Ideal task type | Team coordination, complex multi-file refactors, long-running sessions | Shell-heavy workflows, targeted file edits, deterministic scripts, PR follow-up, artifact review | External research, UI testing, document review, MCP integrations     |
| Cost profile    | Flexible (haiku subagents for cheap tasks, opus for complex)           | Not directly visible                                                    | Token-based, web search has additional costs                         |

---

## 5. Known Limitations Summary

| Limitation                    | Claude                        | Codex                                                          | Gemini                 |
| ----------------------------- | ----------------------------- | -------------------------------------------------------------- | ---------------------- |
| No subagents                  | —                             | Yes                                                            | Partial (browser only) |
| No browser automation         | Yes (read-only)               | Partial (in-app browser is not full automation and excludes sign-in flows) | —                      |
| No image generation           | Yes                           | Yes                                                            | —                      |
| No persistent writable memory | —                             | Partial (app memories are not a replacement for OACP durable memory) | Yes                    |
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
| Full browser automation gap | Claude, Codex                      | Claude is read-only; Codex has early browser review but not full automation or sign-in flows | Delegate full browser tasks to Gemini; use Codex in-app browser for local/public page review |
| Reviewer cost               | All (especially Claude)            | High cost for single PR review with polling pattern                | Stateless reviewer rounds — one round per invocation                           |
| Public skill coverage       | Gemini                             | `kiloloop/oacp-skills` ships `claude/` and `codex/` variants for all 5 public skills; no `gemini/` variants — Gemini users must rely on convention-based adoption | Add `gemini/` variants to each public skill, or document the convention-based pattern as a first-class install path |

---

## 7. Additional Dimensions

| Dimension                    | Claude                           | Codex                                      | Gemini                                        |
| ---------------------------- | -------------------------------- | ------------------------------------------ | --------------------------------------------- |
| Max parallel tool calls      | ~10+                             | Yes (parallel independent calls)           | ~10 (practical)                               |
| Hooks system                 | Yes (pre/post tool call hooks)   | No                                         | No                                            |
| Automation scheduling        | Yes (`CronCreate`, `ScheduleWakeup`, `/loop`, `/schedule` skills) | Yes (desktop app thread automations wake a thread on a schedule, with user request) | No                                            |
| Notebook editing             | Yes (NotebookEdit tool)          | No                                         | No                                            |
| PDF reading                  | Yes (max 20 pages/request)       | No native tool                             | Via `read_url_content`                        |
| Image reading (multimodal)   | Yes                              | Yes (desktop app local image/view support) | Yes                                           |
| Artifact system              | No                               | Yes (sidebar preview for generated files) | Yes (task.md, implementation plans)           |
| Video recording              | No                               | No                                         | Yes (WebP via browser)                        |
| Multi-file editing primitive | Edit tool (one file at a time)   | `apply_patch` (one file)                   | `multi_replace_file_content` (non-contiguous) |
| Workflow file format         | SKILL.md with YAML frontmatter   | SKILL.md with YAML frontmatter             | Markdown with YAML frontmatter                |
| Policy visibility at runtime | Partial (sandbox config visible) | Yes (session policy in system context)     | Yes (`SafeToAutoRun` flags)                   |
| Long-running shell sessions  | Bash tool (no stdin)             | Yes (PTY + stdin; multiple terminals in app) | Yes (`send_command_input`)                    |

---

*Each runtime should update only its own column. Discrepancies should be resolved by the runtime owner.*
