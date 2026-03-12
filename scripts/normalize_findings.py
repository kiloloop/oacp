#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""normalize_findings.py — Convert raw agent QA output into canonical findings YAML.

Accepts structured JSON or unstructured plain-text input and produces a findings
packet that conforms to the canonical format defined in
templates/findings_packet.template.yaml.

Usage:
    normalize_findings.py --input <file> --format json|text --packet-id <id>
                          [--reviewer <name>] [--round <n>] [--output <file>]
    normalize_findings.py --help

Input formats:
    json  — A JSON object or array. If an object, looks for a "findings" key
            containing a list of finding objects. If an array, treats each
            element as a finding. Each finding object should have at minimum
            a description/summary/message field plus optional severity,
            blocking, status, file, line, area fields.

    text  — Unstructured review comments (one finding per paragraph, separated
            by blank lines). Lines starting with "P0"–"P3" or containing
            severity markers are parsed for severity. Everything else defaults
            to P2/non-blocking/open.

Output:
    Canonical findings YAML written to stdout (or --output file).

Exit codes:
    0  — Success
    1  — Validation failure (output would be malformed)
    2  — Usage error / bad input

Stdlib-only (no external dependencies).
"""

import json
import os
import re
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SEVERITIES = {"P0", "P1", "P2", "P3"}
VALID_STATUSES = {"open", "fixed", "wont_fix"}
VALID_AREAS = {
    "code",
    "test",
    "docs",
    "config",
    "infra",
    "security",
    "performance",
    "other",
}

DEFAULT_SEVERITY = "P2"
DEFAULT_STATUS = "open"
DEFAULT_BLOCKING = False
DEFAULT_AREA = "code"


# ---------------------------------------------------------------------------
# JSON input parser
# ---------------------------------------------------------------------------


def parse_json_input(raw_text):
    """Parse structured JSON input into a list of finding dicts."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON input: {exc}", file=sys.stderr)
        sys.exit(2)

    findings_raw = []

    if isinstance(data, list):
        findings_raw = data
    elif isinstance(data, dict):
        # Try common keys agents might use
        for key in ("findings", "issues", "comments", "results", "items"):
            if key in data and isinstance(data[key], list):
                findings_raw = data[key]
                break
        if not findings_raw:
            # Single finding wrapped in an object
            findings_raw = [data]
    else:
        print("Error: JSON input must be an object or array", file=sys.stderr)
        sys.exit(2)

    findings = []
    for i, item in enumerate(findings_raw):
        if not isinstance(item, dict):
            print(f"Warning: skipping non-object finding at index {i}", file=sys.stderr)
            continue
        findings.append(_normalize_finding_dict(item, i + 1))

    return findings


def _normalize_finding_dict(raw, index):
    """Map a raw JSON finding dict to canonical fields."""
    # Extract description from various possible field names
    description = ""
    for key in (
        "description",
        "summary",
        "message",
        "text",
        "body",
        "detail",
        "finding",
        "comment",
        "recommendation",
    ):
        if key in raw and raw[key]:
            description = str(raw[key]).strip()
            break

    # Severity
    severity = _extract_severity(raw.get("severity", ""))
    if not severity:
        # Try to infer from description
        severity = _infer_severity_from_text(description) or DEFAULT_SEVERITY

    # Blocking
    blocking = raw.get("blocking", DEFAULT_BLOCKING)
    if isinstance(blocking, str):
        blocking = blocking.lower() in ("true", "yes", "1")

    # Status
    status = str(raw.get("status", DEFAULT_STATUS)).lower().strip()
    if status not in VALID_STATUSES:
        status = DEFAULT_STATUS

    # Area
    area = str(raw.get("area", raw.get("category", DEFAULT_AREA))).lower().strip()
    if area not in VALID_AREAS:
        area = DEFAULT_AREA

    # File and line
    file_path = raw.get("file", raw.get("path", raw.get("filename", "")))
    line = raw.get("line", raw.get("line_number", raw.get("lineno")))
    if line is not None:
        try:
            line = int(line)
        except (ValueError, TypeError):
            line = None

    # Evidence / repro / expected / recommendation
    evidence = str(raw.get("evidence", raw.get("snippet", ""))).strip()
    repro = str(raw.get("repro", raw.get("reproduction", raw.get("steps", "")))).strip()
    expected = str(raw.get("expected", raw.get("expected_behavior", ""))).strip()
    recommendation = str(
        raw.get("recommendation", raw.get("suggestion", raw.get("fix", "")))
    ).strip()

    # If recommendation is empty but description was from a different field, check for recommendation
    if not recommendation and description != str(raw.get("recommendation", "")).strip():
        recommendation = ""

    return {
        "id": f"F-{index:03d}",
        "severity": severity,
        "blocking": blocking,
        "status": status,
        "area": area,
        "file": str(file_path) if file_path else "",
        "line": line,
        "description": description,
        "repro": repro,
        "expected": expected,
        "evidence": evidence,
        "recommendation": recommendation,
    }


