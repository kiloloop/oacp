# Doctor Guide

`oacp doctor` is the fastest way to check whether your environment is ready for multi-agent workflows. Run it before setting up a project to verify prerequisites, or with `--project` to audit workspace health, inbox state, YAML schemas, and agent status.

## Quick start

```bash
pip install oacp-cli
oacp doctor
```

No project, no config, no commit required — just install and run.

## What doctor checks

Doctor organizes its output into five categories. When run without `--project`, only the first category (Environment) runs. With `--project <name>`, all five categories are evaluated.

### 1. Environment

Verifies that required and optional CLI tools are installed and reachable on `PATH`.

| Check | Required? | What it looks for |
|-------|-----------|-------------------|
| `git` | Yes | Git CLI |
| `python3` | Yes | Python 3.9+ interpreter |
| `gh` | Yes | GitHub CLI (for PR and issue workflows) |
| `ruff` | No | Python linter (optional, used in preflight) |
| `shellcheck` | No | Shell script linter (optional) |
| `pyyaml` | No | PyYAML library (needed for YAML validation) |

### 2. Workspace

Checks the project directory structure under `$OACP_HOME/projects/<name>/`.

- **workspace.json** — must exist and contain valid JSON
- **agents/ directory** — must exist; reports the number of registered agents

### 3. Inbox Health

Scans each agent's `inbox/` directory for pending messages and staleness.

- Reports message count per agent inbox
- Flags inboxes with messages older than 24 hours as stale
- Warns if an agent's inbox directory is missing

### 4. Schemas

Validates YAML files against protocol expectations.

- **packets/** — parses all `.yaml`/`.yml` files and reports syntax errors
- **status.yaml** — checks required fields (`runtime`, `status`, `capabilities`, `updated_at`), validates enum values, and verifies timestamp format

### 5. Agent Status

Checks each agent's `status.yaml` for presence and freshness.

- Warns if `status.yaml` is missing for a registered agent
- Flags status files not updated within the last hour as stale (the agent may have exited without a clean shutdown)

## Sample output

### Environment only (no project)

```
$ oacp doctor

[-] Environment
    [+] git — git version 2.47.0
    [+] python3 — Python 3.12.4
    [+] gh — gh version 2.62.0 (2024-11-14)
    [+] ruff — ruff 0.8.1
    [-] shellcheck — not installed (optional)
        Install: brew install shellcheck
    [+] pyyaml — available

No issues found.
```

### Full project check

```
$ oacp doctor --project my-project

[-] Environment
    [+] git — git version 2.47.0
    [+] python3 — Python 3.12.4
    [+] gh — gh version 2.62.0 (2024-11-14)
    [+] ruff — ruff 0.8.1
    [-] shellcheck — not installed (optional)
        Install: brew install shellcheck
    [+] pyyaml — available

[+] Workspace
    [+] workspace.json — valid
    [+] agents/ directory — 2 agent(s)

[!] Inbox Health
    [+] claude/inbox — empty
    [!] codex/inbox — 3 message(s), oldest 48h stale
        Process or archive stale inbox messages

[+] Schemas
    [+] packets/ — 4 YAML file(s) valid
    [+] claude/status.yaml — valid
    [+] codex/status.yaml — valid

[!] Agent Status
    [+] claude/status.yaml — present
    [!] codex/status.yaml — stale (updated 26h ago)
        Agent may have exited without clean close

No issues found.
```

## Reading the output

Each line starts with a severity indicator:

| Symbol | Meaning | Action needed? |
|--------|---------|----------------|
| `[+]` | Pass | No — everything is working |
| `[!]` | Warning | Recommended — non-blocking, but worth addressing |
| `[x]` | Error | Yes — a blocking issue that needs to be fixed |
| `[-]` | Skipped | No — an optional check that was not run |

Category headers show the worst severity among their checks. If any check is `[x]`, the category header is `[x]` too.

The exit code reflects the overall result:
- **0** — no errors (warnings are non-blocking)
- **1** — one or more blocking errors found

## Common fixes

### Environment

| Issue | Fix |
|-------|-----|
| `git — not found` | Install Git: https://git-scm.com/downloads |
| `python3 — not found` | Install Python 3.9+: https://www.python.org/downloads/ |
| `gh — not found` | Install GitHub CLI: `brew install gh` or https://cli.github.com/ |
| `pyyaml — not importable` | `pip install pyyaml` |
| `ruff — not installed` | `pip install ruff` (optional, for linting) |
| `shellcheck — not installed` | `brew install shellcheck` (optional, for shell script linting) |

### Workspace

| Issue | Fix |
|-------|-----|
| `workspace.json — not found` | Run `oacp init <project>` to create the workspace |
| `workspace.json — invalid` | Check for JSON syntax errors in the file |
| `agents/ directory — not found` | Run `oacp init <project>` to recreate the workspace |

### Inbox Health

| Issue | Fix |
|-------|-----|
| `<agent>/inbox — directory missing` | `mkdir -p $OACP_HOME/projects/<project>/agents/<agent>/inbox/` |
| `<agent>/inbox — N message(s), oldest Xh stale` | Process or archive old messages — they may be from a prior session |

### Schemas

| Issue | Fix |
|-------|-----|
| `packets/ — N invalid YAML file(s)` | Check YAML syntax in the reported files |
| `<agent>/status.yaml — missing required field` | Add the missing field; see `templates/agent_status.template.yaml` |
| `<agent>/status.yaml — unknown capabilities` | Use capabilities from the canonical set (see protocol spec) |

### Agent Status

| Issue | Fix |
|-------|-----|
| `<agent>/status.yaml — not found` | Create from template: `templates/agent_status.template.yaml` |
| `<agent>/status.yaml — stale` | The agent may have crashed or exited without cleanup. Restart the agent session or manually update `updated_at` |

## CLI options

```
oacp doctor                          # environment checks only
oacp doctor --project <name>         # full workspace + agent checks
oacp doctor --json                   # machine-readable JSON output
oacp doctor --project <name> --json  # full checks in JSON format
oacp doctor -o report.txt            # save report to file
```

## JSON output

Use `--json` for CI pipelines or automated monitoring:

```json
{
  "has_errors": false,
  "categories": [
    {
      "name": "Environment",
      "worst_severity": "ok",
      "results": [
        {
          "name": "git",
          "severity": "ok",
          "message": "git — git version 2.47.0"
        }
      ]
    }
  ]
}
```
