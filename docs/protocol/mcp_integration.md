# MCP Tool Integration Protocol

Defines how MCP (Model Context Protocol) tool outputs flow into the
OACP review/findings workflow as structured evidence.

## Problem

Gemini has native MCP tools — BigQuery, browser automation, image generation —
that produce rich outputs (query results, screenshots, generated images).
Today, findings and review packets only have free-text `evidence` fields.
There is no convention for:

1. Attaching structured query results as evidence
2. Linking screenshots or generated images to findings
3. Recording MCP tool invocations in validation sections
4. Making MCP outputs discoverable by other agents (Claude, Codex)

## Principles

1. **File-based** — MCP outputs are saved as files; packets reference them by path
2. **Structured metadata** — each evidence attachment has a type, tool, and summary
3. **Opt-in richness** — plain text evidence still works; MCP metadata is additive
4. **Runtime-neutral** — the schema works for any tool provider, not just Gemini MCP

## Evidence Types

| Type                    | MCP Tool                                      | Typical Output                         | File Format        |
| ----------------------- | --------------------------------------------- | -------------------------------------- | ------------------ |
| `query_result`          | BigQuery (`execute_sql`, `ask_data_insights`) | Tabular data, row counts, aggregations | `.json` or `.csv`  |
| `screenshot`            | Browser (`browser_subagent`)                  | Page state capture, UI validation      | `.webp`, `.png`    |
| `recording`             | Browser (`browser_subagent`)                  | User flow recording                    | `.webp` (animated) |
| `generated_image`       | Image generation (`generate_image`)           | Visual assets, mockups                 | `.png`, `.webp`    |
| `forecast`              | BigQuery (`forecast`)                         | Time series predictions                | `.json`            |
| `contribution_analysis` | BigQuery (`analyze_contribution`)             | Metric attribution                     | `.json`            |
| `catalog_search`        | BigQuery (`search_catalog`)                   | Table/view discovery results           | `.json`            |
| `command_output`        | Shell / terminal                              | CLI execution results                  | `.txt`, `.log`     |

## Evidence Attachment Schema

### In Findings Packets

The existing `evidence` field (free text) is preserved for backward compatibility.
A new optional `evidence_attachments` list provides structured MCP evidence:

```yaml
findings:
  - id: "F-001"
    severity: "P1"
    blocking: true
    status: "open"
    area: "data"
    file: "src/asterbot/core/signal_cache.py"
    evidence: "Query shows 3.2% data gaps in signal stream during H19-H20"
    evidence_attachments:
      - type: "query_result"
        tool: "mcp_bigquery_execute_sql"
        path: "evidence/F-001_signal_gaps.json"
        summary: "SELECT hour, count gaps FROM signals WHERE gap_seconds > 30"
      - type: "screenshot"
        tool: "browser_subagent"
        path: "evidence/F-001_grafana_gaps.webp"
        summary: "Grafana dashboard showing signal gap pattern at H19"
    recommendation: "Add staleness gate for H19-H20 window"
```

### In Review Packets (Validation Section)

The `qa_validation.commands_run` list is extended with an optional `mcp_tool` field
and `attachments` for structured evidence:

```yaml
qa_validation:
  commands_run:
    - command: "python3 -m pytest tests/ -v"
      result: "pass"
      notes: "147 passed, 0 failed"
    - command: "SELECT COUNT(*) FROM trading.fills WHERE date = '2026-02-11'"
      result: "pass"
      mcp_tool: "mcp_bigquery_execute_sql"
      attachments:
        - type: "query_result"
          path: "evidence/fill_count_validation.json"
          summary: "1,247 fills recorded — matches expected range"
      notes: "Fill count within normal bounds"
```

## Evidence Storage Convention

Evidence files are stored alongside packet files in a dedicated subdirectory:

```
packets/
├── review/
│   └── 20260211_signal_gaps_gemini_r1.md
├── findings/
│   └── 20260211_signal_gaps_gemini_r1.yaml
└── evidence/
    ├── F-001_signal_gaps.json
    ├── F-001_grafana_gaps.webp
    └── fill_count_validation.json
```

### Naming Convention

Evidence files follow the pattern:

```
<finding_id>_<description>.<ext>       # Finding-specific evidence
<validation_description>.<ext>         # Validation evidence (no finding ID)
```

### Size Limits

- **Query results**: Store summary (first 100 rows + aggregates), not full dumps
- **Screenshots/recordings**: WebP preferred for compression; max 5 MB per file
- **JSON outputs**: Pretty-printed, max 10,000 lines

## MCP Tool Metadata Fields

When recording an MCP tool invocation in evidence, include:

| Field           | Required | Description                                              |
| --------------- | -------- | -------------------------------------------------------- |
| `type`          | Yes      | Evidence type from the table above                       |
| `tool`          | Yes      | MCP tool name (e.g., `mcp_bigquery_execute_sql`)         |
| `path`          | Yes      | Relative path to evidence file (from project root)       |
| `summary`       | Yes      | Human-readable one-line summary of what the output shows |
| `query`         | No       | For query tools: the SQL or query text                   |
| `timestamp_utc` | No       | When the tool was invoked                                |
| `runtime`       | No       | Which agent runtime produced this (default: `gemini`)    |

## Integration Points

### check_quality_gate.py

No changes needed. The quality gate evaluates findings by `severity`, `blocking`,
and `status` — evidence attachments are informational and don't affect the gate verdict.

### Gemini QA Workflow

When Gemini produces a findings packet (via `aster-review-pr` or similar):

1. Run MCP tools (BigQuery queries, browser checks) to gather evidence
2. Save outputs to `packets/evidence/<finding_id>_<desc>.<ext>`
3. Reference saved files in `evidence_attachments[]` in the findings YAML
4. Include a human-readable summary in the plain `evidence` field for backward compat

### Claude/Codex Consumption

When Claude or Codex read a findings packet with `evidence_attachments`:

- **JSON evidence**: Parse and use for context (e.g., query results showing the bug)
- **Image evidence**: Note the path for reference but don't try to render
- **Summary field**: Always available as a plain-text fallback

## Backward Compatibility

- The `evidence` field remains a plain string — no breaking changes
- `evidence_attachments` is optional — old packets without it still work
- `mcp_tool` in `commands_run` is optional — plain commands still work
- `check_quality_gate.py` ignores unknown fields in findings

## Future Extensions

- **Evidence validation script**: verify all `path` references resolve to actual files
- **Evidence index**: auto-generated cross-reference of all evidence files per packet
- **MCP output normalization**: standardize JSON schemas for common query patterns
