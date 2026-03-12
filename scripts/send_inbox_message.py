#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""send_inbox_message.py — Compose and send protocol-compliant inbox messages.

Builds a valid OACP inbox YAML message, validates it against the schema
(via validate_message.py), and writes it to the recipient's inbox and sender's
outbox directories.

Usage:
    send_inbox_message.py <project> --from <sender> --to <recipient> \\
        --type <type> --subject <subject> --body <body> [options]

Options:
    --from <agent>              Sender agent name
    --to <agent>                Recipient agent name
    --type <type>               Message type (task_request|question|notification|handoff|handoff_complete)
    --subject <text>            Message subject line
    --body <text>               Message body (inline)
    --body-file <path|->        Read body from file or stdin (overrides --body)
    --priority <P0-P3>          Priority level (default: P2)
    --related-pr <number>       Related PR number
    --related-packet <id>       Related packet ID
    --conversation-id <id>      Conversation thread ID
    --parent-message-id <id>    Parent message for threading
    --context-keys <text>       Context keys (inline)
    --context-keys-file <path>  Read context keys from file
    --suffix <text>             Filename disambiguator suffix
    --oacp-dir <path>           Override OACP home directory (default: $OACP_HOME or ~/oacp)
    --dry-run                   Print YAML to stdout, don't write files
    --json                      Output result as JSON
    --quiet                     Suppress success output

Exit codes:
    0 — message sent successfully (or dry-run completed)
    1 — validation error
    2 — usage error or fatal failure

Reference: docs/protocol/inbox_outbox.md, Issue #68/#77
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import string
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import validation from sibling script
_scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_scripts_dir))

from validate_message import (  # noqa: E402
    ALLOWED_PRIORITIES,
    ALLOWED_TYPES,
    parse_duration_to_expires,
    validate_message_dict,
)

# Canonical field order matching templates/inbox_message.template.yaml
FIELD_ORDER = [
    "id",
    "from",
    "to",
    "type",
    "priority",
    "created_at_utc",
    "expires_at",
    "channel",
    "related_packet",
    "related_pr",
    "conversation_id",
    "parent_message_id",
    "context_keys",
    "subject",
    "body",
]

# Fields that use YAML block scalars (|) when multi-line
BLOCK_SCALAR_FIELDS = {"body", "context_keys"}


def generate_message_id(sender: str) -> str:
    """Generate a unique message ID: msg-<compact_ts>-<sender>-<rand4>."""
    now = dt.datetime.now(dt.timezone.utc)
    compact_ts = now.strftime("%Y%m%d%H%M%S")
    rand4 = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"msg-{compact_ts}-{sender}-{rand4}"


