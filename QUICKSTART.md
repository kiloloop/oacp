# OACP Quick Start

Get from zero to your first agent-to-agent message in 5 minutes.

## Prerequisites

- Python 3.9+ and Bash 3.2+

## 1. Set Up OACP Home

The **OACP home** is a directory where all project workspaces, agent inboxes, and shared memory live.

```bash
export OACP_HOME="$HOME/oacp"
mkdir -p "$OACP_HOME"
```

> Add `export OACP_HOME="$HOME/oacp"` to your shell profile so it persists across sessions.

## 2. Install the CLI

```bash
uv tool install oacp-cli
```

Or with pipx:

```bash
pipx install oacp-cli
```

## 3. Initialize a Project

Every project gets its own workspace with agent inboxes, shared memory, and packet directories.

```bash
oacp init my-first-project --agents claude,codex,cursor --repo /path/to/repo
```

Or with defaults (agents: claude, codex, cursor):

```bash
oacp init my-first-project
```

This creates:

```
$OACP_HOME/projects/my-first-project/
├── agents/
│   ├── claude/
│   │   ├── inbox/
│   │   └── outbox/
│   ├── codex/
│   │   ├── inbox/
│   │   └── outbox/
│   └── cursor/
│       ├── inbox/
│       └── outbox/
├── memory/
│   ├── project_facts.md
│   ├── decision_log.md
│   ├── open_threads.md
│   ├── known_debt.md
│   └── archive/
├── packets/
│   ├── review/
│   └── findings/
├── merges/
└── workspace.json
```

## 4. Connect Your Runtime

Run the runtime setup command from the repo you want the agent to work in:

```bash
cd /path/to/repo
oacp setup <runtime> --project my-first-project
```

For Claude Code:

```bash
oacp setup claude --project my-first-project
```

This creates or updates:

- `.claude/agents/my-first-project.md`
- `.claude/skills/`
- `.claude/hooks/oacp-memory-pull.sh`
- `.claude/hooks/oacp-memory-push.sh`
- `.claude/settings.json`

For other supported runtimes, use the same shape:

```bash
oacp setup codex --project my-first-project
oacp setup cursor --project my-first-project
oacp setup gemini --project my-first-project
```

Cursor support is scaffold-only until Cursor-owned rules land. Cursor sessions
must set `OACP_RUNTIME=cursor` or pass `--from` explicitly when sending messages.

For full runtime setup (role templates, guardrails, skills), see [docs/guides/setup.md](docs/guides/setup.md).

## 5. Send a Message

Send a task request from one agent to another:

```bash
oacp send my-first-project \
  --from claude --to codex \
  --type task_request \
  --priority P2 \
  --subject "Implement login endpoint" \
  --body "Add POST /login with JWT auth. See docs/api-spec.md for details."
```

This writes a YAML file to `agents/codex/inbox/` and a copy to `agents/claude/outbox/`.

## 6. Check the Inbox

See what's waiting for an agent:

```bash
oacp inbox my-first-project --agent codex
```

Or inspect the inbox directory directly:

```bash
ls "$OACP_HOME/projects/my-first-project/agents/codex/inbox/"
```

Read the message:

```bash
cat "$OACP_HOME/projects/my-first-project/agents/codex/inbox/"*.yaml
```

You'll see something like:

```yaml
id: "msg-20260311T120000Z-claude-a1b2"
from: "claude"
to: "codex"
type: "task_request"
priority: "P2"
created_at_utc: "2026-03-11T12:00:00Z"
subject: "Implement login endpoint"
body: |
  Add POST /login with JWT auth. See docs/api-spec.md for details.
```

To watch for new messages from a standing runtime or Monitor, use `oacp watch`.
A single `oacp watch` run scans once and exits, so keep re-running it when you
want a persistent worker. If more than one watcher follows the same agent
inbox, give each watcher a stable `--state-id` so each subscriber has its own
cursor:

```bash
OACP_WATCH_STATE_ID="${OACP_WATCH_STATE_ID:-$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')}"
while true; do
  oacp watch --project my-first-project --agent codex --state-id "$OACP_WATCH_STATE_ID" || true
  sleep 120
done
```

## 7. Reply

Send a response back:

```bash
oacp send my-first-project \
  --from codex --to claude \
  --type notification \
  --subject "Re: Implement login endpoint" \
  --body "Accepted. Starting implementation on branch codex/login-endpoint." \
  --in-reply-to "msg-20260311T120000Z-claude-a1b2"
```

## 8. Validate Messages

Check that a message follows the protocol schema:

```bash
oacp validate \
  "$OACP_HOME/projects/my-first-project/agents/codex/inbox/"*.yaml
```

## 9. Run Health Checks

Verify your environment first:

```bash
oacp doctor
```

Then check the workspace you just created:

```bash
oacp doctor --project my-first-project
```

Plain `oacp doctor` checks global environment health. The `--project` form also
checks the project workspace, inboxes, schemas, and agent status files.

## What's Next?

- **Review loop** — Set up structured code review between agents. See [docs/protocol/review_loop.md](docs/protocol/review_loop.md).
- **Durable memory** — Learn how agents share knowledge across sessions. See [docs/protocol/cross_runtime_sync.md](docs/protocol/cross_runtime_sync.md).
- **Safety defaults** — Understand the baseline safety rules. See [docs/protocol/agent_safety_defaults.md](docs/protocol/agent_safety_defaults.md).
- **Full protocol** — Read the complete specification in [SPEC.md](SPEC.md).
- **Adoption guide** — Minimum, recommended, and full adoption paths. See [docs/guides/adoption.md](docs/guides/adoption.md).
