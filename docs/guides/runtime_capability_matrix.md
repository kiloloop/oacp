# Cross-Runtime Parity Matrix

**Date**: 2026-08-07

This is a capability comparison across the currently profiled agent runtimes (Claude Code, Codex, Gemini), compiled from each runtime's self-report and current runtime changelogs. Cursor support is scaffold-only until Cursor-owned onboarding lands, so Cursor is intentionally excluded from this comparison table; see `docs/protocol/runtime_capabilities.md` for its conservative scaffold defaults.

Claude was last checked against Claude Code `v2.1.225` (verified in-session 2026-08-07 via `claude --version`) with Claude Fable 5 (`claude-fable-5`, serving model verified in-session on the 1M-context variant). Fable 5 (released 2026-06-09, first Mythos-class model) is now included in Max and Team Premium plan usage as part of the shared weekly limit pool (the launch-window free-inclusion/credit period has ended); the API rate is $10/$50 per MTok. Claude Opus 5 (`claude-opus-5`, released 2026-07-24) is the current Opus tier at $5/$25 per MTok with the same 1M context; Opus 4.8 remains available at the same price and remains the safeguard-fallback target for Fable 5's classifier fallbacks (unchanged by the Opus 5 release).

Codex was last checked against app update `26.727`, stable CLI `0.146.0`, GPT-5.6, and the OpenAI Codex/API changelog entries through 2026-07-30. Runtime availability remains configuration-dependent: standard multi-agent support and memories are stable, while multi-agent V2, token budgets, current-time reminders, and remote Code Mode may still require explicit enablement or experimental configuration.

---

## 1. Core Capability Matrix

