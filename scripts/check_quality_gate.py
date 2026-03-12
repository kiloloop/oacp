#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""check_quality_gate.py — Machine-readable quality gate check for findings packets.

Reads a findings packet YAML file and outputs a JSON verdict.
Exit code 0 if the quality gate passes, 1 if it fails, 2 on usage error.

Usage:
    check_quality_gate.py <findings_packet.yaml>
    check_quality_gate.py --help

Output (JSON):
    {
        "gate_passed": true/false,
        "unresolved_p0": <int>,
        "unresolved_blocking": <int>,
        "total_findings": <int>,
        "fixed": <int>,
        "wont_fix": <int>
    }

Quality gate passes when:
    - No unresolved findings with severity P0
    - No unresolved findings with blocking: true

A finding is resolved if its status is "fixed" or "wont_fix". Any other
status (e.g., "open", "not_fixed", "needs_clarification") is unresolved.

Stdlib-only (no external dependencies). Uses a minimal YAML parser that
handles the flat-list-of-mappings structure of findings packets. If PyYAML
is available, it is preferred.
"""

import json
import os
import re
import sys


def parse_yaml_findings(text):
    """Minimal parser for findings packet YAML.

    Handles the specific structure of findings_packet.template.yaml:
    a top-level 'findings' key containing a list of mappings with scalar values.
    Falls back gracefully if the structure is unexpected.
    """
    try:
        import yaml

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data.get("findings", []) or []
        return []
    except ImportError:
        pass

    # Fallback: grep-based extraction for the findings list
    findings = []
    in_findings = False
    current = None

    for line in text.splitlines():
        stripped = line.rstrip()

        # Detect the findings: key
        if re.match(r"^findings:\s*$", stripped):
            in_findings = True
            continue

        if not in_findings:
            continue

        # A top-level key (no indent) ends the findings block
        if stripped and not stripped.startswith(" ") and not stripped.startswith("-"):
            break

        # New list item
        if re.match(r"^\s+-\s+", stripped):
            if current is not None:
                findings.append(current)
            current = {}
            # Extract key: value on the same line as the dash
            m = re.match(r"^\s+-\s+(\w+):\s*(.*)", stripped)
            if m:
                current[m.group(1)] = _parse_scalar(m.group(2))
            continue

        # Continuation key: value inside current mapping
        if current is not None:
            m = re.match(r"^\s+(\w+):\s*(.*)", stripped)
            if m:
                current[m.group(1)] = _parse_scalar(m.group(2))

    if current is not None:
        findings.append(current)

    return findings


def _parse_scalar(raw):
    """Convert a raw YAML scalar string to a Python value."""
    raw = raw.strip()
    # Strip surrounding quotes
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    if raw.lower() in ("null", "~", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def check_gate(findings):
    """Evaluate the quality gate against a list of finding dicts."""
    total = len(findings)
    unresolved_p0 = 0
    unresolved_blocking = 0
    fixed = 0
    wont_fix = 0

    for f in findings:
        status = str(f.get("status", "open")).lower()
        severity = str(f.get("severity", "")).upper()
        blocking = f.get("blocking", False)

        if status == "fixed":
            fixed += 1
        elif status == "wont_fix":
            wont_fix += 1

        is_resolved = status in ("fixed", "wont_fix")
        if not is_resolved:
            if severity == "P0":
                unresolved_p0 += 1
            if blocking:
                unresolved_blocking += 1

    gate_passed = unresolved_p0 == 0 and unresolved_blocking == 0

    return {
        "gate_passed": gate_passed,
        "unresolved_p0": unresolved_p0,
        "unresolved_blocking": unresolved_blocking,
        "total_findings": total,
        "fixed": fixed,
        "wont_fix": wont_fix,
    }


def print_interactive_plan(path):
    """Output a human-readable manual checklist for quality gate evaluation."""
    plan = f"""# Quality Gate — Manual Evaluation Plan

## Input
- Findings packet: `{path}`

## Steps

1. **Open the findings packet** at `{path}`
2. **List all findings** with their `id`, `severity`, `status`, and `blocking` fields
3. **Identify resolved findings**: status is `fixed` or `wont_fix`
4. **Count unresolved P0 findings**: severity = P0 AND status NOT in (fixed, wont_fix)
5. **Count unresolved blocking findings**: blocking = true AND status NOT in (fixed, wont_fix)
6. **Evaluate gate**:
   - Gate **PASSES** if both counts are 0
   - Gate **FAILS** if either count > 0

## Recording Template

| Finding | Severity | Blocking | Status    | Resolved? |
|---------|----------|----------|-----------|-----------|
| F-001   | P0       | true     | open      | ❌        |
| F-002   | P2       | false    | fixed     | ✅        |

**Unresolved P0**: ___
**Unresolved Blocking**: ___
**Gate Result**: PASS / FAIL
"""
    print(plan)


def main():
    # Handle --interactive-plan before normal arg parsing
    if "--interactive-plan" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--interactive-plan"]
        path = args[0] if args else "<findings_packet.yaml>"
        print_interactive_plan(path)
        sys.exit(0)

    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)

    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(2)

    with open(path, "r") as fh:
        text = fh.read()

    findings = parse_yaml_findings(text)
    verdict = check_gate(findings)

    print(json.dumps(verdict, indent=2))

    sys.exit(0 if verdict["gate_passed"] else 1)


if __name__ == "__main__":
    main()
