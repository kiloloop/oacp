# Prompt Caching Patterns

How to maximize prompt cache hits across Claude, Codex, and Gemini to reduce cost and latency.

## Why It Matters

Prompt caching avoids reprocessing static context (system prompts, CLAUDE.md, project facts) on every turn. In practice:
- **Cache read**: 10x cheaper than uncached input ($1.50/MTok vs $15/MTok for Opus)
- **Cache write**: 1.25x input price (one-time cost, amortized over subsequent reads)
- **Observed savings**: 83-95% cost reduction on cached input in multi-turn agent sessions

## Claude

### Automatic — No Setup Required

Prompt caching is **enabled by default** for all Claude API usage. Every Claude Code session, headless run, and agent team benefits automatically — there is nothing to opt in to or configure.

How it works:
- The API caches the longest common prefix of your prompt across requests
- In a Claude Code session, the system prompt + CLAUDE.md + tool definitions form a stable prefix
- After turn 1, all subsequent turns read this prefix from cache instead of reprocessing it
- Cache TTL is ~5 minutes of inactivity, auto-extended on every hit
- A typical multi-turn session sees **90%+ cache hit rate** out of the box

### Maximizing Cache Hits

The default behavior already handles the common case. These tips help squeeze out the remaining savings:

1. **Keep static context at the top of the prompt**
   - System prompt, CLAUDE.md, project_facts.md, and tool definitions are loaded first
   - These rarely change within a session → high cache hit rate

2. **Front-load stable context in CLAUDE.md**
   - Put repo structure, conventions, and protocol rules early
   - Put volatile content (open threads, recent decisions) in separate files loaded later

3. **Batch agent work into focused sessions**
   - A 20-turn session on one task reuses cache across all turns
   - Switching tasks mid-session may invalidate cache if the prefix changes

### Explicit Cache Control (Custom API Integrations Only)

When building custom integrations with the Messages API (not Claude Code CLI), you can explicitly mark content blocks for caching. This is **not needed** for Claude Code — it handles caching automatically.

```json
{
  "role": "user",
  "content": [
    {
      "type": "text",
      "text": "<large stable context>",
      "cache_control": {"type": "ephemeral"}
    }
  ]
}
```

Use this when you have a large reference document mid-conversation that isn't part of the natural prefix (e.g., a full API spec injected as a user message).

### Headless Claude automation

When running headless Claude sessions (for example, via a polling daemon or CI-triggered agent invocations), maximize caching by:
- Loading skill files and project context as system prompt (cached after first call)
- Processing multiple PRs in sequence within one invocation to share the same cache
- Keeping poll intervals short enough to stay within cache TTL (~5 min)

### Multi-Agent Teams

When spawning parallel agents via Claude Code teams:
- Each agent gets its own session → **separate cache** per agent
- Shared CLAUDE.md and tool definitions still cache within each agent's session
- Observed: 94% cache hit rate across 3 parallel agents (workflow-templates run)

## Codex

### How It Works

Codex CLI sends the full prompt to the Codex API on each invocation. There is no built-in cross-turn prompt cache like Claude's.

### Context Reuse Strategies

1. **Keep prompts short and focused**
   - Codex charges per input token with no cache discount
   - Include only the files and context directly relevant to the task

2. **Use `--file` flags to scope context**
   ```bash
   codex --file src/auth.py --file tests/test_auth.py "Fix the token expiration bug"
   ```

3. **Batch related fixes in one invocation**
   - `codex exec` processes all instructions in a single session
   - Avoids re-sending the same context for each fix

4. **Lean on diff-only context**
   - For PR fix loops, pass the diff + findings rather than full file contents
   - `run_agent_task.sh` already scopes context to changed files

## Gemini

### How It Works

Gemini supports implicit context caching for large prompts. The API automatically caches prompts above a size threshold.

### Context Reuse Strategies

1. **Use cached content API for large stable context**
   ```
   POST /cachedContents
   {
     "model": "models/gemini-2.5-pro",
     "contents": [{"role": "user", "parts": [{"text": "<stable context>"}]}],
     "ttl": "600s"
   }
   ```

2. **Reference cached content in subsequent requests**
   ```
   POST /generateContent
   {
     "cachedContent": "cachedContents/abc123",
     "contents": [{"role": "user", "parts": [{"text": "New instruction"}]}]
   }
   ```

3. **Gemini CLI sessions** — context is maintained within a session automatically; no explicit caching needed for interactive use.

## Cost Comparison

Approximate pricing as of early 2026 (per million tokens). Check each provider's current pricing page for up-to-date rates:

| Runtime | Input | Cached Read | Cache Write | Output |
|---------|------:|------------:|------------:|-------:|
| Claude Opus | $15.00 | $1.50 | $18.75 | $75.00 |
| Claude Sonnet | $3.00 | $0.30 | $3.75 | $15.00 |
| Claude Haiku | $0.80 | $0.08 | $1.00 | $4.00 |
| Codex | varies | N/A | N/A | varies |
| Gemini Pro | $1.25 | $0.31 | — | $10.00 |

## Practical Guidelines

1. **Measure your cache hit rate** — check `cache_read_input_tokens` vs `input_tokens` in API responses or team stats output
2. **Target >80% cache hit rate** for multi-turn sessions — if lower, your prefix is changing too often
3. **Don't over-optimize** — the biggest savings come from the default behavior (CLAUDE.md + tools cached automatically)
4. **Watch for cache-busting patterns**:
   - Injecting timestamps or random IDs into system prompts
   - Reordering tool definitions between turns
   - Changing the user message prefix frequently