def generate_timestamp() -> str:
    """Generate a UTC RFC3339 timestamp (seconds precision)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def generate_filename(
    sender: str, msg_type: str, suffix: Optional[str] = None
) -> str:
    """Generate filename: <ts>_<sender>_<type>_<rand4>[_<suffix>].yaml.

    Includes a 4-char random component to prevent collisions when
    multiple messages are sent in the same second.
    """
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.strftime("%Y%m%d%H%M%S")
    rand4 = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    parts = [ts, sender, msg_type, rand4]
    if suffix:
        parts.append(suffix)
    return "_".join(parts) + ".yaml"


def resolve_body(
    inline: Optional[str], body_file: Optional[str]
) -> str:
    """Resolve body content from --body-file, --body, or piped stdin.

    Priority: --body-file > --body > stdin (if piped).
    Raises ValueError if no body source is available.
    """
    if body_file is not None:
        if body_file == "-":
            if sys.stdin.isatty():
                raise ValueError("--body-file - specified but stdin is a terminal (no piped input)")
            return sys.stdin.read()
        path = Path(body_file)
        if not path.is_file():
            raise ValueError(f"body file not found: {body_file}")
        return path.read_text(encoding="utf-8")

    if inline is not None:
        return inline

    # Check for piped stdin as last resort
    if not sys.stdin.isatty():
        return sys.stdin.read()

    raise ValueError(
        "no message body provided — use --body, --body-file, or pipe to stdin"
    )


def find_parent_message(
    project_dir: Path, sender: str, parent_id: str
) -> Optional[Dict[str, str]]:
    """Search sender's inbox and outbox for a message with the given ID.

    Returns dict with 'conversation_id' if found, else None.
    """
    from validate_message import _parse_simple_yaml

    for subdir in ("inbox", "outbox"):
        search_dir = project_dir / "agents" / sender / subdir
        if not search_dir.is_dir():
            continue
        for yaml_file in search_dir.glob("*.yaml"):
            try:
                raw = yaml_file.read_text(encoding="utf-8")
                data = _parse_simple_yaml(raw)
                if str(data.get("id", "")).strip() == parent_id:
                    result: Dict[str, str] = {}
                    conv_id = str(data.get("conversation_id", "")).strip()
                    if conv_id:
                        result["conversation_id"] = conv_id
                    return result
            except Exception:
                continue
    return None


def build_message_dict(
    sender: str,
    recipient: Any,  # str or list of str
    msg_type: str,
    subject: str,
    body: str,
    priority: str = "P2",
    related_pr: Optional[str] = None,
    related_packet: Optional[str] = None,
    conversation_id: Optional[str] = None,
    parent_message_id: Optional[str] = None,
    context_keys: Optional[str] = None,
    expires_at: Optional[str] = None,
    channel: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a complete message dict with auto-generated ID and timestamp."""
    msg: Dict[str, Any] = {
        "id": generate_message_id(sender),
        "from": sender,
        "to": recipient,
        "type": msg_type,
        "priority": priority,
        "created_at_utc": generate_timestamp(),
        "subject": subject,
        "body": body.rstrip("\n"),
    }

    # Optional fields — only include if provided (non-empty)
    if expires_at:
        msg["expires_at"] = expires_at
    if channel:
        msg["channel"] = channel
    if related_packet:
        msg["related_packet"] = related_packet
    if related_pr:
        msg["related_pr"] = related_pr
    if conversation_id:
        msg["conversation_id"] = conversation_id
    if parent_message_id:
        msg["parent_message_id"] = parent_message_id
    if context_keys:
        msg["context_keys"] = context_keys.rstrip("\n")

    return msg


_YAML_RESERVED_WORDS = frozenset({
    "true", "false", "yes", "no", "on", "off", "null", "~",
    "True", "False", "Yes", "No", "On", "Off", "Null", "NULL",
    "TRUE", "FALSE", "YES", "NO", "ON", "OFF",
})


def _yaml_escape_scalar(value: str) -> str:
    """Quote a scalar value if it contains YAML-special characters."""
    if not value:
        return '""'
    # Newlines in scalar fields must be escaped
    if "\n" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    # YAML reserved bare words need quoting
    if value in _YAML_RESERVED_WORDS:
        return f'"{value}"'
    # Quote if contains characters that need escaping
    needs_quoting = any(
        c in value for c in (":", "#", "{", "}", "[", "]", ",", "&", "*", "?", "|", ">", "'", '"', "%", "@", "`")
    )
    if needs_quoting or value.startswith(("-", " ")) or value != value.strip():
        # Use double quotes with minimal escaping
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def render_yaml(data: Dict[str, Any]) -> str:
    """Render message dict as YAML string with canonical field order.

    Uses block scalars (|) for multi-line body and context_keys fields.
    Supports list values for 'to' field (broadcast).
    Stdlib-only — no PyYAML dependency.
    """
    lines: List[str] = []

    for key in FIELD_ORDER:
        if key not in data:
            continue
        value = data[key]

        # Handle list values (e.g., broadcast 'to' field)
        if isinstance(value, list):
            items = ", ".join(_yaml_escape_scalar(str(v)) for v in value)
            lines.append(f"{key}: [{items}]")
        elif key in BLOCK_SCALAR_FIELDS and isinstance(value, str) and "\n" in value:
            # Block scalar for multi-line content
            lines.append(f"{key}: |")
            for content_line in value.split("\n"):
                lines.append(f"  {content_line}")
        else:
            str_value = str(value) if value is not None else ""
            lines.append(f"{key}: {_yaml_escape_scalar(str_value)}")

    # Trailing newline
    return "\n".join(lines) + "\n"


