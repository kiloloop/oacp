# Project context and org-memory retrieval

Load the active project memory at init. Retrieve org memory when a task needs
shared rules, decisions, or recent cross-project context. Org-wide history can
grow independently of the small project working set.

| Store | Context policy |
|---|---|
| `projects/<project>/memory/` | Read `project_facts.md`, `decision_log.md`, `open_threads.md`, and `known_debt.md` in order at init. |
| `projects/<project>/memory/archive/` | Historical detail; follow a relevant reference when needed. |
| `org-memory/` | Search relevant topical files and read matching sections on demand. |

## Initialization and synchronization

Step 3 of the session-init protocol requires the four project files. In
Codex, the init command verifies three
protocol files and those four project files, then emits a manifest. The agent
still performs the ordered reads; verification does not inject their contents.
Missing project files are reported under the existing degraded-init behavior.

`oacp memory pull` synchronizes the memory repository on disk, including org
memory and its history. A successful pull does not add those files to model
context. Keep any configured pull before project-memory reads so those reads
use the refreshed files. Sync selection and context selection are independent;
there is no need to remove org memory from sync to avoid eager loading.

Hook enablement remains an adopter choice. This retrieval policy works with a
trusted startup hook or a manual init invocation. It does not require enabling
a hook that the project has disabled.

## Retrieve org context for a task

1. Resolve the configured OACP root and the org's topical filenames. Common
   choices are `rules.md`, `decisions.md`, and `recent.md`; adopters can use
   others. Read applicable standing rules and decisions before the actions
   they govern, such as release, review authority, or architecture work.
2. If cross-machine sync is configured, the task needs current shared facts,
   and local freshness is unknown, run `oacp memory pull`. A failed pull leaves
   freshness unknown; report that limitation before relying on affected facts.
3. Search the relevant explicit files with task identifiers and topic terms.
   For example, after confirming that `rules.md` exists:

   ```bash
   OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
   rg -n -- 'release|version' "$OACP_ROOT/org-memory/rules.md"
   ```

4. Read the full matching sections, including qualifications and provenance.
   Follow relevant source links when needed; avoid loading unrelated history
   or recursively reading `events/` and `debriefs/`. Read `recent.md` sections
   when recent org-wide changes matter to the task.
5. A search miss does not establish that no rule applies. Inspect relevant
   headings or linked topic files when needed. Missing or unreadable files
   are evidence gaps, not permission to bypass a known requirement. Verify
   stale or conflicting claims against their authoritative sources.

Runtime/project guidance may explicitly opt into a bounded startup summary.
The `recent.md` curation budget is a storage-maintenance guideline, not a
startup context budget. Keep required project reads intact even when org
retrieval is deferred.

## Update an existing installation

Updated scaffolds apply to newly created files. Runtime setup and org-memory
initialization preserve existing guidance and curated content; upgrading does
not rewrite an old `Always-loaded` comment or a local mandatory-org-read rule.

Review existing runtime instructions and summary comments explicitly. Keep the
four project reads, replace blanket org-memory startup reads with the task-based
policy above, and retain the underlying rules, decisions, history, and sync
configuration. Avoid rerunning runtime setup solely to change prose: setup can
register hooks, including ones an adopter intentionally disabled.
