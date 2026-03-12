# Coding Standards

Supplemental coding rules that apply to all agent-written code. These complement project-specific linter and formatter configurations.

---

## Timeouts

Set explicit timeouts on all network and HTTP calls. Never use unbounded waits.

```python
# Good
requests.get(url, timeout=30)

# Bad
requests.get(url)
```

## Timezone-aware datetimes

Always use timezone-aware datetime objects. Never use naive datetimes.

```python
# Good
from datetime import datetime, timezone
now = datetime.now(timezone.utc)

# Bad
now = datetime.now()
```

## No hardcoded credentials or magic numbers

- Credentials go in environment variables (see `templates/guardrails/secrets_rules.template.md`).
- Magic numbers go in named constants with a comment explaining the value.

```python
# Good
MAX_RETRIES = 3  # cap retries to avoid runaway loops
TIMEOUT_SECONDS = 30

# Bad
for i in range(3):
    requests.get(url, timeout=30)
```

## Explicit error handling

- No bare `except` or `catch` clauses. Always specify the exception type.
- Handle errors at the appropriate level. Do not silently swallow exceptions.

```python
# Good
try:
    result = do_thing()
except ValueError as e:
    log.error("Invalid input: %s", e)
    raise

# Bad
try:
    result = do_thing()
except:
    pass
```

## File and resource cleanup

Close files, connections, and subprocesses explicitly. Use context managers where available.

```python
# Good
with open(path) as f:
    data = f.read()

# Bad
f = open(path)
data = f.read()
```

# CUSTOMIZE: Project-specific rules

Add rules specific to your project below. Examples:

```
# Linter: all code must pass `ruff check` with zero warnings before commit.
# Naming: use snake_case for Python, camelCase for JavaScript/TypeScript.
# Imports: use absolute imports; no wildcard imports.
# Tests: every new function must have at least one test.
# Logging: use structured logging (JSON) in production code.
```