# ---------------------------------------------------------------------------
# Plain-text input parser
# ---------------------------------------------------------------------------


def parse_text_input(raw_text):
    """Parse unstructured plain-text review comments into findings."""
    # Split into paragraphs (separated by blank lines)
    paragraphs = re.split(r"\n\s*\n", raw_text.strip())

    findings = []
    for i, para in enumerate(paragraphs):
        para = para.strip()
        if not para:
            continue

        finding = _parse_text_paragraph(para, i + 1)
        if finding:
            findings.append(finding)

    return findings


def _parse_text_paragraph(text, index):
    """Parse a single paragraph into a finding dict."""
    lines = text.strip().splitlines()
    if not lines:
        return None

    first_line = lines[0].strip()

    # Try to extract severity from the beginning of the paragraph
    severity = _infer_severity_from_text(first_line)
    if not severity:
        severity = DEFAULT_SEVERITY

    # Try to extract file:line references
    file_path = ""
    line_num = None
    file_match = re.search(r"(?:^|\s)([a-zA-Z0-9_./-]+\.[a-zA-Z0-9]+)(?::(\d+))?", text)
    if file_match:
        candidate = file_match.group(1)
        # Filter out things that don't look like file paths
        if "/" in candidate or candidate.count(".") == 1:
            file_path = candidate
            if file_match.group(2):
                line_num = int(file_match.group(2))

    # Detect blocking signals
    blocking = _infer_blocking(text, severity)

    # Clean up description: strip leading severity markers
    description = re.sub(
        r"^\s*\[?P[0-3]\]?\s*[-:.]?\s*", "", first_line, flags=re.IGNORECASE
    ).strip()
    if len(lines) > 1:
        rest = "\n".join(item_line.strip() for item_line in lines[1:]).strip()
        description = f"{description}\n{rest}" if description else rest

    return {
        "id": f"F-{index:03d}",
        "severity": severity,
        "blocking": blocking,
        "status": DEFAULT_STATUS,
        "area": DEFAULT_AREA,
        "file": file_path,
        "line": line_num,
        "description": description,
        "repro": "",
        "expected": "",
        "evidence": "",
        "recommendation": "",
    }


# ---------------------------------------------------------------------------
# Severity / blocking inference helpers
# ---------------------------------------------------------------------------


def _extract_severity(value):
    """Normalize a severity string to P0-P3 or None."""
    if not value:
        return None
    s = str(value).upper().strip()
    if s in VALID_SEVERITIES:
        return s
    # Handle "critical", "high", "medium", "low"
    mapping = {
        "CRITICAL": "P0",
        "BLOCKER": "P0",
        "HIGH": "P1",
        "MAJOR": "P1",
        "MEDIUM": "P2",
        "MODERATE": "P2",
        "NORMAL": "P2",
        "LOW": "P3",
        "MINOR": "P3",
        "TRIVIAL": "P3",
        "INFO": "P3",
    }
    return mapping.get(s)


