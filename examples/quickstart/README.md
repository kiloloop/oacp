# Quickstart: Your First Agent Message

Send a message to an AI agent and get a reply — coordinated through files in a directory.

```
you (alice)                    AI agent (bob)
  │                                 │
  │──── task_request ──────────────▶│
  │                                 │
  │◀─── notification ───────────────│  "Done!"
  │                                 │
```

## Prerequisites

- Python 3.9+
- `pip install oacp-cli`
- An AI agent runtime — [Claude Code](https://claude.ai/code), [Codex](https://openai.com/index/codex/), or any agent that can read/write files

## Setup

```bash
git clone https://github.com/kiloloop/oacp.git && cd oacp

export OACP_HOME="$HOME/oacp-quickstart"
oacp init demo

# Create inboxes for alice (you) and bob (your AI agent)
mkdir -p $OACP_HOME/projects/demo/agents/alice/{inbox,outbox}
mkdir -p $OACP_HOME/projects/demo/agents/bob/{inbox,outbox}
```

## Step 1: Send a message

```bash
oacp send demo \
  --from alice --to bob \
  --type task_request \
  --subject "Hello from alice" \
  --body "Introduce yourself and tell me what you can do."
```

Check bob's inbox:

```bash
cat $OACP_HOME/projects/demo/agents/bob/inbox/*.yaml
```

```yaml
id: msg-20260315-alice-a1b2
from: alice
to: bob
type: task_request
created_at_utc: "2026-03-15T20:00:00Z"
subject: "Hello from alice"
body: |
  Introduce yourself and tell me what you can do.
```

Every OACP message is a human-readable YAML file. No binary formats, no databases.

## Step 2: Your agent replies

Open your AI agent in this repo and give it this prompt:

> Read `SPEC.md` to learn the OACP protocol. Then check your inbox at `$OACP_HOME/projects/demo/agents/bob/inbox/` and reply to any messages. You are bob.

The agent reads the spec, finds the message in its inbox, and uses `oacp send` to reply to alice.

## Step 3: Check your inbox

```bash
cat $OACP_HOME/projects/demo/agents/alice/inbox/*.yaml
```

You should see a reply from bob. That's it — two agents communicating through files.

## What just happened?

- You sent a message by writing a YAML file to an agent's inbox
- Your agent read the protocol spec, checked its inbox, and replied the same way
- Every message is threaded, human-readable, and stored as a file
- The inbox directory is the audit trail — no log infrastructure needed

**No servers. No databases. No API keys.** Just files in a directory.

## Next steps

- **Add skills** — install [OACP Skills](https://github.com/kiloloop/oacp-skills) so agents check inboxes automatically
- **Read the spec** — [SPEC.md](https://github.com/kiloloop/oacp/blob/main/SPEC.md) covers the full protocol including the review loop
- **See it in production** — [Cortex](https://github.com/kiloloop/cortex) is an example app built on OACP

## Cleanup

```bash
rm -rf "$OACP_HOME"
unset OACP_HOME
```
