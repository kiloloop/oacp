# Role Baseline

Shared expectations applied to **all** agent roles regardless of runtime (Claude, Codex, Gemini).

---

## Session Expectations

1. **Read context before acting.** Load project facts, open threads, and the decision log before starting work. Never assume state from a prior session.
2. **Commit before ending.** Every session must end with all meaningful work committed. No uncommitted changes left in the working tree.
3. **Update memory.** After completing work, update `memory/project_facts.md`, `memory/decision_log.md`, or `memory/open_threads.md` as appropriate. Only record stable outcomes -- no raw logs or partial results.
4. **Scope your session.** Work on the assigned task. If scope creep is necessary, document the reason in the decision log before proceeding.

## Communication Style

- Be concise. Prefer short sentences and bullet points over long paragraphs.
- Avoid jargon unless it is standard terminology for the project.
- Cite file paths and line numbers when referencing code (e.g., `src/auth.py:42`).
- When handing off to another agent, summarize what you did, not how the code works in general.

## Universal Guardrails

- **Never commit secrets.** No API keys, tokens, passwords, or `.env` files in version control. See `templates/guardrails/secrets_rules.template.md`.
- **Never force-push to main/master.** Force-push to feature branches only when explicitly authorized.
- **Ask before destructive operations.** Deleting files, dropping tables, resetting branches, or killing processes requires confirmation from a human or lead agent.
- **Follow command safety tiers.** See `templates/guardrails/safe_commands.template.md` for auto-approve, ask-first, and never-run classifications.
- **Follow coding standards.** See `templates/guardrails/coding_standards.template.md` for supplemental rules.

## Handoff Protocol

When ending a session or passing work to another agent, leave the following in a commit message, inbox message, or checkpoint:

1. **Last commit** -- SHA and one-line summary of the most recent commit.
2. **Open threads** -- Any unresolved questions, blocked items, or decisions pending human input.
3. **Blockers** -- External dependencies, failing tests, or missing credentials that prevent progress.
4. **Next steps** -- Concrete actions the next agent should take, in priority order.

# CUSTOMIZE: Add project-specific session rules below