def _infer_severity_from_text(text):
    """Try to find a severity marker in free text."""
    match = re.search(r"\b(P[0-3])\b", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    text_upper = text.upper()
    for keyword, sev in [
        ("CRITICAL", "P0"),
        ("BLOCKER", "P0"),
        ("HIGH", "P1"),
        ("MAJOR", "P1"),
        ("MEDIUM", "P2"),
        ("LOW", "P3"),
        ("MINOR", "P3"),
    ]:
        if keyword in text_upper:
            return sev

    return None


def _infer_blocking(text, severity):
    """Infer whether a finding is blocking from text and severity."""
    text_lower = text.lower()
    if re.search(r"\bblocking\b", text_lower):
        return True
    if re.search(r"\bnon[- ]?blocking\b", text_lower):
        return False
    if re.search(r"\bmust[ -]fix\b", text_lower):
        return True
    # P0 findings are blocking by default
    if severity == "P0":
        return True
    return DEFAULT_BLOCKING


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_findings(findings):
    """Validate that all findings have required fields with valid values.

    Returns a list of error strings (empty if valid).
    """
    errors = []
    if not findings:
        errors.append("No findings produced from input")
        return errors

    for i, f in enumerate(findings):
        fid = f.get("id", f"index {i}")

        if f.get("severity") not in VALID_SEVERITIES:
            errors.append(
                f"{fid}: invalid severity '{f.get('severity')}' (must be P0-P3)"
            )

        if not isinstance(f.get("blocking"), bool):
            errors.append(f"{fid}: 'blocking' must be a boolean")

        if f.get("status") not in VALID_STATUSES:
            errors.append(
                f"{fid}: invalid status '{f.get('status')}' (must be open/fixed/wont_fix)"
            )

    return errors


# ---------------------------------------------------------------------------
# YAML output (stdlib-only)
# ---------------------------------------------------------------------------


def _yaml_scalar(value):
    """Format a Python value as a YAML scalar string."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not value:
            return '""'
        # Quote if the string contains characters that could be misinterpreted
        needs_quote = any(
            c in value
            for c in (
                ":",
                "#",
                "{",
                "}",
                "[",
                "]",
                ",",
                "&",
                "*",
                "?",
                "|",
                "-",
                "<",
                ">",
                "=",
                "!",
                "%",
                "@",
                "`",
                "\n",
            )
        )
        if needs_quote or value.lower() in ("true", "false", "null", "yes", "no"):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            if "\n" in escaped:
                # Use literal block for multiline
                return None  # signal to use block style
            return f'"{escaped}"'
        return value
    return str(value)


def _yaml_block_scalar(value, indent):
    """Format a multiline string as a YAML literal block scalar."""
    prefix = " " * indent
    lines = value.splitlines()
    result = "|\n"
    for line in lines:
        result += f"{prefix}{line}\n"
    return result


def emit_yaml(packet_id, reviewer, round_num, findings):
    """Produce canonical findings YAML as a string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    blocking_count = sum(1 for f in findings if f.get("blocking"))
    non_blocking_count = len(findings) - blocking_count

    lines = []
    lines.append(f'packet_id: "{packet_id}"')
    lines.append('source_review_packet: ""')
    lines.append(f'reviewer: "{reviewer}"')
    lines.append(f"round: {round_num}")
    lines.append(f'created_at_utc: "{now}"')
    lines.append("summary:")
    lines.append('  verdict: ""')
    lines.append(f"  blocking_count: {blocking_count}")
    lines.append(f"  non_blocking_count: {non_blocking_count}")
    lines.append("findings:")

    for f in findings:
        lines.append(f'  - id: "{f["id"]}"')
        lines.append(f'    severity: "{f["severity"]}"')
        lines.append(f"    blocking: {_yaml_scalar(f['blocking'])}")
        lines.append(f'    status: "{f["status"]}"')
        lines.append(f'    area: "{f.get("area", DEFAULT_AREA)}"')
        lines.append(f"    file: {_yaml_scalar(f.get('file', ''))}")

        line_val = f.get("line")
        lines.append(f"    line: {_yaml_scalar(line_val)}")

        # Text fields — use block scalar if multiline
        for field in ("description", "repro", "expected", "evidence", "recommendation"):
            val = f.get(field, "")
            scalar = _yaml_scalar(val)
            if scalar is None:
                # multiline block
                lines.append(f"    {field}: {_yaml_block_scalar(val, 6)}")
            else:
                lines.append(f"    {field}: {scalar}")

    lines.append("qa_validation:")
    lines.append("  commands_run: []")
    lines.append("  deployment_check:")
    lines.append("    ready: false")
    lines.append("    rollback_verified: false")
    lines.append('    notes: ""')

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv):
    """Minimal argument parser (stdlib-only)."""
    args = {
        "input": None,
        "format": None,
        "packet_id": None,
        "reviewer": "unknown",
        "round": 1,
        "output": None,
    }

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__.strip())
            sys.exit(0)
        elif a == "--input" and i + 1 < len(argv):
            args["input"] = argv[i + 1]
            i += 2
        elif a == "--format" and i + 1 < len(argv):
            args["format"] = argv[i + 1]
            i += 2
        elif a == "--packet-id" and i + 1 < len(argv):
            args["packet_id"] = argv[i + 1]
            i += 2
        elif a == "--reviewer" and i + 1 < len(argv):
            args["reviewer"] = argv[i + 1]
            i += 2
        elif a == "--round" and i + 1 < len(argv):
            args["round"] = int(argv[i + 1])
            i += 2
        elif a == "--output" and i + 1 < len(argv):
            args["output"] = argv[i + 1]
            i += 2
        elif a == "--interactive-plan":
            print_interactive_plan()
            sys.exit(0)
        else:
            print(f"Error: unknown option '{a}'", file=sys.stderr)
            print(
                "Usage: normalize_findings.py --input <file> --format json|text --packet-id <id>",
                file=sys.stderr,
            )
            sys.exit(2)

    # Validate required args
    missing = []
    if not args["input"]:
        missing.append("--input")
    if not args["format"]:
        missing.append("--format")
    if not args["packet_id"]:
        missing.append("--packet-id")

    if missing:
        print(
            f"Error: missing required arguments: {', '.join(missing)}", file=sys.stderr
        )
        print(
            "Usage: normalize_findings.py --input <file> --format json|text --packet-id <id>",
            file=sys.stderr,
        )
        sys.exit(2)

    if args["format"] not in ("json", "text"):
        print(
            f"Error: --format must be 'json' or 'text', got '{args['format']}'",
            file=sys.stderr,
        )
        sys.exit(2)

    return args


