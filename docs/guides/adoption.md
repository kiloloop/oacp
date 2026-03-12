# Adoption Guide

## Minimum Adoption

- Use review/findings/merge packets for all code PRs.
- Enforce one batched findings file per round.
- Stop async loop after round 2.

## Recommended Adoption

- Add checkpoint files at major handoff boundaries.
- Maintain decision/open-thread memory per project.
- Tag standards repo releases and pin versions in internal docs.
- Define roles via `role_definition.template.yaml` for each agent.
- Set up guardrails (`safe_commands`, `secrets_rules`, `coding_standards`).
- Create a `skills_manifest.yaml` to track agent capabilities across runtimes.

## Full Adoption

- For Claude projects: set up agent templates, session lifecycle skills, and guardrail rules.

## Anti-Patterns

- Inline ad-hoc findings in chat without packet normalization.
- Per-comment fix loops for more than one round.
- Keeping critical decisions only in ephemeral chat context.
- Copying templates without customizing the `# CUSTOMIZE:` points.
