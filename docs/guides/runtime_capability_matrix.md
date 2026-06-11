# Cross-Runtime Parity Matrix

**Date**: 2026-06-09

This is a capability comparison across the currently profiled agent runtimes (Claude Code, Codex, Gemini), compiled from each runtime's self-report and current runtime changelogs. Cursor support is scaffold-only until Cursor-owned onboarding lands, so Cursor is intentionally excluded from this comparison table; see `docs/protocol/runtime_capabilities.md` for its conservative scaffold defaults.

Claude was last checked against Claude Code `v2.1.170` with Claude Fable 5 (`claude-fable-5`, serving model verified in-session on the 1M-context variant). Fable 5 (released 2026-06-09, first Mythos-class model) is included at no extra cost on Pro/Max/Team/Enterprise plans Jun 9–22, 2026, with usage credits required after; Opus 4.8 remains available and serves as Fable 5's safeguard-fallback model.

Codex was last checked against app update `26.602`, CLI `0.137.0`, OpenAI's GPT-5.5 launch note from 2026-04-23, and the June 2026 Codex/API entries for Sites and Amazon Bedrock.

---

## 1. Core Capability Matrix

| Capability             | Claude (Claude Code CLI)                                                   | Codex (Desktop App)                                                                            | Gemini                                                                |
| ---------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Spawn background tasks | Yes — Task tool + Bash `run_in_background`                                 | Yes — shell background processes                                                               | Yes — `run_command` async mode                                        |
| Spawn subagents        | Yes — typed agents (Explore, Plan, general-purpose, code-reviewer, etc.)   | Yes — native multi-agent lifecycle with runtime metadata and follow-up defaults                | Partial — `browser_subagent` only                                     |
| Parallel agent teams   | Yes — TeamCreate, task lists, SendMessage, broadcast                       | Partial — parallel spawned agents are supported, but there is no team/task-list primitive      | No — parallel tool calls but no independent agent instances           |
| MCP tools              | Yes — extensible via MCP servers                                           | Yes — MCP/plugin support with `/mcp verbose`, per-server environment targeting, read-only MCP parallelism, and scriptable plugin inventory | Yes — MCP server support                                              |
| Web search             | Yes — native WebSearch tool                                                | Yes — web search/fetch tools; hosted web tools are expanding in code-mode flows                | Yes — native `search_web` tool                                        |
| Browser interaction    | Partial — WebFetch (read-only, HTML→markdown)                              | Partial — in-app browser and Chrome extension can inspect local/public and approved browser contexts, with faster asset extraction and read-only JS structured-data extraction; not full browser automation | Yes — full browser control (click, type, navigate, screenshot, video) |
| File system access     | Sandboxed — configurable read/write allowlists                             | Policy-dependent per session; named permission profiles can include deny-read rules and managed requirements | Full — unrestricted                                                   |
| Git operations         | Yes — via Bash (may need sandbox configuration)                            | Yes — native                                                                                   | Yes — via shell                                                       |
| GitHub CLI (gh)        | Yes — via Bash (may need sandbox configuration)                            | Yes — authenticated                                                                            | Yes — native                                                          |
| Session memory         | Strong — auto-loaded MEMORY.md + optional MCP memory                       | Partial — app memories where available plus OACP file memory; app memories are not protocol SSOT | Partial — Knowledge Items (not directly writable), conversation logs  |
| Interactive mode       | Yes — CLI chat with permissions, plan mode                                 | Yes — desktop app and CLI/TUI, including Plan Mode, Goal mode, side conversations, and archive/unarchive flows | Yes — chat with task UI, artifacts                                    |
| Context window         | ~1M with Fable 5 or Opus 4.8 (auto-compaction extends indefinitely)        | Model-dependent; GPT-5.5 in Codex is documented at 400K, with no auto-compaction guarantee      | ~1M tokens                                                            |
| Cost model             | Token-based, visible in statusline; Fable 5 API rate is $10/$50 per MTok (2× Opus 4.8's $5/$25) | Not surfaced per session; GPT-5.5 Fast mode trades 2.5x cost for 1.5x token generation speed   | Token-based                                                           |
| Sandbox restrictions   | Yes — configurable allowlists                                              | Session-dependent; supports deny-read policies, isolated `codex exec`, named permission profiles, managed requirements, and explicit approval policies | None — full system access                                             |

---

## 2. Distinctive Capabilities

| Capability                       | Runtime       | Details                                                                      |
| -------------------------------- | ------------- | ---------------------------------------------------------------------------- |
| Typed subagent orchestration     | Claude        | Multiple agent types with scoped tools and model selection                   |
| Team coordination primitive      | Claude        | TeamCreate + task lists + assignment + broadcast + shutdown                  |
| Dynamic multi-agent workflows    | Claude        | Workflow tool orchestrates tens–hundreds of agents; `/workflows` to view     |
| Plan mode                        | Claude, Codex | Claude has structured explore → plan → approve → implement; Codex CLI can move from planning into fresh-context implementation |
| Auto-compaction                  | Claude        | Context auto-compresses, enabling unlimited session length                   |
| Cross-session semantic search    | Claude        | MCP-based searchable memory (optional)                                       |
| Browser automation (full)        | Gemini        | Click, type, navigate, screenshot, WebP video recording                      |
| GPT-5.5 model availability       | Codex         | Available in Codex for Plus, Pro, Business, Enterprise, Edu, and Go plans with a 400K context window |
| Image generation                 | Codex, Gemini | Codex CLI image generation is enabled by default; Gemini has native `generate_image` |
| URL content reading (no browser) | Gemini        | `read_url_content` fetches HTML→markdown or PDF directly                     |
| Code outline navigation          | Gemini        | `view_file_outline`, `view_code_item` for structured exploration             |
| PTY / terminal stdin             | Codex, Gemini | Codex: native PTY; Gemini: `send_command_input` (Claude lacks stdin support) |
| `apply_patch` editing            | Codex         | Grammar-based file edits                                                     |
| App-level computer use           | Codex         | macOS app, simulator, and GUI-only workflows; unavailable in EEA, UK, and Switzerland at launch |
| Windows computer use             | Codex         | Codex app can operate Windows desktop apps in the foreground when available                    |
| Remote host control              | Codex         | Mobile or desktop remote control can run work on connected Mac or Windows hosts with host-local files, credentials, plugins, skills, and config |
| App-level artifact review        | Codex         | Sidebar preview for generated PDFs, spreadsheets, documents, and presentations |
| App-level PR review              | Codex         | PR sidebar can inspect changed files, review comments, and follow-up fixes   |
| App-server automation            | Codex         | JSON-RPC app-server, SDK, schema generation, thread APIs, and websocket/Unix-socket transports for custom clients |
| Hosted site deployment           | Codex         | Sites preview can create, deploy, inspect, and manage hosted websites or internal tools through the Codex app |
| Plugin marketplace inventory     | Codex         | Plugin directory plus `codex plugin list --json` for installed plugin inventory and marketplace-aware diagnostics |
| Goal mode                        | Codex         | Stable long-running objective mode with dedicated state; candidate for OACP wait/review-loop experiments |

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
| Best at         | Orchestration, multi-agent teams, persistent memory, plan-then-execute | Terminal-native execution, GPT-5.5 agentic coding, fast iterative patching, plan-to-implementation handoff, app-assisted PR/artifact review, app-server automation, protocol discipline | Web research, browser automation, visual verification, large context |
| Ideal task type | Team coordination, complex multi-file refactors, long-running sessions | Shell-heavy workflows, long-horizon coding, targeted file edits, deterministic scripts, PR follow-up, artifact review, CLI planning passes, plugin/app-server automation prototypes | External research, UI testing, document review, MCP integrations     |
| Cost profile    | Flexible (haiku subagents for cheap tasks, opus for complex, Fable 5 at 2× Opus API rates for the hardest work) | Per-session cost not visible; GPT-5.5 is described as more token-efficient than GPT-5.4 for Codex tasks | Token-based, web search has additional costs                         |

---

## 5. Known Limitations Summary

| Limitation                    | Claude                        | Codex                                                          | Gemini                 |
| ----------------------------- | ----------------------------- | -------------------------------------------------------------- | ---------------------- |
| No subagents                  | —                             | Yes                                                            | Partial (browser only) |
| No browser automation         | Yes (read-only)               | Partial (in-app browser and Chrome extension are useful for review/verification, but not a general full-browser automation substitute) | —                      |
| No image generation           | Yes                           | —                                                              | —                      |
| No persistent writable memory | —                             | Partial (app memories are not a replacement for OACP durable memory) | Yes                    |
| Sandbox friction              | Yes (configurable)            | Session-dependent                                              | —                      |
| No team primitive             | —                             | Yes                                                            | Yes                    |
| Context limits                | Auto-compaction mitigates     | Model-dependent; GPT-5.5 in Codex is 400K, but there is no documented auto-compaction behavior | Large but finite       |
| No terminal stdin             | Yes                           | —                                                              | —                      |
| Cost not surfaced             | —                             | Yes                                                            | —                      |
| Serving model can change mid-session | Yes (Fable 5 only — cyber/bio-chem/distillation classifiers fall back to Opus 4.8; default and non-configurable in Claude interfaces incl. Claude Code, with a session event emitted; <5% of sessions — system card §1.5) | — | — |

---

## 6. Parity Gaps — Actionable Items

These are the highest-impact gaps where one runtime's limitation blocks effective collaboration:

| Gap                         | Affected Runtime(s)                | Impact                                                             | Proposed Fix                                                                   |
| --------------------------- | ---------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| No team orchestration       | Codex, Gemini                      | Cannot run parallel agent teams                                    | Agent cards — let runtimes discover and delegate to capable peers              |
| Memory asymmetry            | Codex (partial), Gemini (KIs only) | Cross-session context degrades without MEMORY.md equivalent        | Standardize memory protocol; each runtime implements its own persistence layer |
| Sandbox blocks git/gh       | Claude                             | Every git/gh call needs sandbox configuration                      | Configure sandbox allowlists or disable sandbox for specific commands          |
| Full browser automation gap | Claude, Codex                      | Claude is read-only; Codex has stronger browser review and Chrome-extension support but not full autonomous browser automation | Delegate full browser tasks to Gemini; use Codex browser/Chrome workflows for local, public, or approved signed-in page review |
| Reviewer cost               | All (especially Claude)            | High cost for single PR review with polling pattern                | Stateless reviewer rounds — one round per invocation                           |
| Public skill coverage       | Gemini                             | `kiloloop/oacp-skills` ships `claude/` and `codex/` variants for all 5 public skills; no `gemini/` variants — Gemini users must rely on convention-based adoption | Add `gemini/` variants to each public skill, or document the convention-based pattern as a first-class install path |

---

## 7. Additional Dimensions

| Dimension                    | Claude                           | Codex                                      | Gemini                                        |
| ---------------------------- | -------------------------------- | ------------------------------------------ | --------------------------------------------- |
| Max parallel tool calls      | ~10+                             | Yes (parallel independent calls)           | ~10 (practical)                               |
| Side conversations           | No                               | Yes (`/side` in CLI/TUI)                   | No                                            |
| Hooks system                 | Yes (pre/post tool call hooks)   | Yes (stable hooks and extension lifecycle hooks; plugin-bundled hooks are configuration-dependent) | No                                            |
| Automation scheduling        | Yes (`CronCreate`, `ScheduleWakeup`, `/loop`, `/schedule` skills) | Yes (desktop app thread automations, Goal mode, and app-server/SDK automation surfaces) | No                                            |
| Notebook editing             | Yes (NotebookEdit tool)          | No                                         | No                                            |
| PDF reading                  | Yes (max 20 pages/request)       | No native tool                             | Via `read_url_content`                        |
| Image reading (multimodal)   | Yes                              | Yes (desktop app local image/view support) | Yes                                           |
| Artifact system              | No                               | Yes (sidebar preview for generated files) | Yes (task.md, implementation plans)           |
| Video recording              | No                               | No                                         | Yes (WebP via browser)                        |
| Image generation             | No                               | Yes (enabled by default in CLI)            | Yes                                           |
| MCP diagnostics              | Partial                          | Yes (`/mcp verbose`, per-server environment targeting, read-only MCP parallelism, plugin JSON inventory) | Partial                                       |
| Multi-file editing primitive | Edit tool (one file at a time)   | `apply_patch` (one file)                   | `multi_replace_file_content` (non-contiguous) |
| Workflow file format         | SKILL.md with YAML frontmatter   | SKILL.md with YAML frontmatter             | Markdown with YAML frontmatter                |
| Policy visibility at runtime | Partial (sandbox config visible) | Yes (session policy, approval policy, sandbox, and named permission profiles) | Yes (`SafeToAutoRun` flags)                   |
| Long-running shell sessions  | Bash tool (no stdin)             | Yes (PTY + stdin; multiple terminals in app) | Yes (`send_command_input`)                    |
| App-server / SDK             | No                               | Yes (JSON-RPC app-server, Python SDK, archive/thread APIs, schema generation) | No                                            |
| Hosted site deployment       | No                               | Yes (Sites preview, app-only/cloud-hosted with separate secret management) | No                                            |

---

## 8. Source Notes

- GPT-5.5 Codex availability, 400K context, Fast mode, token-efficiency, and API timing come from OpenAI's 2026-04-23 release note: <https://openai.com/index/introducing-gpt-5-5/>.
- Codex app/CLI capability changes through app `26.602` and CLI `0.137.0` come from OpenAI's Codex changelog: <https://developers.openai.com/codex/changelog>.
- Sites, Amazon Bedrock, app-server, plugin, and permissions details come from the official Codex docs under <https://developers.openai.com/codex/>.
- Claude Fable 5 release date, pricing, and plan-inclusion window come from Anthropic's 2026-06-09 announcement: <https://www.anthropic.com/news/claude-fable-5-mythos-5>. Safeguard-fallback behavior comes from the Fable 5 / Mythos 5 system card §1.5 ("Novel safeguards"): client apps and Claude interfaces auto-fall back to Opus 4.8 (default and non-configurable in interfaces, session event emitted), while the Messages API blocks by default with a structured refusal category and offers opt-in server-side fallback. The serving model in the header was verified in-session by the Claude runtime.

---

*Each runtime should update only its own column. Discrepancies should be resolved by the runtime owner.*
