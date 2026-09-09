# Memory layout conformance fixture

The canonical three-tier memory layout specified in
`docs/protocol/org_memory.md`, as data. The kernel scaffolds the per-project
tier against it (`oacp init`); the memory tool
([agent-memory](https://github.com/kiloloop/agent-memory)) scaffolds the org
tier and writes the sync allowlist. Both repositories test against this
fixture, so a layout change is a change to these files first and to the
implementations second.

- `layout.yaml` — the marker and ignore-file names, the never-synced
  directory names, each storage tier (pattern, files, dirs, unsynced dirs),
  and `entries`, the literal closure of everything the layout names.
- `canonical_memory_gitignore.txt` — the sync allowlist written to a home's
  `.gitignore`, byte-exact. Compare bytes, never lines.

`tests/test_memory_layout_fixture.py` pins, in both directions, the spec's
"Layout" block and its `.gitignore` block to this fixture, the fixture's
`entries` to the closure of its tiers, the ignore file's rule lines to the
tiers and never-synced names (so the two files here cannot disagree), and
the kernel's project-tier scaffold to its tier. The memory tool vendors these
two files and asserts its own layout table, ignore text, and org-tier scaffold
against them the same way.