| Capability             | Claude (Claude Code CLI)                                                   | Codex (Desktop App)                                                                            | Gemini                                                                |
| ---------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Spawn background tasks | Yes — Task tool + Bash `run_in_background`                                 | Yes — shell background processes                                                               | Yes — `run_command` async mode                                        |
| Spawn subagents        | Yes — typed agents (Explore, Plan, general-purpose, code-reviewer, etc.)   | Yes — stable native multi-agent lifecycle with runtime metadata and follow-up controls         | Partial — `browser_subagent` only                                     |
| Parallel agent teams   | Yes — TeamCreate, task lists, SendMessage, broadcast                       | Partial — parallel delegation is supported; opt-in multi-agent V2 adds configurable models, reasoning, concurrency, and roles, but there is no shared team/task-list primitive | No — parallel tool calls but no independent agent instances           |
| MCP tools              | Yes — extensible via MCP servers                                           | Yes — MCP/plugin support with `/mcp verbose`, per-server environment targeting, read-only MCP parallelism, scriptable plugin inventory, and default tool-search exposure where supported | Yes — MCP server support                                              |
| Web search             | Yes — native WebSearch tool                                                | Yes — web search/fetch tools; hosted web tools are expanding in code-mode flows                | Yes — native `search_web` tool                                        |
| Browser interaction    | Partial — WebFetch (read-only, HTML→markdown)                              | Configuration-dependent — Browser and Chrome plugins can navigate, click, type, inspect, and capture approved contexts; history search remains separately permissioned | Yes — full browser control (click, type, navigate, screenshot, video) |
| File system access     | Sandboxed — configurable read/write allowlists                             | Policy-dependent per session; named permission profiles can include deny-read rules and managed requirements | Full — unrestricted                                                   |
| Multi-folder projects  | Manual workspace composition                                               | Yes — the primary folder controls new chats, Git, and automatic instruction/skill/config discovery; secondary folders provide file access | Manual workspace composition                                          |
| Git operations         | Yes — via Bash (may need sandbox configuration)                            | Yes — native; in a multi-folder project, operations are rooted in the primary folder           | Yes — via shell                                                       |
| GitHub CLI (gh)        | Yes — via Bash (may need sandbox configuration)                            | Yes — authenticated                                                                            | Yes — native                                                          |
| Session memory         | Strong — auto-loaded MEMORY.md + optional MCP memory                       | Partial — stable generated app memories plus OACP file memory; app memories are not protocol SSOT | Partial — Knowledge Items (not directly writable), conversation logs  |
| Interactive mode       | Yes — CLI chat with permissions, plan mode                                 | Yes — desktop app and CLI/TUI, including Plan and Goal modes, named and pinned sessions, side-chat switching, forks, and archive/unarchive/delete flows | Yes — chat with task UI, artifacts                                    |
| Context window         | ~1M across the current lineup (Fable 5, Opus 5, Opus 4.8); auto-compaction extends indefinitely | Model-dependent; GPT-5.6 Sol, Terra, and Luna are documented at 272K                           | ~1M tokens                                                            |
| Cost model             | Token-based, visible in statusline; Fable 5 API rate is $10/$50 per MTok (2× the $5/$25 shared by Opus 5 and Opus 4.8) | ChatGPT sessions do not surface per-session cost; API Fast mode for Sol trades 2× price for up to 2.5× speed, while Terra and Luna target lower-cost work | Token-based                                                           |
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
| Cross-session messaging          | Claude        | SendMessage reaches the user's other Claude Code sessions, including across machines (macOS/Linux), with ListAgents discovery; inbound delivery is configurable (`crossSessionInbound`, `dialogExpiry`) and messages can initiate conversations with Remote Control sessions by name |
| Browser automation              | Codex, Gemini | Codex Browser/Chrome plugins can click, type, navigate, inspect, and capture approved contexts; Gemini also supports WebP video recording |
| GPT-5.6 model family            | Codex         | Sol, Terra, and Luna provide task-tier choices with a documented 272K context window |
| Image generation                 | Codex, Gemini | Codex CLI image generation is enabled by default; Gemini has native `generate_image` |
| URL content reading (no browser) | Gemini        | `read_url_content` fetches HTML→markdown or PDF directly                     |
| Code outline navigation          | Gemini        | `view_file_outline`, `view_code_item` for structured exploration             |
| PTY / terminal stdin             | Codex, Gemini | Codex: native PTY; Gemini: `send_command_input` (Claude lacks stdin support) |
| `apply_patch` editing            | Codex         | Grammar-based file edits                                                     |
| App-level computer use           | Codex         | macOS app, simulator, and GUI-only workflows; unavailable in EEA, UK, and Switzerland at launch |
| Windows computer use             | Codex         | Codex app can operate Windows desktop apps in the foreground when available                    |
| Remote host control              | Codex         | Mobile or desktop remote control can run work on connected Mac or Windows hosts with host-local files, credentials, plugins, skills, and config; Claude Code's cross-session messaging now also reaches its own Remote Control sessions on other machines, narrowing this distinction |
| App-level artifact review        | Codex         | Sidebar preview for generated PDFs, spreadsheets, documents, and presentations |
| App-level PR review              | Codex         | PR sidebar can inspect changed files, review comments, and follow-up fixes   |
| Multi-folder projects            | Codex         | One project can span repositories; the primary folder owns Git and automatic instruction, skill, and config discovery, while secondary folders are file-access-only |
| Session organization             | Codex         | CLI/TUI supports named and pinned sessions, side-chat switching, archive/unarchive, deletion, and temporary or persisted forks |
| App-server automation            | Codex         | JSON-RPC app-server, SDK, schema generation, thread APIs, and authenticated WebSocket/Unix-socket/stdio transports; remote Code Mode remains experimental |
| Hosted site deployment           | Codex         | Sites preview can create, deploy, inspect, and manage hosted websites or internal tools through the Codex app |
| Plugin marketplace inventory     | Codex         | Agent Plugin manifests, workspace publishing, additional marketplaces, and `codex plugin list --json`; availability and trust remain configuration-dependent |
| Goal mode                        | Codex         | Stable long-running objective mode with dedicated state; candidate for OACP wait/review-loop experiments |
| Record & Replay                  | Codex         | Mac desktop workflows can be recorded and converted into reusable skills; candidate for private skill capture after privacy review |
| Current-time reminders           | Codex         | Announced CLI surface for relative-date work; feature availability must be checked before workflow dependence |

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
| Best at         | Orchestration, multi-agent teams, persistent memory, plan-then-execute | Terminal-native execution, GPT-5.6 agentic coding, fast iterative patching, native delegation, plan-to-implementation handoff, app-assisted review, app-server automation, protocol discipline | Web research, browser automation, visual verification, large context |
| Ideal task type | Team coordination, complex multi-file refactors, long-running sessions | Shell-heavy workflows, long-horizon coding, targeted file edits, deterministic scripts, PR follow-up, artifact review, CLI planning passes, plugin/app-server automation prototypes | External research, UI testing, document review, MCP integrations     |
| Cost profile    | Flexible (haiku subagents for cheap tasks, opus for complex, Fable 5 at 2× Opus API rates for the hardest work) | Per-session ChatGPT cost not visible; Sol, Terra, and Luna provide quality/speed/cost tiers, with API Fast mode available for Sol | Token-based, web search has additional costs                         |

