#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Schema validation for agent inbox/outbox YAML messages."""

from __future__ import annotations

import argparse
import datetime as dt
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from _oacp_constants import AGENT_RE

from handoff_schema import (  # noqa: E402
    validate_handoff_complete_text,
    validate_handoff_packet_text,
)
from message_signing import (  # noqa: E402
    auth_structure_errors,
    split_signed_message,
)

REQUIRED_FIELDS = (
    "id",
    "from",
    "to",
    "type",
    "priority",
    "created_at_utc",
    "subject",
    "body",
)
OPTIONAL_FIELDS = (
    "related_packet",
    "related_pr",
    "conversation_id",
    "parent_message_id",
    "context_keys",
    "expires_at",
    "channel",
    "autonomy_hint",
    "model",
    "turns",
    "input_tokens",
    "output_tokens",
    "wall_time_s",
    "est_cost_usd",
    # Signed-message auth trailer (v0.4.0 message signing).
    # Strict all-or-none: when present it must be a structurally complete
    # detached-JWS container and the final physical line of the file.
    "auth",
)
ALLOWED_FIELDS = set(REQUIRED_FIELDS + OPTIONAL_FIELDS)
ALLOWED_TYPES = {
    "task_request",
    "question",
    "notification",
    "follow_up",
    "handoff",
    "handoff_complete",
    "review_request",
    "review_feedback",
    "review_addressed",
    "review_lgtm",
    "brainstorm_request",
    "brainstorm_followup",
}
ALLOWED_PRIORITIES = {"P0", "P1", "P2", "P3"}
ALLOWED_AUTONOMY_HINTS = frozenset({"auto_proceed"})
REVIEW_TELEMETRY_FIELDS = frozenset({
    "model",
    "turns",
    "input_tokens",
    "output_tokens",
    "wall_time_s",
    "est_cost_usd",
})
REVIEW_TELEMETRY_TYPES = frozenset({
    "review_feedback",
    "review_lgtm",
    "review_addressed",
})
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CONVERSATION_ID_RE = re.compile(r"^conv-\d{8}-[A-Za-z0-9._-]{1,64}-\d{1,6}$")

_DURATION_RE = re.compile(r"^(\d+)\s*([hHdDmM])$")


def parse_duration_to_expires(duration: str, base_time: Optional[dt.datetime] = None) -> str:
    """Parse a human-friendly duration (e.g. '1h', '2d', '30m') into ISO 8601 UTC expires_at.

    Raises ValueError if the format is invalid.
    """
    m = _DURATION_RE.match(duration.strip())
    if not m:
        raise ValueError(
            f"invalid duration format: {duration!r} — expected <number><unit> where unit is h/d/m"
        )
    amount = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "h":
        delta = dt.timedelta(hours=amount)
    elif unit == "d":
        delta = dt.timedelta(days=amount)
    elif unit == "m":
        delta = dt.timedelta(minutes=amount)
    else:
        raise ValueError(f"unknown duration unit: {unit}")

    base = base_time or dt.datetime.now(dt.timezone.utc)
    expires = base + delta
    return expires.strftime("%Y-%m-%dT%H:%M:%SZ")


class MessageValidationError(Exception):
    """Raised for parse/validation failures."""


def _strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    out: List[str] = []
    for ch in value:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            break
        out.append(ch)
    return "".join(out).strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_simple_yaml(raw: str) -> Dict[str, Any]:
    """Fallback parser for simple top-level mapping with optional block scalar."""
    lines = raw.splitlines()
    data: Dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if line.startswith((" ", "\t")):
            raise MessageValidationError(f"line {i + 1}: unexpected indentation at top level")
        if ":" not in line:
            raise MessageValidationError(f"line {i + 1}: expected key: value")

        key, rest = line.split(":", 1)
        key = key.strip()
        if not FIELD_RE.fullmatch(key):
            raise MessageValidationError(f"line {i + 1}: invalid field name '{key}'")
        rest = rest.lstrip()

        if rest in ("|", "|-", "|+"):
            i += 1
            block_lines: List[str] = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.startswith("  "):
                    block_lines.append(nxt[2:])
                    i += 1
                    continue
                if nxt == "":
                    block_lines.append("")
                    i += 1
                    continue
                break
            data[key] = "\n".join(block_lines)
            continue

        # Handle YAML flow sequence: [item1, item2, ...]
        stripped_rest = _strip_inline_comment(rest)
        if stripped_rest.startswith("[") and stripped_rest.endswith("]"):
            inner = stripped_rest[1:-1]
            items = [_unquote(item.strip()) for item in inner.split(",") if item.strip()]
            data[key] = items
            i += 1
            continue

        value = _unquote(stripped_rest)
        data[key] = value
        i += 1
    return data


