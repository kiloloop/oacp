# Versioning Guide

The `oacp-cli` package follows semantic versioning:

- `v0.x.y` while the CLI surface and protocol are still stabilizing.
- `v1.0.0` once format fields and loop controls are stable.

## Change Types

- Patch (`x.y.Z`): wording fixes, non-breaking docs improvements.
- Minor (`x.Y.0`): new optional fields/templates/scripts.
- Major (`X.0.0`): breaking template/protocol changes.

## Where the Version Lives

`pyproject.toml` is the single source of truth. `oacp/__init__.py` resolves
`__version__` at runtime via `importlib.metadata`, so `oacp --version` always
reports the installed package version — the version number itself is edited in
exactly one file (the release-bump PR also adds the matching `CHANGELOG.md`
section, see below). The protocol contract carries a separate stamp,
`SPEC_VERSION` in `scripts/_oacp_constants.py`, which moves only under the
contract-version rule in
[`docs/protocol/autonomy.md`](../protocol/autonomy.md#contract-version)
(step 2 below).

## Release Pipeline

Releases flow through a staged promotion: development repo → private staging
repo (soak) → public repo (`kiloloop/oacp`) → PyPI. Every repository content
change lands via a reviewed pull request (branch protection enforces squash
merges and rejects direct pushes); the one non-PR step is the release tag
itself, which triggers the publish workflow.

1. **Pre-export audit** — run `make preflight ARGS="--full"` (conflict-marker
   scan, Makefile checks, YAML validation, ruff, shellcheck, full test suite),
   then scan for leaks: hardcoded user paths, private repository references,
   secret patterns, and SPDX headers on all tracked Python files. All checks
   must pass before anything is exported.
2. **Contract-version check** — diff `docs/protocol/*.md` and the stamp
   producers (`scripts/autonomy_gate.py`, `scripts/envelope_compiler.py`,
   `scripts/init_project_workspace.py`) against the last release and apply the
   contract-version rule in full: a change every receiver must understand to
   verify or exchange a message, or to produce and finalize an artifact
   another receiver reads, moves `SPEC_VERSION` together with the spec's
   `spec_version` literals and a `CHANGELOG.md` line; additive-and-ignorable
   changes do not, and the release PR then records "no contract change". The
   package version moves on every release, the contract stamp only when the
   contract does.
3. **Version bump PR** — update the `version` in `pyproject.toml` and add a
   `CHANGELOG.md` section in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
   format. Gather the changes with `git log --oneline <last-tag>..HEAD`.
4. **Export to staging** — open a staging PR mirroring the development branch.
   The export copies the tracked tree only (`git archive`), so gitignored
   local files never leave the development machine; rerun the leak audit on
   the staged tree before merging. Non-urgent releases soak here before
   promotion; urgent fixes may skip the soak.
5. **Promote to public** — open a promotion PR into `kiloloop/oacp` under the
   release automation identity; a human reviews and merges it. The squash
   commit message becomes permanent public history — keep it free of internal
   references (the PR description alone is not what gets published).
6. **Tag and publish** — push an annotated `vX.Y.Z` tag to the public repo
   (a direct tag push, not a PR). The tag-triggered release workflow reruns
   the checks, builds the sdist/wheel, publishes `oacp-cli` to PyPI via
   [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC — no
   long-lived API tokens), and creates the GitHub Release with notes from the
   CHANGELOG section.
7. **Verify** — check the version-pinned endpoint
   `https://pypi.org/pypi/oacp-cli/<version>/json` first; the unversioned
   "latest" endpoint and the pip simple index can lag a few minutes behind it.
   Then install and confirm `oacp --version` prints the new version. On a
   machine with an existing uv-managed install, replace it explicitly —
   `uv cache prune`, `uv tool uninstall oacp-cli`, then
   `uv tool install oacp-cli==<version> --no-cache`; the short
   `uv tool install` form is only reliable in a clean tool environment.

## Doc-Only Changes

Documentation-only updates promote through the same two-PR staging path
(staging PR plus public PR) with no version bump, no tag, and no PyPI publish.
