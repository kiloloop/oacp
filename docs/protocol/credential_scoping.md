# Credential Scoping Protocol

## Purpose

Define per-agent, per-project fine-grained token boundaries so that agents operate under least-privilege credentials instead of sharing a single `gh` auth session.

## Scope Model

### Per-Agent Credentials

Each agent (claude, codex, gemini) receives its own set of credentials. No two agents share the same token for the same service. This provides:

- **Audit trail** — API logs attribute actions to the correct agent.
- **Blast-radius containment** — a leaked token only compromises one agent's access.
- **Independent rotation** — rotating one agent's token does not disrupt others.

### Per-Project Boundaries

Credentials are scoped to the project they serve. An agent working on project A must not use project A's tokens to access project B's resources. When an agent participates in multiple projects, it holds separate credentials per project.

## Credential Types

| Type | Env Var Pattern | Example | Scope |
|------|----------------|---------|-------|
| GitHub token | `GH_TOKEN_<AGENT>` | `GH_TOKEN_CLAUDE` | Repo-level or org-level PAT |
| Anthropic API key | `ANTHROPIC_API_KEY_<AGENT>` | `ANTHROPIC_API_KEY_CLAUDE` | Per-agent usage tracking |
| OpenAI API key | `OPENAI_API_KEY_<AGENT>` | `OPENAI_API_KEY_CODEX` | Per-agent usage tracking |
| Webhook secrets | `WEBHOOK_SECRET_<SERVICE>` | `WEBHOOK_SECRET_DISCORD` | Per-project notification channels |
| SSH deploy keys | `SSH_KEY_<AGENT>_<REPO>` | `SSH_KEY_CODEX_MYREPO` | Per-agent, per-repo |

## Storage Rules

1. **Environment variables only.** Credentials are loaded from env vars at runtime. Never store credential values in config files, scripts, YAML, or any version-controlled file.
2. **Reference by name, not value.** In documentation, messages, logs, and commit messages, refer to credentials by their env var name (e.g., "the `GH_TOKEN_CLAUDE` variable"), never by their actual value.
3. **No shared credential files.** Each agent process inherits its own environment. Do not use a shared `.env` file that multiple agents source.
4. **Secret managers for production.** For long-lived deployments, use a secret manager (AWS Secrets Manager, HashiCorp Vault, 1Password CLI) rather than plain env vars.

## Scoping Rules (Least Privilege)

### GitHub Tokens

- Use fine-grained personal access tokens (PATs) with repository-scoped permissions.
- Grant only the permissions the agent needs:
  - **Implementer agents** (claude, codex): `contents: write`, `pull_requests: write`, `issues: write` on assigned repos.
  - **QA agents** (gemini): `contents: read`, `pull_requests: read`, `issues: write` (for comments/labels only).
  - **Poll daemon**: `pull_requests: read`, `issues: read` — read-only is sufficient for polling.
- Never grant `admin`, `delete`, or `organization` permissions to agent tokens.

### API Keys

- Use separate API keys per agent so usage and costs are attributable.
- If the provider supports it, apply rate limits or spending caps per key.

### Webhook Secrets

- Webhook URLs that embed tokens (e.g., Discord webhook URLs) are treated as secrets.
- Each project should have its own webhook endpoints; do not reuse webhook URLs across projects.

## Credential Rotation

Agents must support credential rotation without downtime:

1. **New credential is provisioned** and set in the agent's environment (e.g., via secret manager update or env var change).
2. **Agent picks up the new credential** on next invocation. Since agent processes are short-lived (headless runs triggered by the poll daemon), restarting is not required — the next poll cycle will use the updated env.
3. **Old credential is revoked** after confirming the new one works (verify at least one successful API call).
4. **Rotation is logged** in the project's `memory/decision_log.md` with the date and which credential was rotated (by name, not value).

### Rotation Frequency

- GitHub tokens: rotate at least every 90 days, or immediately if a token may have been exposed.
- API keys: rotate when an agent is decommissioned, when team membership changes, or on a regular cadence per organizational policy.
- Webhook secrets: rotate when a webhook URL is suspected of leaking.

## Integration with background review automation

Background review automation often starts with the ambient `gh` CLI auth (whatever `gh auth status` returns). To support scoped credentials:

### Per-Agent Token Selection

Per-agent GitHub tokens can be configured in the automation environment:

```bash
# Per-agent GitHub tokens (fine-grained PATs)
# These override the ambient gh auth for the respective runtime.
# Leave empty to fall back to the default gh auth.
GH_TOKEN_CLAUDE=""
GH_TOKEN_CODEX=""
GH_TOKEN_GEMINI=""
```

### Runtime Behavior

When the automation selects a runtime for a PR branch, it should also select the corresponding token:

1. Determine the runtime (`claude`, `codex`, or fallback).
2. Look up `GH_TOKEN_<RUNTIME>` (uppercased) from environment or secret manager.
3. If set, export `GH_TOKEN=<value>` for the subprocess. The `gh` CLI respects `GH_TOKEN` as an override for its auth.
4. If not set, fall back to the ambient `gh` auth (backward-compatible).

This means each headless agent run uses the token scoped to that agent, even though the automation process itself may run under a different identity.

### Validation

Before running a task, the dispatcher should validate that the scoped token has sufficient permissions:

```bash
GH_TOKEN="$agent_token" gh api repos/$OWNER/$REPO --jq '.permissions'
```

If the token lacks required permissions, log an error and skip the task (do not fall back to a higher-privilege token silently).

## Example: Fully Scoped Setup

```bash
# Environment variables or secret manager injection for the automation process

# Claude's token — can push to <YOUR_ORG>/<repo> only
export GH_TOKEN_CLAUDE="github_pat_..."

# Codex's token — can push to <YOUR_ORG>/<repo> only
export GH_TOKEN_CODEX="github_pat_..."

# Gemini's token — read-only on the same repo
export GH_TOKEN_GEMINI="github_pat_..."

# API keys for headless model invocations
export ANTHROPIC_API_KEY_CLAUDE="sk-ant-..."
export OPENAI_API_KEY_CODEX="sk-..."
```

## Migration Path

Adopting credential scoping is incremental:

1. **Phase 1 (current):** Shared `gh` auth. All agents use the same token. This is the default and requires no configuration changes.
2. **Phase 2:** Set `GH_TOKEN_<AGENT>` in the automation environment for agents that need isolation. The automation uses scoped tokens when available, falls back to shared auth otherwise.
3. **Phase 3:** Enforce scoping — the automation refuses to run if a scoped token is not configured for the selected runtime. This is opt-in via a `REQUIRE_SCOPED_TOKENS=true` config flag.

## Related Documents

- `templates/guardrails/secrets_rules.template.md` — general secrets handling rules
