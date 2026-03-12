# Secrets Rules

Rules for handling secrets, credentials, and sensitive data. These apply to all agent roles.

---

## Never commit secrets

The following must never appear in version-controlled files:

- API keys, tokens, and passwords
- `.env` files or their contents
- Private keys (SSH, TLS, signing)
- Database connection strings with embedded credentials
- OAuth client secrets
- Webhook URLs that contain tokens

## Never log or print secret values

- Do not echo, print, or log the value of any secret, even for debugging.
- If you need to verify a secret is set, check for its existence (e.g., `test -n "$API_KEY"`), not its value.

## Reference names, not values

- In documentation, messages, and commit messages, refer to secrets by name (e.g., "the `DISCORD_WEBHOOK_URL` env var") rather than including the actual value.
- When describing configuration, show placeholder patterns: `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`

## Ensure .gitignore coverage

These patterns must be present in `.gitignore` for any project that uses secrets:

```
.env
.env.*
*.pem
*.key
credentials.json
secrets.yaml
```

# CUSTOMIZE: Add project-specific patterns to .gitignore as needed.

## Use environment variables or secret managers

- Load secrets from environment variables at runtime.
- For production, use a secret manager (e.g., AWS Secrets Manager, Vault, 1Password CLI).
- Never hardcode secret values in source files, config files, or scripts.

## Remediation

If a secret is accidentally committed:

1. Immediately rotate the compromised credential.
2. Remove the secret from the repository history (use `git filter-repo` or BFG Repo Cleaner).
3. Force-push the cleaned history (this is the one case where force-push to main may be justified -- coordinate with the team first).
4. Document the incident in the decision log.

## Credential Scoping

Agent credentials must follow the per-agent, per-project scoping rules defined in `docs/protocol/credential_scoping.md`. Key requirements:

- **Per-agent tokens**: each agent (claude, codex, gemini) gets its own credentials. Never share a token across agents.
- **Per-project boundaries**: credentials for project A must not be used to access project B.
- **Env var naming**: use the pattern `<TYPE>_<AGENT>` (e.g., `GH_TOKEN_CLAUDE`, `ANTHROPIC_API_KEY_CODEX`).
- **Least privilege**: grant only the permissions each agent's role requires (e.g., read-only for QA agents, write for implementers).
- **Rotation**: rotate tokens on a regular cadence (at least every 90 days for GitHub PATs) and log rotations in `memory/decision_log.md`.
- **No fallback escalation**: if a scoped token lacks permissions, fail and log — do not silently fall back to a higher-privilege token.

See `docs/protocol/credential_scoping.md` for integration details and the migration path from shared auth.

# CUSTOMIZE: Add project-specific secret handling rules below
