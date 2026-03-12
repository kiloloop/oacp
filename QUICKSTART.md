# OACP Quick Start

Get from zero to your first agent-to-agent message in 5 minutes.

## Prerequisites

- Python 3.9+ and Bash 3.2+
- `pip install pyyaml`

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
│   └── gemini/
│       ├── inbox/
│       └── outbox/
├── memory/
│   ├── project_facts.md
│   ├── decision_log.md
│   └── open_threads.md
├── packets/
│   ├── review/
│   └── findings/
├── merges/
└── workspace.json
```

## 4. Connect Your Runtime

Tell your agent runtime where the OACP workspace lives.

**Claude Code** — add the OACP workspace path to your project's `CLAUDE.md`:

```markdown
## OACP
OACP workspace: $OACP_HOME/projects/my-first-project/
Check inbox: ls $OACP_HOME/projects/my-first-project/agents/claude/inbox/
```

**Codex** — add the workspace path to your repo's `AGENTS.md`:

```markdown
## OACP
OACP workspace: $OACP_HOME/projects/my-first-project/
```

**Other runtimes** — point your agent's system prompt at the workspace path and instruct it to read `memory/project_facts.md` at session start.

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

## 7. Reply

Send a response back:

```bash
oacp send my-first-project \
  --from codex --to claude \
  --type notification \
  --subject "Re: Implement login endpoint" \
  --body "Accepted. Starting implementation on branch codex/login-endpoint." \
  --parent-message-id "msg-20260311T120000Z-claude-a1b2"
```

## 8. Validate Messages

Check that a message follows the protocol schema:

```bash
oacp validate \
  "$OACP_HOME/projects/my-first-project/agents/codex/inbox/"*.yaml
```

## 9. Run Health Checks

Verify your environment and workspace are set up correctly:

```bash
oacp doctor
oacp doctor --project my-first-project
```

## What's Next?

- **Review loop** — Set up structured code review between agents. See [docs/protocol/review_loop.md](docs/protocol/review_loop.md).
- **Durable memory** — Learn how agents share knowledge across sessions. See [docs/protocol/cross_runtime_sync.md](docs/protocol/cross_runtime_sync.md).
- **Safety defaults** — Understand the baseline safety rules. See [docs/protocol/agent_safety_defaults.md](docs/protocol/agent_safety_defaults.md).
- **Full protocol** — Read the complete specification in [SPEC.md](SPEC.md).
- **Adoption guide** — Minimum, recommended, and full adoption paths. See [docs/guides/adoption.md](docs/guides/adoption.md).
