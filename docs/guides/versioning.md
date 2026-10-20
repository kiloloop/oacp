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
(step 2 below). Between cuts the package version carries a `.dev0` suffix
(see [Dev Builds Between Cuts](#dev-builds-between-cuts)).

## Dev Builds Between Cuts

The development repository never sits on a released number while it carries
unreleased code. Right after each cut, a one-line follow-up PR moves
`pyproject.toml` from the released version to the next patch number with a
`.dev0` suffix (`0.5.3` → `0.5.4.dev0`, a [PEP 440](https://peps.python.org/pep-0440/#developmental-releases)
developmental release). The release-bump PR of the next cut replaces that
suffix with the release number (`0.5.4.dev0` → `0.5.4`, or `0.6.0` when the
cut is a minor). Nothing is published for a dev version: no tag, no PyPI
upload, no GitHub Release, and the `[Unreleased]` CHANGELOG section stays as
it is. `tests/test_version_convention.py` pins the shape.

What the convention buys:

- A checkout build (`pip install -e .`, `uv run --with-editable`) and the
  wheel it produces self-identify: `oacp --version` prints `0.5.4.dev0`,
  never the number of a release whose code it does not carry.
- Consumers pin release tags or PyPI versions and never a dev build. A dev
  version installs only from a checkout; an internal index or PyPI
  pre-release channel is not part of this convention.
- The contract stamp is untouched: `SPEC_VERSION` always names a release
  (`X.Y.Z`, never a dev suffix) because it names the release that carries a
  contract move, not the package that stamped it. Between cuts a dev build
  may therefore stamp a `spec_version` ahead of the last published package
  — the contract moved on the development branch before the cut that
  carries it. Readers treat `spec_version` as the contract stamp alone,
  never as the package version, and read artifacts by that contract's shape
  exactly as the contract-version history already requires of the
  release-window case.
- Every repository on this pipeline follows the same step at its own next
  cut, so a `.dev0` version means the same thing everywhere.

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
8. **Post-cut dev bump** — open a one-line PR on the development repository
   moving `pyproject.toml` to the next patch number with a `.dev0` suffix
   (see [Dev Builds Between Cuts](#dev-builds-between-cuts)). Nothing else
   moves and nothing is published.

## Doc-Only Changes

Documentation-only updates promote through the same two-PR staging path
(staging PR plus public PR) with no version bump, no tag, and no PyPI publish.