def write_message_files(
    project_dir: Path,
    sender: str,
    recipient: str,
    msg_type: str,
    yaml_content: str,
    suffix: Optional[str] = None,
) -> Tuple[Path, Path]:
    """Write YAML to recipient's inbox and sender's outbox.

    Creates directories if they don't exist. Returns (inbox_path, outbox_path).
    """
    filename = generate_filename(sender, msg_type, suffix)

    inbox_dir = project_dir / "agents" / recipient / "inbox"
    outbox_dir = project_dir / "agents" / sender / "outbox"

    inbox_dir.mkdir(parents=True, exist_ok=True)
    outbox_dir.mkdir(parents=True, exist_ok=True)

    inbox_path = inbox_dir / filename
    outbox_path = outbox_dir / filename

    inbox_path.write_text(yaml_content, encoding="utf-8")
    outbox_path.write_text(yaml_content, encoding="utf-8")

    return inbox_path, outbox_path


def write_broadcast_files(
    project_dir: Path,
    sender: str,
    recipients: List[str],
    msg_type: str,
    yaml_content: str,
    suffix: Optional[str] = None,
) -> Tuple[List[Path], Path]:
    """Write YAML to each recipient's inbox and a single sender outbox copy.

    Returns (list_of_inbox_paths, outbox_path).
    """
    filename = generate_filename(sender, msg_type, suffix)

    outbox_dir = project_dir / "agents" / sender / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    outbox_path = outbox_dir / filename
    outbox_path.write_text(yaml_content, encoding="utf-8")

    inbox_paths: List[Path] = []
    for recipient in recipients:
        inbox_dir = project_dir / "agents" / recipient / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        inbox_path = inbox_dir / filename
        inbox_path.write_text(yaml_content, encoding="utf-8")
        inbox_paths.append(inbox_path)

    return inbox_paths, outbox_path


def _parse_status_yaml(raw: str) -> Optional[str]:
    """Extract the 'status' field value from a status.yaml file.

    Uses PyYAML if available, falls back to regex extraction.
    Returns the status field value (lowercased) or None if not found.
    """
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            val = loaded.get("status")
            if val is not None:
                return str(val).strip().lower()
        return None
    except ImportError:
        pass
    except Exception:
        pass

    # Regex fallback: match top-level "status: <value>" line
    m = re.match(r"^status:\s*(.+)$", raw, re.MULTILINE)
    if m:
        value = m.group(1).strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value.strip().lower()
    return None


def _check_recipient_status(project_dir: Path, recipient: str, priority: str) -> Optional[str]:
    """Check if recipient has a status.yaml indicating offline. Returns warning string or None."""
    status_path = project_dir / "agents" / recipient / "status.yaml"
    if not status_path.is_file():
        return None
    try:
        raw = status_path.read_text(encoding="utf-8")
        status_value = _parse_status_yaml(raw)
        if status_value == "offline":
            if priority == "P0":
                return f"WARNING: P0 message to OFFLINE agent '{recipient}' — consider out-of-band notification!"
            return f"Note: agent '{recipient}' appears offline"
    except Exception:
        pass
    return None


