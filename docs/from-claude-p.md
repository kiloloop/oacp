# From `claude -p` to an interactive Claude Code session

On June 15, programmatic Claude usage — `claude -p`, the Agent SDK, Claude Code GitHub Actions, third-party tools — moves onto a separate metered monthly credit. Interactive Claude Code and chat stay on your subscription, unchanged.

It's a reasonable change: programmatic usage is heavier and burstier than a person typing. But if you run agents in automation, it's worth asking a different question than "how do I dodge the meter" — **why is the agent headless and synchronous in the first place?**

This guide is built to be handed to your agent. Read the first half for the *why*; paste the setup block into your Claude Code session for the *how*.

> **Setup is a one-time ~5 minutes. Every task after that is one `oacp send` — async, non-blocking, on your subscription.**

## The shape of `claude -p`

`claude -p "do X"` is synchronous and headless:

- it **blocks** — your script waits for the agent to finish
- it runs **headless** — no session to glance at, attach to, or steer mid-run
- after June 15, it's **metered** — programmatic usage draws from the separate credit pool

That shape was always a compromise. You wanted to script an agent, and `claude -p` was the way to do it from a shell. Synchronous-and-headless came along for the ride.

## The other shape

Run the agent as a **standing interactive Claude Code session**, and *send* it work.

```
Before:  claude -p "do X"
         blocks · headless · metered after Jun 15

After:   oacp send  →  a standing interactive session picks it up
         async · you're not blocked · on your subscription
```

Concretely — the call that replaces `claude -p "do X"`:

```bash
oacp send my-project --from sender --to claude --type task_request \
  --subject "do X" --body "...details..."
```

Same prompt, different delivery. The text you'd have handed to `claude -p` becomes the message `--body`; `oacp send` drops it in the interactive session's inbox, and the session picks it up and runs it. Sending the prompt as an OACP message is the whole trick — it routes the work into a session running in interactive mode instead of a headless `claude -p` call.

The session is a real interactive Claude Code session — so it stays on your subscription. You're not spoofing interactive mode; you're using it, and feeding it a queue.

This isn't only about the credit. Async is the right shape for most automation — a build, an overnight refactor, a research job — none of it needs your script to sit and wait. And once tasks are messages in an inbox, you get real multi-agent coordination — review loops, handoffs, several agents on one project — instead of a pile of blocking shell calls.

## Your options, honestly

Two real choices if June 15 affects you.

**1. Pay the metered credit (or API rates).** Simplest. Low programmatic volume — the included credit may cover you; past that it's API pricing. Already on the API — nothing changes. No new tooling; it just costs money at scale.

**2. OACP — run interactive for real, feed it a queue.** Run a real interactive Claude Code session and send it work. It stays on your subscription because it genuinely *is* an interactive session. Not a one-liner — it's a workflow change. In exchange you get real agent coordination, not just a credit workaround.

Option 1 is the no-effort path that costs money at volume. Option 2 is a one-time workflow change that doesn't. Pick on that.

## What OACP is

OACP isn't a June-15 tool. It's a file-based protocol for coordinating AI agents — inbox/outbox messaging, structured review loops, shared memory — no server, no daemon, just files in a directory. It was built to run a multi-agent fleet; the `claude -p` transition is **one usage** of it. Full picture: the [README](../README.md) and [SPEC](../SPEC.md). The companion [oacp-skills](https://github.com/kiloloop/oacp-skills) repo packages the runtime guidance — skills that teach Claude, Codex, and other agents to operate the protocol.

## Set it up — paste this into your agent

Open Claude Code in the repo you want the agent to work in, and paste this in:

~~~
Set me up as an async OACP worker for this repo.

1. Install the oacp-cli tool if it isn't already installed — check with
   oacp --version. Package and docs: https://github.com/kiloloop/oacp
2. Create an OACP project workspace for this repo, named after the repo.
   Give it two agents: "claude" (you, the worker) and "sender" (whoever
   dispatches tasks — my shell, my CI, or another agent).
3. Wire this repo for the Claude Code runtime against that project (the
   oacp setup command for the claude runtime).
4. Install the companion OACP agent skills from
   https://github.com/kiloloop/oacp-skills — at minimum the check-inbox
   skill, so you know how to process a task when it lands in your inbox.
5. Run oacp doctor --project <the project name> and confirm there are
   no issues — plain oacp doctor only checks the environment; the
   --project form checks this workspace's inbox, schema, and status.
6. Start a Monitor that keeps oacp watch re-running for the claude
   agent on this project. A single oacp watch does one scan and exits,
   so it has to run on a loop — while true; do oacp watch --project
   <project> --agent claude || true; sleep 120; done — otherwise you
   only pick up tasks that happened to be there at setup time. Then
   tell me the exact oacp send command I use to dispatch a task to you.

Protocol reference: https://github.com/kiloloop/oacp/blob/main/QUICKSTART.md
~~~

When it finishes, that session is a standing worker, and it has told you your send command. It looks like this (swap `my-project` for whatever it named the project):

```bash
oacp send my-project --from sender --to claude --type task_request \
  --subject "do X" --body "...details..."
```

Run that from your shell, your CI, or another agent — anywhere `claude -p` ran. The watcher fires, the session picks up the task, it runs async while you move on.

<details>
<summary>Manual setup — no agent, or you want the exact commands</summary>

```bash
# install
uv tool install oacp-cli          # or: pipx install oacp-cli

# create a workspace — "claude" does the work, "sender" dispatches
oacp init my-project --agents claude,sender

# wire this repo for Claude Code
oacp setup claude --project my-project

# verify the workspace is wired (not just the environment)
oacp doctor --project my-project
```

Then, in a Claude Code session, arm the watcher in a Monitor. `oacp watch` does one scan and exits, so it has to run on a loop:

```
while true; do
  oacp watch --project my-project --agent claude || true
  sleep 120
done
```

Keep that loop running — it's now a standing worker, picking up tasks as they land. Send work with the `oacp send` command above.

</details>

**On verbosity.** `oacp send` is explicit by design — typed messages, priorities, threading. If you miss `claude -p "X"`, a shell alias closes the gap:

```bash
oacp-do() {
  oacp send my-project --from sender --to claude --type task_request \
    --subject "$1" --body "${2:-$1}"
}
# now: oacp-do "refactor the auth module"
```

## Where it fits — and where it doesn't

**Fire-and-forget callers** — "build this," "refactor that," an overnight job, a research task. Clean fit. The caller never needed to block; async is strictly better.

**Request-response callers** — CI doing `RESULT=$(claude -p ...)` and using the output inline. Works too, but not a one-liner: the caller also arms `oacp watch --agent sender` to catch the reply. Worth it for a real pipeline — just know it's more than a swap.

**If you just want zero workflow change** and don't care about coordination — keep `claude -p` and pay the meter (option 1). OACP earns its setup cost when you have more than one agent, recurring work, or you want to see and steer what the agent is doing. If that's not you, we'd rather say so.

## Try it

Do the setup above in a repo you actually work in, send one real task, watch the session pick it up.

If something breaks, the setup is rougher than it should be, or the docs are wrong — **[open an issue](https://github.com/kiloloop/oacp/issues)**. That feedback is what we're after; it's more useful than a star.
