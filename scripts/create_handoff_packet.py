#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Create a structured handoff packet.

Usage:
    create_handoff_packet.py <project> --from <agent> --to <agent> --intent <text> [options]

Options:
    --artifact <text>           Artifact path/PR to review (repeatable)
    --done <text>               Definition-of-done item (repeatable)
    --next-step <text>          Suggested next step (repeatable)
    --output <path>             Output file path (default: projects/<project>/packets/handoff/<ts>_<from>_to_<to>.yaml)
    --oacp-dir <path>           Override OACP home directory (default: $OACP_HOME or ~/oacp)
    --dry-run                   Print packet instead of writing
    --json                      Output machine-readable report
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

from handoff_schema import validate_handoff_packet_text

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(text: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", text.strip())
    cleaned = cleaned.strip("-")
    return cleaned or "unknown"


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_packet(data: Dict[str, object]) -> str:
    artifacts = data["artifacts_to_review"]
    done_items = data["definition_of_done"]
    next_steps = data["suggested_next_steps"]

    lines: List[str] = [
        f"source_agent: {_quote(str(data['source_agent']))}",
        f"target_agent: {_quote(str(data['target_agent']))}",
        f"intent: {_quote(str(data['intent']))}",
        "",
        "artifacts_to_review:",
    ]

    for item in artifacts:
        lines.append(f"  - {_quote(str(item))}")

    lines.append("")
    lines.append("definition_of_done:")
    for item in done_items:
        lines.append(f"  - {_quote(str(item))}")

    lines.extend(
        [
            "",
            "context_bundle:",
            "  files_touched:",
            "    - path: \"TBD\"",
            "      rationale: \"Fill before sending\"",
            "  decisions_made:",
            "    - decision: \"TBD\"",
            "      alternatives_considered:",
            "        - \"TBD\"",
            "  blockers_hit:",
            "    - blocker: \"none\"",
            "      workarounds_attempted:",
            "        - \"n/a\"",
            "  suggested_next_steps:",
        ]
    )

    for item in next_steps:
        lines.append(f"    - {_quote(str(item))}")

    return "\n".join(lines) + "\n"


def _default_output_path(project_dir: Path, source: str, target: str) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}_{_sanitize(source)}_to_{_sanitize(target)}.yaml"
    return project_dir / "packets" / "handoff" / name


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a structured handoff packet")
    parser.add_argument("project", help="Project under OACP_HOME/projects")
    parser.add_argument("--from", dest="source_agent", required=True, help="Source agent")
    parser.add_argument("--to", dest="target_agent", required=True, help="Target agent")
    parser.add_argument("--intent", required=True, help="Short handoff intent")
    parser.add_argument("--artifact", action="append", default=[], help="Artifact path or PR ref")
    parser.add_argument("--done", action="append", default=[], help="Definition-of-done item")
    parser.add_argument("--next-step", action="append", default=[], help="Suggested next step")
    parser.add_argument("--output", default=None, help="Output path")
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print packet instead of writing")
    parser.add_argument("--json", action="store_true", dest="json_output", help="JSON output")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    from _oacp_env import resolve_oacp_home
    oacp_dir = resolve_oacp_home(args.oacp_dir)
    project_dir = oacp_dir / "projects" / args.project

    if not project_dir.is_dir():
        print(f"Error: project directory not found: {project_dir}", file=sys.stderr)
        return 2

    artifacts = args.artifact or ["PR #<number>"]
    done_items = args.done or ["Implement agreed deliverables and update tests"]
    next_steps = args.next_step or ["Review packet and begin implementation"]

    packet_data: Dict[str, object] = {
        "source_agent": args.source_agent,
        "target_agent": args.target_agent,
        "intent": args.intent,
        "artifacts_to_review": artifacts,
        "definition_of_done": done_items,
        "suggested_next_steps": next_steps,
    }

    packet_text = render_packet(packet_data)
    validation_errors = validate_handoff_packet_text(packet_text)
    if validation_errors:
        for err in validation_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    output_path = Path(args.output).expanduser() if args.output else _default_output_path(
        project_dir,
        args.source_agent,
        args.target_agent,
    )

    report = {
        "project": args.project,
        "source_agent": args.source_agent,
        "target_agent": args.target_agent,
        "intent": args.intent,
        "dry_run": bool(args.dry_run),
        "output_path": str(output_path),
    }

    if args.dry_run:
        if args.json_output:
            report["packet"] = packet_text
            print(json.dumps(report, indent=2))
        else:
            print(packet_text, end="")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(packet_text, encoding="utf-8")

    if args.json_output:
        print(json.dumps(report, indent=2))
    else:
        print(f"OK: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
