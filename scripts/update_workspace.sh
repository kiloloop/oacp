#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# update_workspace.sh — idempotent sync of an existing project workspace
# with the latest directory structure expected by OACP.
# Directory structure — keep in sync with init_project_workspace.sh

usage() {
  cat <<EOF
Usage: $0 <project> [--repo /path/to/repo] [--link SRC:DST ...] [--dry-run] [--quiet]

Sync an existing project workspace with the latest directory structure.
The workspace must already exist (use init_project_workspace.sh to create one).

Options:
  --repo PATH       Set repo root for artifact symlinks.
  --link SRC:DST    Create/update symlink: artifacts/DST -> REPO/SRC.
                    Can be repeated. Requires --repo.
  --dry-run         Show what would change without making changes.
  --quiet           Suppress unchanged items, only show actions and summary.

Exit codes:
  0  Success (all changes applied or nothing to do)
  1  Warnings (e.g., broken symlinks)
  2  Usage error
EOF
  exit 2
}

if [[ $# -lt 1 ]]; then
  usage
fi

PROJECT_NAME="$1"
shift

if [[ "$PROJECT_NAME" == */* || "$PROJECT_NAME" == .* ]]; then
  echo "Error: project name must not contain '/' or start with '.'" >&2
  exit 2
fi

OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
PROJECT_ROOT="$OACP_ROOT/projects/$PROJECT_NAME"

REPO_DIR=""
ARTIFACT_LINKS=()
DRY_RUN=false
QUIET=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      [[ $# -ge 2 ]] || { echo "Error: --repo requires a value" >&2; usage; }
      REPO_DIR="$2"; shift 2 ;;
    --link)
      [[ $# -ge 2 ]] || { echo "Error: --link requires a value" >&2; usage; }
      ARTIFACT_LINKS+=("$2"); shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --quiet) QUIET=true; shift ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

# ── Guard: workspace must already exist ──────────────────────────────────
if [[ ! -d "$PROJECT_ROOT" ]]; then
  echo "Error: workspace '$PROJECT_ROOT' does not exist." >&2
  echo "Use init_project_workspace.sh to create a new workspace." >&2
  exit 2
fi

# ── Counters ─────────────────────────────────────────────────────────────
CREATED=0
UNCHANGED=0
WARNINGS=0

log_action() {
  local prefix="$1" msg="$2"
  if [[ "$prefix" == "~" && "$QUIET" == true ]]; then
    return
  fi
  echo " $prefix $msg"
}

# ── Phase 1: Ensure directories ─────────────────────────────────────────
EXPECTED_DIRS=(
  "agents/codex/inbox"
  "agents/codex/outbox"
  "agents/codex/dead_letter"
  "agents/claude/inbox"
  "agents/claude/outbox"
  "agents/claude/dead_letter"
  "agents/gemini/inbox"
  "agents/gemini/outbox"
  "agents/gemini/dead_letter"
  "packets/review"
  "packets/findings"
  "packets/test"
  "packets/deploy"
  "checkpoints"
  "merges"
  "memory"
  "memory/archive"
  "artifacts"
  "state"
  "logs"
)

for dir in "${EXPECTED_DIRS[@]}"; do
  target="$PROJECT_ROOT/$dir"
  if [[ -d "$target" ]]; then
    log_action "~" "dir  $dir"
    ((UNCHANGED+=1))
  else
    if [[ "$DRY_RUN" == true ]]; then
      log_action "+" "dir  $dir (dry-run)"
    else
      mkdir -p "$target"
      log_action "+" "dir  $dir"
    fi
    ((CREATED+=1))
  fi
done

# ── Phase 2: Ensure .gitkeep files ──────────────────────────────────────
# .gitkeep goes in dirs that are typically empty placeholders.
# NOT in memory/, state/, logs/ — those get real content.
GITKEEP_DIRS=(
  "agents/codex/inbox"
  "agents/codex/outbox"
  "agents/codex/dead_letter"
  "agents/claude/inbox"
  "agents/claude/outbox"
  "agents/claude/dead_letter"
  "agents/gemini/inbox"
  "agents/gemini/outbox"
  "agents/gemini/dead_letter"
  "packets/review"
  "packets/findings"
  "packets/test"
  "packets/deploy"
  "checkpoints"
  "merges"
  "artifacts"
)

for dir in "${GITKEEP_DIRS[@]}"; do
  target="$PROJECT_ROOT/$dir/.gitkeep"
  if [[ -f "$target" ]]; then
    log_action "~" "file $dir/.gitkeep"
    ((UNCHANGED+=1))
  else
    if [[ "$DRY_RUN" == true ]]; then
      log_action "+" "file $dir/.gitkeep (dry-run)"
    else
      touch "$target"
      log_action "+" "file $dir/.gitkeep"
    fi
    ((CREATED+=1))
  fi
done

# ── Phase 3: Ensure memory files ────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXAMPLES_DIR="$SCRIPT_DIR/../examples"

if [[ ! -f "$PROJECT_ROOT/memory/project_facts.md" ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    log_action "+" "file memory/project_facts.md (dry-run)"
  else
    if [[ -f "$EXAMPLES_DIR/project_facts.example.md" ]]; then
      cp "$EXAMPLES_DIR/project_facts.example.md" "$PROJECT_ROOT/memory/project_facts.md"
    else
      cat > "$PROJECT_ROOT/memory/project_facts.md" <<'EOM'
# Project Facts

- Project workspace created.
EOM
    fi
    log_action "+" "file memory/project_facts.md"
  fi
  ((CREATED+=1))
else
  log_action "~" "file memory/project_facts.md"
  ((UNCHANGED+=1))
fi

if [[ ! -f "$PROJECT_ROOT/memory/decision_log.md" ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    log_action "+" "file memory/decision_log.md (dry-run)"
  else
    cat > "$PROJECT_ROOT/memory/decision_log.md" <<EOM
# Decision Log

## $(date +%F)
- Project workspace initialized.
EOM
    log_action "+" "file memory/decision_log.md"
  fi
  ((CREATED+=1))
else
  log_action "~" "file memory/decision_log.md"
  ((UNCHANGED+=1))
fi

if [[ ! -f "$PROJECT_ROOT/memory/open_threads.md" ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    log_action "+" "file memory/open_threads.md (dry-run)"
  else
    cat > "$PROJECT_ROOT/memory/open_threads.md" <<'EOM'
# Open Threads

- None yet.
EOM
    log_action "+" "file memory/open_threads.md"
  fi
  ((CREATED+=1))
else
  log_action "~" "file memory/open_threads.md"
  ((UNCHANGED+=1))
fi

if [[ ! -f "$PROJECT_ROOT/memory/known_debt.md" ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    log_action "+" "file memory/known_debt.md (dry-run)"
  else
    cat > "$PROJECT_ROOT/memory/known_debt.md" <<'EOM'
# Known Debt

Use this file to track verified, unresolved project debt that future sessions
should not rediscover from scratch.

| Item | Severity | Date Found | Source | Status |
| --- | --- | --- | --- | --- |
| _None yet._ |  |  |  |  |
EOM
    log_action "+" "file memory/known_debt.md"
  fi
  ((CREATED+=1))
else
  log_action "~" "file memory/known_debt.md"
  ((UNCHANGED+=1))
fi

# ── Phase 4: workspace.json ──────────────────────────────────────────────
VERSION_FILE="$SCRIPT_DIR/../VERSION"
if [[ -f "$VERSION_FILE" ]]; then
  STANDARDS_VERSION="$(head -1 "$VERSION_FILE" | tr -d '[:space:]')"
else
  STANDARDS_VERSION="0.5.0"
fi

WORKSPACE_JSON="$PROJECT_ROOT/workspace.json"
if [[ -f "$WORKSPACE_JSON" ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    log_action "~" "file workspace.json (dry-run, would update timestamps)"
  else
    python3 -c "
import json, datetime, sys
path = sys.argv[1]
version = sys.argv[2]
with open(path) as f:
    data = json.load(f)
data['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
data['standards_version'] = version
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
" "$WORKSPACE_JSON" "$STANDARDS_VERSION"
    log_action "~" "file workspace.json (updated timestamps)"
  fi
  ((UNCHANGED+=1))
else
  if [[ "$DRY_RUN" == true ]]; then
    log_action "+" "file workspace.json (dry-run)"
  else
    python3 -c "
import json, datetime, sys
data = {
    'project_name': sys.argv[1],
    'repo_path': None,
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'standards_version': sys.argv[2]
}
with open(sys.argv[3], 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
" "$PROJECT_NAME" "$STANDARDS_VERSION" "$WORKSPACE_JSON"
    log_action "+" "file workspace.json"
  fi
  ((CREATED+=1))
fi

# ── Phase 5: Symlink management ─────────────────────────────────────────
# Validate existing symlinks in artifacts/
if [[ -d "$PROJECT_ROOT/artifacts" ]]; then
  for link in "$PROJECT_ROOT/artifacts"/*; do
    [[ -e "$link" || -L "$link" ]] || continue
    if [[ -L "$link" ]]; then
      if [[ ! -e "$link" ]]; then
        log_action "!" "broken symlink: artifacts/$(basename "$link") -> $(readlink "$link")"
        ((WARNINGS+=1))
      fi
    fi
  done
fi

# Create/update symlinks from --link flags
if [[ ${#ARTIFACT_LINKS[@]} -gt 0 ]]; then
  if [[ -z "$REPO_DIR" ]]; then
    echo "Error: --link requires --repo" >&2
    exit 2
  fi
  for entry in "${ARTIFACT_LINKS[@]}"; do
    src="${entry%%:*}"
    dst="${entry##*:}"
    target_path="$REPO_DIR/$src"
    link_path="$PROJECT_ROOT/artifacts/$dst"
    if [[ -L "$link_path" ]] && [[ "$(readlink "$link_path")" == "$target_path" ]]; then
      log_action "~" "link artifacts/$dst -> $target_path"
      ((UNCHANGED+=1))
    elif [[ -d "$target_path" ]]; then
      if [[ "$DRY_RUN" == true ]]; then
        log_action "+" "link artifacts/$dst -> $target_path (dry-run)"
      else
        ln -sfn "$target_path" "$link_path"
        log_action "+" "link artifacts/$dst -> $target_path"
      fi
      ((CREATED+=1))
    else
      log_action "!" "link target not found: $target_path"
      ((WARNINGS+=1))
    fi
  done
fi

# ── Summary ──────────────────────────────────────────────────────────────
LABEL=""
if [[ "$DRY_RUN" == true ]]; then
  LABEL=" (dry-run)"
fi
echo ""
echo "Updated workspace: $PROJECT_ROOT$LABEL"
echo "  $CREATED created, $UNCHANGED unchanged, $WARNINGS warnings"

if [[ $WARNINGS -gt 0 ]]; then
  exit 1
fi
exit 0
