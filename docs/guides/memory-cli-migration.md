# Memory command migration for 0.5.3

OACP 0.5.3 removes the `memory`, `org-memory`, `write-event`, and `retention`
commands. Calls, including `--help` and `oacp help <command>`, now return the
normal unknown-command error with exit code 2. Update scripts, skills, and
operator commands before upgrading.

## Memory and events

Install [agent-memory-cli](https://github.com/kiloloop/agent-memory) separately
and call `agent-memory` directly. Event writing requires a version that provides
`agent-memory event write` (0.1.1 or later); OACP no longer bundles a fallback.

| Previous command | Direct replacement |
| --- | --- |
| `oacp memory init` | `agent-memory enable` |
| `oacp memory <verb> ...` | `agent-memory <verb> ...` |
| `oacp org-memory init` | `agent-memory org init` |
| `oacp write-event ...` | `agent-memory event write ...` |

Replace `--oacp-dir <path>` with `--home <path>` (including the `--flag=value`
form). Preserve the remaining arguments. For example:

```bash
agent-memory pull --home "$OACP_HOME"
agent-memory event write --home "$OACP_HOME" --agent <agent> \
  --project <project> --type decision --slug api-convention --body "Use REST"
```

The direct event writer reports `published:` or `idempotent:` rather than the
old fallback's `OK:`; its dry-run preview goes to stderr. Update callers that
parse that output. Existing memory files and their layout do not need to move.

If a workspace still registers `.claude/hooks/oacp-memory-pull.sh` or
`oacp-memory-push.sh`, re-run `oacp setup claude` before upgrading to retire
the recognized legacy hooks; configure replacement hooks with `agent-memory`
only when automatic sync is intended.

## Retention

`oacp retention` was an advisory command and performed no pruning. It has no
replacement inside OACP. Retention remains operator-owned; any external tool
must preserve the [protected-evidence rules](../protocol/inbox_outbox.md#message-history-retention).
