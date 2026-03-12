# Setup Guide

## Prerequisites

- `bash` 3.2+ (macOS default) or 4+ (recommended)
- `python3` 3.8+ (for JSON state management, quality gate scripts, and inbox messaging)
- `gh` CLI authenticated (`gh auth login`)
- Agent runtime CLI: `claude`, `codex`, or `gemini` (depending on your agents)
- `pyyaml` — required for preflight checks and YAML-based scripts (`pip3 install pyyaml`)

## 1) Clone OACP

```bash
export OACP_HOME="${OACP_HOME:-$HOME/oacp}"
mkdir -p "$OACP_HOME"
cd "$OACP_HOME"
git clone https://github.com/kiloloop/oacp.git
```

## 2) Initialize project workspace

```bash
scripts/init_project_workspace.sh <project>
```

With artifact symlinks from a repo checkout:

```bash
scripts/init_project_workspace.sh <project> --repo /path/to/repo
```

## 3) Set up templates

Copy and customize the templates you need:

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

## 4) Claude-specific setup (optional)

For Claude Code projects, copy templates into the repo's `.claude/` directory:

```bash
# From the oacp repo root:
cp templates/claude/agents/role_agent.template.md /path/to/repo/.claude/agents/<role>.md
cp templates/claude/rules/guardrail.template.md /path/to/repo/.claude/rules/guardrails.md
cp templates/claude/skills/session_lifecycle.template.md /path/to/repo/.claude/skills/lifecycle/SKILL.md
```

## 5) Initialize packet files per change/review cycle

```bash
scripts/init_packet.sh <project> <packet_id> --agent <agent_name>
```

## 6) Update existing workspaces

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