def _load_message(raw: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore

        loaded = yaml.load(raw, Loader=yaml.BaseLoader)
    except ImportError:
        loaded = _parse_simple_yaml(raw)
    except Exception as exc:
        raise MessageValidationError(f"YAML parse error: {exc}") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise MessageValidationError("top-level YAML must be a mapping/object")
    return loaded


def _as_scalar_str(data: Dict[str, Any], key: str, errors: List[str]) -> str:
    if key not in data:
        errors.append(f"missing required field: {key}")
        return ""

    value = data.get(key)
    if isinstance(value, (dict, list)):
        errors.append(f"field '{key}' must be a scalar string, not nested YAML")
        return ""
    if value is None:
        errors.append(f"field '{key}' must be non-empty")
        return ""
    result = str(value).strip()
    if not result:
        errors.append(f"field '{key}' must be non-empty")
    return result


CHANNEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _agent_pattern_error(field: str, value: Optional[str] = None) -> str:
    if value is None:
        return f"field '{field}' must match {AGENT_RE.pattern}"
    return f"field '{field}' list entry '{value}' must match {AGENT_RE.pattern}"


def _is_nonnegative_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return bool(re.fullmatch(r"\d+", str(value).strip()))


def _is_nonnegative_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def validate_message_dict(data: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    unknown = sorted(set(data.keys()) - ALLOWED_FIELDS)
    if unknown:
        errors.append(f"unknown field(s): {', '.join(unknown)}")

    msg_id = _as_scalar_str(data, "id", errors)
    sender = _as_scalar_str(data, "from", errors)
    msg_type = _as_scalar_str(data, "type", errors)
    priority = _as_scalar_str(data, "priority", errors)
    created_at = _as_scalar_str(data, "created_at_utc", errors)
    subject = _as_scalar_str(data, "subject", errors)
    body = _as_scalar_str(data, "body", errors)

    # Validate 'to' field — accepts string or list of strings (broadcast)
    to_value = data.get("to")
    recipients: List[str] = []
    if to_value is None:
        errors.append("missing required field: to")
    elif isinstance(to_value, list):
        if len(to_value) == 0:
            errors.append("field 'to' list must not be empty")
        elif len(to_value) > 10:
            errors.append("field 'to' list exceeds maximum of 10 recipients")
        else:
            for item in to_value:
                s = str(item).strip()
                if not s:
                    errors.append("field 'to' list contains empty entry")
                elif not AGENT_RE.fullmatch(s):
                    errors.append(_agent_pattern_error("to", s))
                else:
                    recipients.append(s)
            if sender and sender in recipients:
                errors.append("field 'to' list must not include the sender")
        # Handoff types are point-to-point only
        if msg_type in ("handoff", "handoff_complete") and len(to_value) > 1:
            errors.append(f"type '{msg_type}' does not support broadcast (multiple recipients)")
    elif isinstance(to_value, dict):
        errors.append("field 'to' must be a string or list of strings, not a mapping")
    else:
        recipient_str = str(to_value).strip()
        if not recipient_str:
            errors.append("field 'to' must be non-empty")
        elif not AGENT_RE.fullmatch(recipient_str):
            errors.append(_agent_pattern_error("to"))
        else:
            recipients.append(recipient_str)

    for optional in OPTIONAL_FIELDS:
        value = data.get(optional, "")
        if isinstance(value, (dict, list)):
            errors.append(f"field '{optional}' must be a scalar value")

    if sender and not AGENT_RE.fullmatch(sender):
        errors.append(_agent_pattern_error("from"))
    if msg_type and msg_type not in ALLOWED_TYPES:
        errors.append(f"field 'type' must be one of: {', '.join(sorted(ALLOWED_TYPES))}")
    if priority and priority not in ALLOWED_PRIORITIES:
        errors.append(f"field 'priority' must be one of: {', '.join(sorted(ALLOWED_PRIORITIES))}")

    present_telemetry = REVIEW_TELEMETRY_FIELDS.intersection(data)
    if present_telemetry and msg_type not in REVIEW_TELEMETRY_TYPES:
        errors.append(
            "review telemetry fields are allowed only for: "
            f"{', '.join(sorted(REVIEW_TELEMETRY_TYPES))}"
        )
    if "model" in data:
        model = data.get("model")
        if not isinstance(model, str) or not model.strip():
            errors.append("field 'model' must be a non-empty string")
    for field in ("turns", "input_tokens", "output_tokens"):
        if field in data and not _is_nonnegative_int(data.get(field)):
            errors.append(f"field '{field}' must be a non-negative integer")
    for field in ("wall_time_s", "est_cost_usd"):
        if field in data and not _is_nonnegative_number(data.get(field)):
            errors.append(f"field '{field}' must be a non-negative number")

    autonomy_hint_value = data.get("autonomy_hint", "")
    if not isinstance(autonomy_hint_value, (dict, list)):
        autonomy_hint = str(autonomy_hint_value or "").strip()
        if autonomy_hint and autonomy_hint not in ALLOWED_AUTONOMY_HINTS:
            errors.append(
                "field 'autonomy_hint' must be one of: "
                f"{', '.join(sorted(ALLOWED_AUTONOMY_HINTS))}"
            )

    if created_at:
        if not UTC_RE.fullmatch(created_at):
            errors.append("field 'created_at_utc' must be UTC RFC3339 seconds format: YYYY-MM-DDTHH:MM:SSZ")
        else:
            try:
                dt.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                errors.append("field 'created_at_utc' is not a valid UTC timestamp")

    related_pr = str(data.get("related_pr", "") or "").strip()
    if related_pr and not related_pr.isdigit():
        errors.append("field 'related_pr' must be empty or a numeric PR id")

    conversation_id = str(data.get("conversation_id", "") or "").strip()
    if conversation_id and not CONVERSATION_ID_RE.fullmatch(conversation_id):
        errors.append(
            "field 'conversation_id' must match pattern conv-<YYYYMMDD>-<agent>-<seq>"
        )

    parent_msg = str(data.get("parent_message_id", "") or "").strip()
    if parent_msg and len(parent_msg) > 200:
        errors.append("field 'parent_message_id' is too long (max 200 chars)")

    context_keys = data.get("context_keys", "")
    if context_keys is not None and not isinstance(context_keys, str):
        errors.append("field 'context_keys' must be a string (use YAML block scalar)")

    # Validate expires_at (optional, ISO 8601 UTC)
    expires_at = str(data.get("expires_at", "") or "").strip()
    if expires_at:
        if not UTC_RE.fullmatch(expires_at):
            errors.append("field 'expires_at' must be UTC RFC3339 seconds format: YYYY-MM-DDTHH:MM:SSZ")
        else:
            try:
                dt.datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                errors.append("field 'expires_at' is not a valid UTC timestamp")

    # Validate channel (optional, alphanumeric + hyphens + underscores, max 64)
    channel = str(data.get("channel", "") or "").strip()
    if channel:
        if not CHANNEL_RE.fullmatch(channel):
            errors.append("field 'channel' must be 1-64 chars of alphanumeric, hyphens, or underscores")

    if msg_id and len(msg_id) > 200:
        errors.append("field 'id' is too long (max 200 chars)")
    if subject and len(subject) > 200:
        errors.append("field 'subject' is too long (max 200 chars)")
    if body and len(body) > 20000:
        errors.append("field 'body' is too long (max 20000 chars)")

    if msg_type == "handoff" and body:
        for err in validate_handoff_packet_text(body):
            errors.append(f"handoff body: {err}")
    if msg_type == "handoff_complete" and body:
        for err in validate_handoff_complete_text(body):
            errors.append(f"handoff_complete body: {err}")

    # Signed-message auth trailer: strict structural validation (framing +
    # locked JOSE profile), no crypto — verification is receiver-side.
    if "auth" in data:
        auth_value = data.get("auth")
        if not isinstance(auth_value, (dict, list)):
            errors.extend(auth_structure_errors(str(auth_value or "")))

    return errors


def validate_message_file(path: Path) -> List[str]:
    if not path.is_file():
        return [f"message file does not exist: {path}"]
    try:
        # Bytes, not read_text(): text mode applies universal-newline
        # normalization, which would hide CRLF byte differences from the
        # signed-message framing checks (byte covenant).
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode("utf-8")
    except Exception as exc:
        return [f"failed to read file: {exc}"]

    try:
        data = _load_message(raw)
    except MessageValidationError as exc:
        return [str(exc)]

    errors = validate_message_dict(data)

    # Raw-position rule for signed messages, checked against the exact
    # on-disk bytes: the auth trailer must be the final physical line,
    # followed by exactly one LF. CRLF or any other byte variation fails.
    if "auth" in data:
        _, trailer_value = split_signed_message(raw_bytes)
        if trailer_value is None:
            errors.append(
                "field 'auth' must be the final physical line of the file: "
                'auth: "<base64url>" followed by exactly one LF (no CRLF)'
            )
        elif str(data.get("auth")) != trailer_value:
            errors.append(
                "field 'auth' parsed value does not match the raw trailer line"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an agent hub inbox/outbox message YAML file.")
    parser.add_argument("message_file", help="Path to message YAML")
    parser.add_argument("--quiet", action="store_true", help="Suppress success output")
    args = parser.parse_args()

    path = Path(args.message_file)
    errors = validate_message_file(path)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"OK: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