def print_interactive_plan():
    """Output step-by-step instructions for manual findings normalization."""
    plan = """# Normalize Findings — Manual Plan

## Steps

1. **Collect raw review output** — copy the LLM's review response into a text file
2. **For each finding block**, extract these fields:

   | Field | Required | How to find it |
   |-------|----------|----------------|
   | `id` | yes | Assign sequentially: F-001, F-002, ... |
   | `severity` | yes | Look for P0/P1/P2/P3 or keywords: critical→P0, major→P1, minor→P2, nit→P3 |
   | `blocking` | yes | P0 = always true. Look for "must fix", "blocking" = true. Otherwise false |
   | `status` | yes | Set to "open" for all new findings |
   | `area` | yes | code / docs / tests / protocol / config |
   | `file` | yes | File path mentioned in the finding |
   | `line` | no | Line number if mentioned |
   | `description` | yes | The main issue description |
   | `recommendation` | yes | Suggested fix |

3. **Format as YAML** using the template at `templates/findings_packet.template.yaml`
4. **Validate**: every finding must have id, severity (P0-P3), blocking (bool), status (open/fixed/wont_fix)
5. **Save** to `packets/findings/<YYYYMMDD>_<topic>_<reviewer>_r<round>.yaml`

## Automated Alternative

```bash
normalize_findings.py --input raw_output.txt --format text --packet-id <id> --reviewer <name> --round 1
```
"""
    print(plan)


def main():
    args = parse_args(sys.argv[1:])

    # Read input
    input_path = args["input"]
    if input_path == "-":
        raw_text = sys.stdin.read()
    elif not os.path.isfile(input_path):
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(2)
    else:
        with open(input_path, "r") as fh:
            raw_text = fh.read()

    if not raw_text.strip():
        print("Error: input file is empty", file=sys.stderr)
        sys.exit(2)

    # Parse
    if args["format"] == "json":
        findings = parse_json_input(raw_text)
    else:
        findings = parse_text_input(raw_text)

    # Validate
    errors = validate_findings(findings)
    if errors:
        print("Validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    # Emit
    yaml_output = emit_yaml(
        packet_id=args["packet_id"],
        reviewer=args["reviewer"],
        round_num=args["round"],
        findings=findings,
    )

    if args["output"]:
        with open(args["output"], "w") as fh:
            fh.write(yaml_output)
        print(f"Wrote {len(findings)} findings to {args['output']}", file=sys.stderr)
    else:
        sys.stdout.write(yaml_output)


if __name__ == "__main__":
    main()
