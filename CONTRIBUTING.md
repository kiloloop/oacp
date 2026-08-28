# Contributing to OACP

Thank you for your interest in contributing to the Open Agent Coordination Protocol!

Please read and follow our [Code of Conduct](https://github.com/kiloloop/.github/blob/main/CODE_OF_CONDUCT.md).

## How to Contribute

### Reporting Issues

- Use [GitHub Issues](https://github.com/kiloloop/oacp/issues) to report bugs or request features.
- Search existing issues before creating a new one.
- Include steps to reproduce for bug reports.

### Pull Requests

1. Fork the repository and create a feature branch from `main`.
2. Make your changes with clear, focused commits.
3. Run quality checks before submitting:
   ```bash
   make preflight
   make test
   ```
4. Open a PR against `main` with a clear description of what and why.
5. PRs require one approval before merging.

### What We're Looking For

- **Protocol improvements** — better message schemas, new state transitions, clearer specs
- **New templates** — packet or guardrail templates for common patterns
- **Script enhancements** — bug fixes, new validation rules, better error messages
- **Documentation** — typo fixes, clearer explanations, new guides
- **Test coverage** — additional tests for scripts and validators

## Development Setup

Development and test tooling requires Python 3.10 or newer. The installed CLI
continues to support Python 3.9.2 and newer.

```bash
# Clone
git clone https://github.com/kiloloop/oacp.git
cd oacp

# Install the package, crypto extra, and pinned development tools
python -m pip install --group dev -e ".[crypto]"

# Verify setup
make preflight
make test
```

### Running Tests

```bash
# Full test suite
make test

# Quality checks (what CI runs)
make preflight

# Extended checks including tests
make preflight ARGS="--full"
```

## Code Style

- **Python**: Follow PEP 8. We use `ruff` for linting (run via `make preflight`).
- **Shell**: Bash 3.2 compatible (macOS default). No bash 4+ features (`mapfile`, associative arrays). We use `shellcheck` for linting.
- **YAML**: 2-space indentation. Follow existing message and template schemas.
- **Markdown**: ATX headings (`#`), one sentence per line in prose sections.

## Conventions

- Templates use `# CUSTOMIZE:` markers for user-editable points.
- Packet naming follows `<YYYYMMDD>_<topic>_<owner>_r<round>`.
- Scripts use `python3` and avoid external dependencies beyond the standard library (exception: `pyyaml`).
- Shell scripts use POSIX-compatible constructs where possible; bash-specific features require bash 3.2+.
- README images should stay under ~500KB each. Use `docs/img/` for all README assets.

## Commit Messages

- Use imperative mood: "Add feature" not "Added feature"
- Keep the first line under 72 characters
- Reference issue numbers where applicable: "Fix message validation (#42)"

## Changelog Entries

Entries in `CHANGELOG.md` follow [Common Changelog](https://common-changelog.org) discipline: a changelog answers "does this affect me, and how", not "how does it work".

- One line, one change — split a multi-facet feature into two or three scoped bullets rather than one long bullet.
- State the user-visible delta, not the implementation path.
- Do not inline flag enums or sub-mechanism walkthroughs — link the protocol spec or doc section that carries the detail.
- One link per bullet, pointing at the best entry point.
- Each bullet must read as self-describing without its `### Added`/`### Changed` heading.

## License

By contributing, you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).
