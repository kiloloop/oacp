# Setup Guide

## Prerequisites

- `bash` 3.2+ (macOS default) or 4+ (recommended)
- `python3` 3.9+ (for JSON state management, quality gate scripts, and inbox messaging)
- `gh` CLI (optional, for GitHub operations — `gh auth login`)
- Agent runtime CLI: `claude`, `codex`, or `gemini` (depending on your agents)

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

```bash
oacp init <project>
```

With artifact symlinks from a repo checkout:

```bash
oacp init <project> --repo /path/to/repo
```

## 3) Set up templates (source checkout only)

If you installed from source, copy and customize the templates you need:

```bash
# From the oacp repo root:

# Roles
cp templates/roles/role_baseline.template.md $OACP_HOME/projects/<project>/roles/baseline.md
cp templates/roles/role_definition.template.yaml $OACP_HOME/projects/<project>/roles/implementer.yaml

# Guardrails
cp templates/guardrails/*.template.md $OACP_HOME/projects/<project>/guardrails/

# Skills manifest
cp templates/skills_manifest.template.yaml $OACP_HOME/projects/<project>/skills_manifest.yaml
```

Edit each file and fill in the `# CUSTOMIZE:` points.

> **Packaged install?** Templates are bundled inside the package. You can find them by running `python3 -c "import oacp; print(oacp.__path__)"` and looking in the `_templates/` directory, or browse them on [GitHub](https://github.com/kiloloop/oacp/tree/main/templates).

## 4) Claude-specific setup (source checkout only)

For Claude Code projects, copy templates into the repo's `.claude/` directory:

```bash
# From the oacp repo root:
cp templates/claude/agents/role_agent.template.md /path/to/repo/.claude/agents/<role>.md
cp templates/claude/rules/guardrail.template.md /path/to/repo/.claude/rules/guardrails.md
cp templates/claude/skills/session_lifecycle.template.md /path/to/repo/.claude/skills/lifecycle/SKILL.md
```

## 5) Initialize packet files (source checkout only)

For the review/findings/merge packet workflow:

```bash
# From the oacp repo root:
scripts/init_packet.sh <project> <packet_id> --agent <agent_name>
```

> **Note:** `init_packet.sh` is not yet exposed as an `oacp` CLI subcommand. It requires a source checkout. See [docs/protocol/review_loop.md](../protocol/review_loop.md) for the review workflow documentation.

## 6) Update existing workspaces (source checkout only)

After pulling new standards, sync your workspace structure:

```bash
make update PROJECT=<project>
# Or dry-run to preview changes:
make update PROJECT=<project> ARGS="--dry-run"
```

**Note:** `workspace.json` is generated in the project workspace directory. Runtimes should symlink it into the repo root:

```bash
ln -sf $OACP_HOME/projects/<project>/workspace.json /path/to/repo/.oacp
```

## 7) Wire project-local helpers (optional)

Project-local wrappers can call these scripts using the `OACP_HOME` environment variable (defaults to `$HOME/oacp`).
