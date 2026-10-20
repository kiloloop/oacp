# Plan and create a workspace

`oacp init` is create-only. An existing project root, including an empty directory
or dangling symlink, is refused with exit code **3** before workspace or global
agent-profile writes. `--fail-if-exists` makes this policy explicit; it is also
the default. There is no overwrite flag. Attach an existing workspace through
its `workspace.json`; use workspace-update or agent-add workflows for repairs.

Preview creation with:

```sh
oacp init demo --agents author,reviewer --dry-run --json
```

The version-1 JSON plan contains `project_root`, `can_apply`, `reason`, and
`actions`. Operations describe files/directories to create, symlinks (or skipped
missing sources), and shared agent profiles whose project membership is ensured.
Parent directories are created as needed at apply time. Profile operations may
leave already-complete profiles unchanged. Planning creates no files, directories,
locks or profiles. An existing root produces `can_apply: false`,
`reason: project_exists`, no actions, and exit code 3.

Apply with the same arguments without `--dry-run`. `--json` also supports apply
results. Successful apply JSON has `version: 1`, `project_root`,
`workspace_path`, `repo_dir` (string or null), `created_links` (destination/target
pairs), and `warnings` (strings). Error JSON has exactly `version: 1`, `error`
(`already_exists` or `initialization_failed`), and `message`. `already_exists`
means only the project-root guard or exclusive root creation refused; another
filesystem collision reports `initialization_failed`, even after partial creation.

Exit 0 means successful planning/creation; 3 means an existing target;
2 is an argument syntax error; 1 is another validation or filesystem failure.
Apply rechecks by exclusively creating the project root, so a competing
initializer cannot overwrite the winner. A plan is a snapshot, not a reservation.

Initialization is not an all-or-nothing filesystem transaction. A later I/O
failure can leave a partial newly created root; preserve and inspect it before
repair. Subsequent init calls refuse that root. Existing shared profile updates
retain their lock and atomic-update semantics. Hostile same-user mutation of
parent directories is outside this create-only contract.

## Migrating repeated initialization

This is a breaking default change. Earlier releases rewrote workspace metadata on repeated initialization. Callers
must now handle exit 3 as an attach-or-inspect result rather than retrying init.
No changes are made to existing workspace metadata, timestamps, agent roster or
global profiles when the initial project-root guard refuses creation.
