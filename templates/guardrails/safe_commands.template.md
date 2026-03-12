# Safe Commands

Command approval tiers for agent execution. These apply regardless of runtime.

---

## Auto-approve (read-only)

These commands are safe to run without confirmation. They do not mutate state.

- `ls`, `tree`
- `cat`, `head`, `tail`, `less`, `wc`
- `grep`, `rg`, `find`, `fd`
- `git status`, `git log`, `git diff`, `git show`, `git branch`
- `git stash list`
- `pwd`, `whoami`, `which`, `type`
- `echo` (for display, not redirection)
- `jq` (read-only queries)
- `gh pr list`, `gh pr view`, `gh issue list`, `gh issue view`

## Ask-first (mutating)

These commands change local or remote state. Require confirmation from a human or lead agent before running.

- `git push`, `git merge`, `git rebase`, `git cherry-pick`
- `git stash drop`, `git stash pop`
- `git checkout` (switching branches with uncommitted changes)
- `rm`, `mv`, `cp` (when overwriting)
- `mkdir -p` with deep paths
- `npm install`, `pip install`, `brew install`, `apt install`
- `docker run`, `docker build`, `docker-compose up`
- `curl -X POST/PUT/DELETE`, `wget` (write operations)
- `gh pr create`, `gh pr merge`, `gh issue create`
- File writes / redirections (`>`, `>>`)

## Never (destructive)

These commands must never be executed by any agent under any circumstances.

- `rm -rf /` or any recursive delete at a root or home-level path
- `git push --force` to `main` or `master`
- `git reset --hard` on shared branches
- `DROP TABLE`, `DROP DATABASE`, or equivalent destructive SQL
- `format`, `fdisk`, `mkfs` (disk operations)
- `kill -9` on system processes (PID 1, init, launchd)
- `chmod -R 777` on system directories
- `> /dev/sda` or raw device writes

# CUSTOMIZE: Add project-specific command rules below
# For example, restrict `terraform apply` to ask-first, or add deployment
# commands to the never tier during code-freeze windows.
