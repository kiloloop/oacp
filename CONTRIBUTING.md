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

```bash
# Clone
git clone https://github.com/kiloloop/oacp.git
cd oacp

# Install dependencies
pip install pyyaml pytest

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

## Commit Messages

- Use imperative mood: "Add feature" not "Added feature"
- Keep the first line under 72 characters
- Reference issue numbers where applicable: "Fix message validation (#42)"

## License

By contributing, you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).