---

## 5. Known Limitations Summary

| Limitation                    | Claude                        | Codex                                                          | Gemini                 |
| ----------------------------- | ----------------------------- | -------------------------------------------------------------- | ---------------------- |
| No subagents                  | —                             | —                                                              | Partial (browser only) |
| No browser automation         | Yes (read-only)               | Configuration-dependent — Browser/Chrome plugins and an approved context are required | —                      |
| No image generation           | Yes                           | —                                                              | —                      |
| No persistent writable memory | —                             | Partial (app memories are not a replacement for OACP durable memory) | Yes                    |
| Sandbox friction              | Yes (configurable)            | Session-dependent                                              | —                      |
| No team primitive             | —                             | Partial — native parallel delegation exists, but not a shared task-list/broadcast primitive | Yes                    |
| Context limits                | Auto-compaction mitigates     | GPT-5.6 Sol, Terra, and Luna are 272K; compaction behavior is runtime-dependent | Large but finite       |
| No terminal stdin             | Yes                           | —                                                              | —                      |
| Cost not surfaced             | —                             | Yes                                                            | —                      |
| Permanent session delete      | —                             | `codex delete` is available; use archive/unarchive for routine cleanup and reserve delete for explicit destructive cleanup | —                      |
| Serving model can change mid-session | Yes (Fable 5 only — cyber/bio-chem/distillation classifiers fall back to Opus 4.8, a target unchanged by the Opus 5 release; enabled by default and user-configurable in Claude interfaces — off-toggle in Settings > Capabilities, or Config > MODEL & OUTPUT in Claude Code, after which a flagged request pauses instead of switching; a session event is emitted on switch; <5% of sessions) | — | — |

---

## 6. Parity Gaps — Actionable Items

These are the highest-impact gaps where one runtime's limitation blocks effective collaboration:

| Gap                         | Affected Runtime(s)                | Impact                                                             | Proposed Fix                                                                   |
| --------------------------- | ---------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| No shared team/task-list primitive | Codex, Gemini                | Codex can delegate in parallel but lacks Claude-style shared task lists and broadcast; Gemini lacks independent general agents | Agent cards — let runtimes discover and delegate to capable peers |
| Memory asymmetry            | Codex (partial), Gemini (KIs only) | Cross-session context degrades without MEMORY.md equivalent        | Standardize memory protocol; each runtime implements its own persistence layer |
| Sandbox blocks git/gh       | Claude                             | Every git/gh call needs sandbox configuration                      | Configure sandbox allowlists or disable sandbox for specific commands          |
| Full browser automation gap | Claude; Codex without Browser/Chrome plugins | Claude is read-only; Codex browser control depends on installed plugins and an approved browser context | Delegate to a browser-capable runtime or enable the scoped Codex browser surface after privacy review |
| Reviewer cost               | All (especially Claude)            | High cost for single PR review with polling pattern                | Stateless reviewer rounds — one round per invocation                           |
| Public skill coverage       | Gemini                             | `kiloloop/oacp-skills` ships `claude/` and `codex/` variants for all 5 public skills; no `gemini/` variants — Gemini users must rely on convention-based adoption | Add `gemini/` variants to each public skill, or document the convention-based pattern as a first-class install path |

