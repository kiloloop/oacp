# Agent Safety Defaults

Cross-agent baseline safety rules. All agents (Claude, Codex, Gemini) follow these defaults unless a project-level `AGENTS.md` explicitly overrides a specific rule.

## Git Safety

- **No push/deploy without explicit approval** from the user or dispatcher.
- **No destructive commands** unless explicitly requested: `push --force`, `reset --hard`, `branch -D`, `clean -f`, `checkout .` (discarding changes).
- **No direct-to-main pushes** — all changes require a PR, regardless of size.
- **No hook bypasses** (`--no-verify`, `--no-gpg-sign`) unless explicitly requested.
- **New commits over amend** — after a pre-commit hook failure, fix the issue and create a new commit rather than amending the previous one.

## Staging Hygiene

- **Stage only files relevant to the current task** — no `git add .` or `git add -A`.
- **No secrets or credentials in commits** — never stage `.env`, `credentials.json`, API keys, tokens, or similar files.
- **Verify before commit** — run `git status` and `git diff --staged` before every commit to confirm only intended files are staged.

## Inbox Safety

- **Delete inbox messages immediately after processing.** For messages that require a reply (`task_request`, `question`, `review_request`), "processed" means reply sent. For `notification` messages, "processed" means read and acknowledged — no reply needed, delete right away. Do not let processed messages accumulate in the inbox.
- **Never silently consume a message that expects a response** — `task_request`, `question`, and `review_request` always require a reply. `notification` is no-response by default, but requires ack/reply when it reports findings or issues.
- **Always reply** using `oacp send` with `--parent-message-id` to maintain threading.
- **Idempotent sends** — before sending a response message (e.g., `review_feedback`, `review_addressed`, `review_request`), check the recipient's inbox and your own outbox for an already-sent message matching the same PR + round + type. Skip the send if a duplicate exists. This prevents retry storms when an agent crashes after sending a message but before completing the full workflow, and is re-invoked.

## Credential Safety

Follow `credential_scoping.md` in this directory for:
- Token scope and lifetime rules
- Per-agent credential boundaries
- Rotation and revocation procedures

## Scope Discipline

- **Do not edit files outside the requested scope** — if a task says "fix X in file Y", do not refactor file Z.
- **Do not modify auth, config, or secrets** without explicit approval from the user or dispatcher.
- **Do not install packages or change dependencies** unless the task requires it and approval is given.
- **Do not create files unnecessarily** — prefer editing existing files over creating new ones.

## Applying These Defaults

Agent config files (`CLAUDE.md`, `AGENTS.md`) should reference this document rather than restating these rules. Agent-specific additions (e.g., worktree preferences, tool preferences) belong in the agent config.

```
# Example reference in an agent config:
## Safety Defaults
Follow `docs/protocol/agent_safety_defaults.md`.
```
