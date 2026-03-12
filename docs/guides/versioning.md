# Versioning Guide

Use semantic-style tags for standards evolution:

- `v0.x.y` while process is still stabilizing.
- `v1.0.0` once format fields and loop controls are stable.

## Change Types

- Patch (`x.y.Z`): wording fixes, non-breaking docs improvements.
- Minor (`x.Y.0`): new optional fields/templates/scripts.
- Major (`X.0.0`): breaking template/protocol changes.

## Release Checklist

1. Update `CHANGELOG.md`.
2. Validate scripts on at least one project.
3. Tag release and announce migration notes for breaking changes.

## Project Pin Bump Checklist

When a project pins standards via `.oacp_version`, use this flow:

1. Confirm target standards tag/commit in this repo.
2. Update project pin file values:
   - `STANDARDS_TAG=<new_tag>`
   - `STANDARDS_COMMIT=<new_short_sha>`
   - `UPDATED_AT=<YYYY-MM-DD>`
3. Run smoke validation in the target project:
   - packet bootstrap (`scripts/shared/init_packet.sh`)
   - any updated standards scripts used by that project
4. Update project memory if the standards change introduces operational behavior changes.
5. Include pin bump details in project commit/PR notes (old -> new tag/commit).
6. If multiple projects are pinned, track rollout status and only retire old guidance after all critical projects migrate.