---

## 7. Additional Dimensions

| Dimension                    | Claude                           | Codex                                      | Gemini                                        |
| ---------------------------- | -------------------------------- | ------------------------------------------ | --------------------------------------------- |
| Max parallel tool calls      | ~10+                             | Yes (parallel independent calls)           | ~10 (practical)                               |
| Side conversations           | No                               | Yes — named/pinned sessions and side-chat switching in CLI/TUI | No                                            |
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
| App-server / SDK             | No                               | Yes (JSON-RPC app-server, Python SDK, thread/fork APIs, authenticated WebSocket/Unix/stdio transports, and experimental remote Code Mode) | No                                            |
| Multi-folder projects        | No native primary-folder model   | Yes (primary folder owns Git and automatic instruction/config discovery; secondary folders are file-access-only) | No native primary-folder model                |
| Hosted site deployment       | No                               | Yes (Sites preview, app-only/cloud-hosted with separate secret management) | No                                            |
| Desktop workflow capture     | No                               | Yes (Record & Replay on Mac; privacy-sensitive and best treated as private-skill capture until reviewed) | No                                            |

---

## 8. Source Notes

- GPT-5.6 model availability, the corrected 272K context window, stable memories and multi-agent support, session organization, Agent Plugins, multi-folder behavior, browser/Chrome changes, and app-server transports come from OpenAI's Codex changelog: <https://developers.openai.com/codex/changelog>.
- Codex app/CLI capability changes are reviewed through app `26.727` and stable CLI `0.146.0`; workstation-specific alpha versions are intentionally kept out of this public matrix.
- GPT-5.6 API model and pricing changes through 2026-07-30 come from the OpenAI API changelog: <https://developers.openai.com/api/docs/changelog>.
- Sites, Amazon Bedrock, Remote, Record & Replay, app-server, plugin, and permissions details come from the official Codex docs under <https://developers.openai.com/codex/>.
- Claude Fable 5 release date and pricing come from Anthropic's 2026-06-09 announcement: <https://www.anthropic.com/news/claude-fable-5-mythos-5>; the current plan-inclusion posture (Max/Team Premium shared weekly pool, launch promo ended) reflects Anthropic's plan policy as of 2026-07-20. Claude Opus 5 model ID, pricing ($5/$25 per MTok), 1M context, and its release as the current Opus tier were re-verified 2026-08-03 against Anthropic's model documentation (models overview + migration guide at <https://platform.claude.com/docs/en/about-claude/models/overview.md>): Opus 5 is a drop-in at Opus 4.8's pricing, and Opus 4.8 stays available. Present-day interface fallback behavior comes from Anthropic's Help Center article "Why Claude switched models in your conversation with Fable 5" (2026-07-01, verified 2026-08-04): automatic switching to Opus 4.8 is enabled by default and user-configurable — Settings > Capabilities in the apps, Config > MODEL & OUTPUT in Claude Code ("Switch models when a message is flagged" toggle); with it off, a flagged request pauses the conversation. The Fable 5 / Mythos 5 system card §1.5 ("Novel safeguards") remains the source for the safeguard design, the observed fallback-rate/session-event facts, and the API posture: the Messages API blocks by default with a structured refusal category and offers opt-in server-side fallback — whose category-routed default likewise targets Opus 4.8 for cyber-class refusals, so the fallback target is unchanged post-Opus-5. The serving model (claude-fable-5) and Claude Code version (v2.1.225) in the header were verified in-session by the Claude runtime on 2026-08-07. Cross-session messaging capabilities (SendMessage across sessions and machines, ListAgents discovery, crossSessionInbound/dialogExpiry settings) come from the Claude Code v2.1.222–v2.1.225 release notes.

---

*Each runtime should update only its own column. Discrepancies should be resolved by the runtime owner.*