def send_message(
    project: str,
    sender: str,
    recipient: str,
    msg_type: str,
    subject: str,
    body: str,
    priority: str = "P2",
    related_pr: Optional[str] = None,
    related_packet: Optional[str] = None,
    conversation_id: Optional[str] = None,
    parent_message_id: Optional[str] = None,
    context_keys: Optional[str] = None,
    suffix: Optional[str] = None,
    oacp_dir: Optional[Path] = None,
    dry_run: bool = False,
    expires_at: Optional[str] = None,
    channel: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Orchestrate message building, validation, rendering, and writing.

    recipient can be a single name or comma-separated list for broadcast.
    Returns a report dict with message details and file paths (or dry-run info).
    Raises ValueError for validation errors.
    """
    if oacp_dir is None:
        from _oacp_env import resolve_oacp_home
        oacp_dir = resolve_oacp_home()

    # Parse recipient(s)
    recipients_list = [r.strip() for r in recipient.split(",") if r.strip()]
    is_broadcast = len(recipients_list) > 1

    # Validate agent names don't contain path traversal components
    for name, label in [(sender, "sender")] + [(r, "recipient") for r in recipients_list]:
        if name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError(f"{label} name must not be a path component: {name!r}")

    # Validate suffix is safe for filenames
    if suffix and not _SAFE_SUFFIX_RE.fullmatch(suffix):
        raise ValueError(
            f"suffix must match [A-Za-z0-9._-]{{1,64}}, got: {suffix!r}"
        )

    project_dir = oacp_dir / "projects" / project

    # Handle --in-reply-to: search for parent message to inherit conversation_id
    warnings: List[str] = []
    if in_reply_to:
        parent_message_id = in_reply_to
        parent_info = find_parent_message(project_dir, sender, in_reply_to)
        if parent_info is not None:
            if not conversation_id and parent_info.get("conversation_id"):
                conversation_id = parent_info["conversation_id"]
        else:
            warnings.append(
                f"WARNING: parent message '{in_reply_to}' not found in {sender}'s inbox/outbox "
                f"— thread may be broken! A new conversation_id will be generated."
            )
            if not conversation_id:
                now = dt.datetime.now(dt.timezone.utc)
                conversation_id = f"conv-{now.strftime('%Y%m%d')}-{sender}-1"

    # Determine the 'to' value for the message dict
    to_value: Any = recipients_list if is_broadcast else recipients_list[0]

    # Build the message dict
    msg = build_message_dict(
        sender=sender,
        recipient=to_value,
        msg_type=msg_type,
        subject=subject,
        body=body,
        priority=priority,
        related_pr=related_pr,
        related_packet=related_packet,
        conversation_id=conversation_id,
        parent_message_id=parent_message_id,
        context_keys=context_keys,
        expires_at=expires_at,
        channel=channel,
    )

    # Validate
    errors = validate_message_dict(msg)
    if errors:
        raise ValueError("; ".join(errors))

    # Render YAML
    yaml_content = render_yaml(msg)

    # Check recipient status for P0 warnings
    for r in recipients_list:
        status_warning = _check_recipient_status(project_dir, r, priority)
        if status_warning:
            warnings.append(status_warning)

    report: Dict[str, Any] = {
        "message_id": msg["id"],
        "from": sender,
        "to": recipients_list if is_broadcast else recipients_list[0],
        "type": msg_type,
        "priority": priority,
        "subject": subject,
        "created_at_utc": msg["created_at_utc"],
        "dry_run": dry_run,
    }
    if warnings:
        report["warnings"] = warnings

    if dry_run:
        report["yaml"] = yaml_content
        return report

    # Write files
    if is_broadcast:
        inbox_paths, outbox_path = write_broadcast_files(
            project_dir=project_dir,
            sender=sender,
            recipients=recipients_list,
            msg_type=msg_type,
            yaml_content=yaml_content,
            suffix=suffix,
        )
        report["inbox_paths"] = [str(p) for p in inbox_paths]
        report["outbox_path"] = str(outbox_path)
    else:
        inbox_path, outbox_path = write_message_files(
            project_dir=project_dir,
            sender=sender,
            recipient=recipients_list[0],
            msg_type=msg_type,
            yaml_content=yaml_content,
            suffix=suffix,
        )
        report["inbox_path"] = str(inbox_path)
        report["outbox_path"] = str(outbox_path)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compose and send a protocol-compliant agent inbox message.",
        epilog=(
            "Reference: docs/protocol/inbox_outbox.md (Issue #68/#80)\n\n"
            "Suggested channels: brainstorm, review, deploy, incident\n"
            "(Channels are free-text — these are hints, not enforced.)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project", help="Agent hub project name")
    parser.add_argument("--from", dest="sender", required=True, help="Sender agent name")
    parser.add_argument(
        "--to",
        dest="recipient",
        required=True,
        help="Recipient agent name (comma-separated for broadcast, e.g. 'claude,gemini')",
    )
    parser.add_argument(
        "--type",
        dest="msg_type",
        required=True,
        choices=sorted(ALLOWED_TYPES),
        help="Message type",
    )
    parser.add_argument("--subject", required=True, help="Message subject line")
    parser.add_argument("--body", default=None, help="Message body (inline)")
    parser.add_argument(
        "--body-file",
        default=None,
        help="Read body from file (use '-' for stdin)",
    )
    parser.add_argument(
        "--priority",
        default="P2",
        choices=sorted(ALLOWED_PRIORITIES),
        help="Priority level (default: P2)",
    )
    parser.add_argument("--related-pr", default=None, help="Related PR number")
    parser.add_argument("--related-packet", default=None, help="Related packet ID")
    parser.add_argument("--conversation-id", default=None, help="Conversation thread ID")
    parser.add_argument("--parent-message-id", default=None, help="Parent message ID for threading")
    parser.add_argument(
        "--in-reply-to",
        default=None,
        help="Parent message ID — auto-inherits conversation_id from parent (searches sender inbox/outbox)",
    )
    parser.add_argument("--context-keys", default=None, help="Context keys (inline)")
    parser.add_argument(
        "--context-keys-file",
        default=None,
        help="Read context keys from file",
    )
    parser.add_argument(
        "--expires",
        default=None,
        help="Message expiry duration (e.g. '1h', '2d', '30m') — sets expires_at field",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Channel tag (free-text, e.g. 'review', 'deploy', 'brainstorm', 'incident')",
    )
    parser.add_argument("--suffix", default=None, help="Filename disambiguator suffix")
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print YAML, don't write files")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output result as JSON")
    parser.add_argument("--quiet", action="store_true", help="Suppress success output")

    args = parser.parse_args()

    # Resolve body
    try:
        body = resolve_body(args.body, args.body_file)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Resolve context keys from file if provided
    context_keys = args.context_keys
    if args.context_keys_file:
        ck_path = Path(args.context_keys_file)
        if not ck_path.is_file():
            print(f"ERROR: context-keys file not found: {args.context_keys_file}", file=sys.stderr)
            return 2
        context_keys = ck_path.read_text(encoding="utf-8").rstrip("\n")

    # Parse --expires into ISO 8601 expires_at
    expires_at = None
    if args.expires:
        try:
            expires_at = parse_duration_to_expires(args.expires)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    # Send
    try:
        report = send_message(
            project=args.project,
            sender=args.sender,
            recipient=args.recipient,
            msg_type=args.msg_type,
            subject=args.subject,
            body=body,
            priority=args.priority,
            related_pr=args.related_pr,
            related_packet=args.related_packet,
            conversation_id=args.conversation_id,
            parent_message_id=args.parent_message_id,
            context_keys=context_keys,
            suffix=args.suffix,
            oacp_dir=Path(args.oacp_dir) if args.oacp_dir else None,
            dry_run=args.dry_run,
            expires_at=expires_at,
            channel=args.channel,
            in_reply_to=args.in_reply_to,
        )
    except ValueError as exc:
        if args.json_output:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Print warnings
    for warning in report.get("warnings", []):
        print(warning, file=sys.stderr)

    # Output
    if args.json_output:
        print(json.dumps(report, indent=2))
    elif args.dry_run:
        print(report["yaml"], end="")
    elif not args.quiet:
        print(f"OK: {report['message_id']}")
        if "inbox_paths" in report:
            for p in report["inbox_paths"]:
                print(f"  inbox:  {p}")
        elif "inbox_path" in report:
            print(f"  inbox:  {report['inbox_path']}")
        print(f"  outbox: {report['outbox_path']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
