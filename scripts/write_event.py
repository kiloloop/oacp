#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Write an org-memory event file with proper naming and frontmatter.

Creates a timestamped event file at $OACP_HOME/org-memory/events/ with
OACP-compliant frontmatter.

Usage:
    write_event.py --agent <agent> --project <project> --type <type> \\
        --slug <slug> --body <body> [options]

Options:
    --agent <name>              Agent creating the event (required)
    --project <name>            Originating project (required)
    --type <type>               Event type: decision, event, rule (required)
    --slug <text>               Short slug for filename (required)
    --body <text>               Event body (inline)
    --body-file <path|->        Read body from file or stdin
    --source-ref <id>           Provenance ID for dual-write reconciliation
    --related <items>           Cross-references (comma-separated or JSON array)
    --supersedes <event-path>   Event path this entry overrides
    --oacp-dir <path>           Override OACP home directory
    --dry-run                   Print event to stdout, don't write file

Exit codes:
    0 — event written (or dry-run completed)
    1 — validation error
    2 — usage error
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from _oacp_constants import utc_now_iso

ALLOWED_TYPES = ("decision", "event", "rule")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _normalize_related(raw: str) -> List[str]:
    """Parse a --related value into a list of strings.

    Accepts both comma-separated strings ("PR #1, PR #2") and pre-encoded
    JSON arrays ('["PR #1", "PR #2"]').  Returns a list of stripped,
    non-empty strings.
    """
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(item).strip() for item in parsed if str(item).strip()]
        except (json.JSONDecodeError, TypeError):
            pass  # fall through to comma split
    return [r.strip() for r in raw.split(",") if r.strip()]


def _validate_slug(slug: str) -> None:
    """Validate slug format: lowercase alphanumeric with hyphens."""
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid slug '{slug}': must be lowercase alphanumeric with hyphens, "
            f"1-64 chars, start/end with alphanumeric"
        )


def _resolve_body(inline: Optional[str], body_file: Optional[str]) -> str:
    """Resolve body from --body-file, --body, or piped stdin."""
    if body_file is not None:
        if body_file == "-":
            if sys.stdin.isatty():
                raise ValueError("--body-file - specified but stdin is a terminal")
            return sys.stdin.read().rstrip("\n")
        path = Path(body_file)
        if not path.is_file():
            raise ValueError(f"body file not found: {body_file}")
        return path.read_text(encoding="utf-8").rstrip("\n")

    if inline is not None:
        return inline.rstrip("\n")

    if not sys.stdin.isatty():
        return sys.stdin.read().rstrip("\n")

    raise ValueError("no body provided — use --body, --body-file, or pipe to stdin")


def build_event(
    agent: str,
    project: str,
    event_type: str,
    slug: str,
    body: str,
    source_ref: Optional[str] = None,
    related: Optional[List[str]] = None,
    supersedes: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> dict:
    """Build event metadata and content.

    Returns dict with 'filename', 'content', and 'metadata'.
    """
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)

    _validate_slug(slug)

    if event_type not in ALLOWED_TYPES:
        raise ValueError(f"Invalid type '{event_type}': must be one of {ALLOWED_TYPES}")

    filename = now.strftime("%Y%m%d-%H%M%S") + f"-{slug}.md"
    date_str = now.strftime("%Y-%m-%d")
    timestamp_str = utc_now_iso(now)

    # Build frontmatter
    lines = [
        "---",
        f"created_at_utc: {timestamp_str}",
        f"date: {date_str}",
        f"agent: {agent}",
        f"project: {project}",
        f"type: {event_type}",
    ]

    if source_ref:
        lines.append(f"source_ref: {source_ref}")
    if related:
        quoted = ", ".join(f'"{item}"' for item in related)
        lines.append(f"related: [{quoted}]")
    if supersedes:
        lines.append(f"supersedes: {supersedes}")

    lines.append("---")
    lines.append("")
    lines.append(body)
    lines.append("")

    content = "\n".join(lines)

    return {
        "filename": filename,
        "content": content,
        "metadata": {
            "created_at_utc": timestamp_str,
            "date": date_str,
            "agent": agent,
            "project": project,
            "type": event_type,
        },
    }


def write_event_file(oacp_root: Path, event: dict) -> Path:
    """Write event file to org-memory/events/. Returns the written path."""
    events_dir = oacp_root / "org-memory" / "events"
    if not events_dir.is_dir():
        raise ValueError(
            f"Events directory not found: {events_dir}\n"
            f"Run `oacp org-memory init` first."
        )

    path = events_dir / event["filename"]
    if path.exists():
        raise ValueError(f"Event file already exists: {path}")

    path.write_text(event["content"], encoding="utf-8")
    return path


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write an org-memory event file.",
    )
    parser.add_argument("--agent", required=True, help="Agent creating the event")
    parser.add_argument("--project", required=True, help="Originating project")
    parser.add_argument(
        "--type",
        dest="event_type",
        required=True,
        choices=sorted(ALLOWED_TYPES),
        help="Event type",
    )
    parser.add_argument(
        "--slug",
        required=True,
        help="Short slug for filename (lowercase, hyphens, e.g. 'api-convention')",
    )
    parser.add_argument("--body", default=None, help="Event body (inline)")
    parser.add_argument(
        "--body-file",
        default=None,
        help="Read body from file (use '-' for stdin)",
    )
    parser.add_argument("--source-ref", default=None, help="Provenance ID")
    parser.add_argument(
        "--related",
        default=None,
        help="Cross-references: comma-separated or JSON array (e.g. 'PR #43,event/foo' or '[\"PR #43\"]')",
    )
    parser.add_argument(
        "--supersedes",
        default=None,
        help="Event path this entry overrides",
    )
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print event to stdout, don't write file",
    )
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    from _oacp_env import resolve_oacp_home

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)

    # Resolve body
    try:
        body = _resolve_body(args.body, args.body_file)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Parse related items
    related = None
    if args.related:
        related = _normalize_related(args.related)

    # Build event
    try:
        event = build_event(
            agent=args.agent,
            project=args.project,
            event_type=args.event_type,
            slug=args.slug,
            body=body,
            source_ref=args.source_ref,
            related=related,
            supersedes=args.supersedes,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(event["content"], end="")
        return 0

    # Write
    try:
        path = write_event_file(oacp_root, event)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
