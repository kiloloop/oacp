# Setup Guide

## Prerequisites

- `bash` 3.2+ (macOS default) or 4+ (recommended)
- `python3` 3.9.2+ (for state management, message validation, and inbox messaging)
- `gh` CLI (optional, for GitHub operations — `gh auth login`)
- Agent runtime CLI: `claude`, `codex`, `cursor`, or `gemini` (depending on your agents)

## 1) Install OACP

```bash
pip install oacp-cli
```

Or with uv/pipx:

```bash
uv tool install oacp-cli
# or
pipx install oacp-cli
```

<details>
<summary>From source (for contributors)</summary>

```bash
git clone https://github.com/kiloloop/oacp.git
cd oacp
pip install -e .
```

</details>

## 2) Initialize project workspace

Initialization is create-only. See [planning, exit codes and migration](workspace-initialization.md)
before attaching or repairing an existing workspace.

```bash
oacp init <project>
```

By default, `oacp init` creates `claude`, `codex`, and `cursor` agents.
Use `--agents` to include Gemini or custom agent names, and `--dry-run` to
preview the plan without writing anything.

Cursor support is scaffold-only until Cursor-owned rules land. Cursor sessions
must set `OACP_RUNTIME=cursor` or pass `--from` explicitly when sending messages.

With artifact symlinks from a repo checkout:

```bash
oacp init <project> --repo /path/to/repo
```

`workspace.json` is generated in the project workspace directory. Symlink it
into the repo root so the CLI and runtime hooks discover the workspace from
inside the repo without `OACP_HOME` set (`oacp init` prints this hint):

```bash
ln -sf $OACP_HOME/projects/<project>/workspace.json /path/to/repo/.oacp
```

## 3) Set up templates (source checkout only)

`oacp init` does not create role, guardrail or skills-manifest files, and the
package does not ship their templates. They are optional project conventions;
seed them from a source checkout:

```bash
# From the oacp repo root:
mkdir -p $OACP_HOME/projects/<project>/roles $OACP_HOME/projects/<project>/guardrails

# Roles
cp templates/roles/role_baseline.template.md $OACP_HOME/projects/<project>/roles/baseline.md
cp templates/roles/role_definition.template.yaml $OACP_HOME/projects/<project>/roles/implementer.yaml

# Guardrails
cp templates/guardrails/*.template.md $OACP_HOME/projects/<project>/guardrails/

# Skills manifest
cp templates/skills_manifest.template.yaml $OACP_HOME/projects/<project>/skills_manifest.yaml
```

Edit each file and fill in the `# CUSTOMIZE:` points.

> **Packaged install?** The wheel bundles only the templates the CLI itself
> uses (inbox message, agent card, agent profile, agent status, receiver config,
> runtime capabilities, and the Claude role agent) under the package's
> `_templates/` directory. Browse the rest on
> [GitHub](https://github.com/kiloloop/oacp/tree/main/templates).

## 4) Runtime setup

`oacp setup <runtime>` generates the runtime's configuration files in the repo.
Run it from the repo the agent works in; the project is read from the `.oacp`
marker when `--project` is omitted:

```bash
oacp setup claude --project <project>
```

For Claude Code this creates `.claude/agents/<project>.md` from the bundled
role agent template and `.claude/skills/`, and registers the
`oacp-envelope-hook` PreToolUse hook in `.claude/settings.json`. The agent file
and skills directory are created only when missing and `settings.json` is
merged, so re-running after an upgrade is safe. `codex`, `cursor`, and `gemini`
take the same shape; the [Quick Start](../../QUICKSTART.md#4-connect-your-runtime)
lists what each writes.

**Source checkout only:** the Claude guardrail rules file is not generated and
its template is not bundled. Copy it from the repo:

```bash
# From the oacp repo root:
mkdir -p /path/to/repo/.claude/rules
cp templates/claude/rules/guardrail.template.md /path/to/repo/.claude/rules/guardrails.md
```

## 5) Prepare workflow artifacts

Use the owning review or handoff workflow's artifact formats. OACP no longer
bundles a packet initializer or review, merge-decision and handoff templates.
See the [migration notes](command-migration.md#packet-and-handoff-templates)
and [review workflow documentation](../protocol/review_loop.md).

## 6) Update existing workspaces

`oacp init` refuses an existing workspace (exit 3). After upgrading, bring a
workspace up to the current directory layout with the idempotent sync script.
From a source checkout:

```bash
make update PROJECT=<project>
# Or dry-run to preview changes:
make update PROJECT=<project> ARGS="--dry-run"
```

Packaged installs ship the same script as `_scripts/update_workspace.sh` inside
the installed `oacp` package; run it with `bash <path> <project> [--dry-run]`.
There is no `oacp` subcommand for it.

## 7) Wire project-local helpers (optional)

Project-local wrappers should resolve the workspace the way the CLI does: an
explicit `--oacp-dir`, else `OACP_HOME`, else the repo's `.oacp` marker, else
`$HOME/oacp`.
