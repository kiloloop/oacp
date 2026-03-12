# Project Guardrails
# CUSTOMIZE: Add project-specific rules below. Copy to .claude/rules/ in your project.

## Commands
- Never run `rm -rf` without explicit user confirmation
- Never force-push to main/master branches
- Never run destructive git commands (reset --hard, clean -f, branch -D) without confirmation
# CUSTOMIZE: add project-specific command restrictions

## Secrets
- Never commit files matching: .env, *.key, *.pem, credentials.*, *secret*
- Never print or log secret values
- Never hardcode API keys, tokens, or passwords in source files
# CUSTOMIZE: add project-specific secret patterns

## Code
- All network calls must have timeouts
- Use timezone-aware datetimes
- Validate all external input at system boundaries
# CUSTOMIZE: add project-specific coding rules

## Review Protocol
- All changes require a review packet before merge
- No unresolved P0 findings at merge time
- Validation commands must be recorded with outcomes
# CUSTOMIZE: add project-specific review requirements
