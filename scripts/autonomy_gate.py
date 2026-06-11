#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Receiver autonomy gate evaluator.

Evaluates an inbox message plus receiver config against the OACP auto-review
scope-envelope contract. The module is intentionally separate from
``check_quality_gate.py``, which evaluates review findings packets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from validate_message import validate_message_dict


VALID_MODES = {"always_pause", "auto_review"}
POLICY_ACTIONS = {"pause"}
NUMERIC_THRESHOLD_KEYS = ("max_estimated_minutes", "max_expected_files_touched")
POLICY_THRESHOLD_KEYS = (
    "destructive_ops",
    "external_side_effects",
    "auth_config_or_secrets",
    "dependency_changes",
    "public_visibility",
    "git_push_or_deploy",
)

LEGACY_PROFILE_BOOL_FIELDS = (
    "destructive_ops",
    "external_side_effects",
    "touches_auth_config_or_secrets",
    "touches_dependencies",
    "public_visibility",
)
SIDE_EFFECT_BOOL_FIELDS = (
    "creates_or_updates_pr",
    "comments_on_github",
    "commits_changes",
    "sends_oacp_reply_only",
)
COVERABLE_CONTINUATION_FIELDS = (
    "creates_or_updates_pr",
    "comments_on_github",
    "commits_changes",
)

FINAL_STATES = {"done", "paused", "blocked", "superseded", "error"}

DESTRUCTIVE_PATTERNS = (
    ("rm -rf", re.compile(r"(?<!\w)rm\s+-rf(?!\w)", re.IGNORECASE)),
    ("--force", re.compile(r"(?<![\w-])--force(?![\w-])", re.IGNORECASE)),
    ("--no-verify", re.compile(r"(?<![\w-])--no-verify(?![\w-])", re.IGNORECASE)),
    (
        "--dangerously-skip-permissions",
        re.compile(
            r"(?<![\w-])--dangerously-skip-permissions(?![\w-])",
            re.IGNORECASE,
        ),
    ),
)

SIDE_EFFECT_VERB_PATTERNS = (
    ("deploy", re.compile(r"(?<![\w/-])deploy(?![\w/-])", re.IGNORECASE)),
    ("publish", re.compile(r"(?<![\w/-])publish(?![\w/-])", re.IGNORECASE)),
    ("merge", re.compile(r"(?<![\w/-])merge(?![\w/-])", re.IGNORECASE)),
)

NON_DEMOTABLE_SIDE_EFFECT_PATTERNS = (
    ("push to main", re.compile(r"\bpush(?:es|ing)?\s+to\s+main\b", re.IGNORECASE)),
    (
        "rotate credentials",
        re.compile(r"\brotat(?:e|es|ing)\s+credentials?\b", re.IGNORECASE),
    ),
    (
        "install dependency",
        re.compile(r"\binstall(?:s|ing)?\b.*\bdependenc(?:y|ies)\b", re.IGNORECASE),
    ),
)

SENSITIVE_SCOPE_PATTERNS = (
    ("auth", re.compile(r"(?<![\w/-])auth(?![\w/-])", re.IGNORECASE)),
    ("secrets", re.compile(r"(?<![\w/-])secrets?(?![\w/-])", re.IGNORECASE)),
    ("credentials", re.compile(r"(?<![\w/-])credentials?(?![\w/-])", re.IGNORECASE)),
    ("pricing", re.compile(r"(?<![\w/-])pricing(?![\w/-])", re.IGNORECASE)),
    ("commercial", re.compile(r"(?<![\w/-])commercial(?![\w/-])", re.IGNORECASE)),
    (
        "config",
        re.compile(
            r"\bconfig(?:uration)?\s+(?:file|files|setting|settings|template|templates|yaml|yml)\b"
            r"|\b(?:project|workspace|runtime|agent)\s+config(?:uration)?\b"
            r"|\bconfig\.ya?ml\b",
            re.IGNORECASE,
        ),
    ),
    (
        "public repo",
        re.compile(r"\bpublic\s+repositor(?:y|ies)|\bpublic\s+repo\b", re.IGNORECASE),
    ),
    (
        "memory SSOT",
        re.compile(r"\bmemory\s+ssot\b|\bmemory\s+single\s+source\b", re.IGNORECASE),
    ),
)

AMBIGUOUS_SCOPE_PATTERNS = (
    ("all files", re.compile(r"\ball\s+files\b", re.IGNORECASE)),
)


class AutonomyConfigError(ValueError):
    """Raised when receiver autonomy config is malformed."""


class TaskProfileError(ValueError):
    """Raised when a task_profile block exists but cannot be normalized."""


def load_yaml_file(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def validate_receiver_config(config: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    autonomy = config.get("autonomy")
    if not isinstance(autonomy, dict):
        return ["field 'autonomy' must be a mapping"]

    mode = str(autonomy.get("default_mode") or "")
    if mode not in VALID_MODES:
        errors.append("autonomy.default_mode must be always_pause or auto_review")

    thresholds = autonomy.get("auto_review_thresholds")
    if mode == "auto_review" and not isinstance(thresholds, dict):
        errors.append("autonomy.auto_review_thresholds must be a mapping")
    if isinstance(thresholds, dict):
        for key in NUMERIC_THRESHOLD_KEYS:
            value = thresholds.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"autonomy.auto_review_thresholds.{key} invalid")
        for key in POLICY_THRESHOLD_KEYS:
            if str(thresholds.get(key) or "") not in POLICY_ACTIONS:
                errors.append(f"autonomy.auto_review_thresholds.{key} must be pause")

    allow_without_profile = autonomy.get("allow_without_task_profile", [])
    if allow_without_profile is None:
        allow_without_profile = []
    if not isinstance(allow_without_profile, list):
        errors.append("autonomy.allow_without_task_profile must be a list")

    grants = autonomy.get("continuation_grants", {})
    if grants is None:
        grants = {}
    if not isinstance(grants, dict):
        errors.append("autonomy.continuation_grants must be a mapping")
    elif "enabled" in grants and not isinstance(grants.get("enabled"), bool):
        errors.append("autonomy.continuation_grants.enabled must be a boolean")

    return errors


def receiver_policy(config: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    errors = validate_receiver_config(config)
    if errors:
        raise AutonomyConfigError("; ".join(errors))

    autonomy = config["autonomy"]
    thresholds = autonomy.get("auto_review_thresholds") or {}
    continuation = autonomy.get("continuation_grants") or {}
    return str(autonomy.get("default_mode")), {
        "thresholds": thresholds,
        "allow_without_task_profile": list(
            autonomy.get("allow_without_task_profile") or []
        ),
        "continuation_grants_enabled": bool(continuation.get("enabled", False)),
    }


def extract_task_profile(body: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (task_profile, error_code) from a markdown/YAML body."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)task_profile:\s*(.*)$", line)
        if not match:
            continue

        base_indent = len(match.group(1))
        block = [line[base_indent:]]
        for next_line in lines[index + 1:]:
            if not next_line.strip():
                block.append("")
                continue
            indent = len(next_line) - len(next_line.lstrip(" "))
            if indent <= base_indent:
                break
            block.append(next_line[base_indent:])

        try:
            data = yaml.safe_load("\n".join(block))
        except yaml.YAMLError:
            return None, "task_profile_unparsable"
        if not isinstance(data, dict) or not isinstance(data.get("task_profile"), dict):
            return None, "task_profile_unparsable"
        return data["task_profile"], None
    return None, None


def _bool_value(profile: Dict[str, Any], key: str) -> bool:
    value = profile.get(key, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    if key in profile:
        raise TaskProfileError(f"task_profile.{key} must be boolean")
    return False


def _int_value(profile: Dict[str, Any], key: str) -> int:
    value = profile.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise TaskProfileError(f"task_profile.{key} must be a non-negative integer")


def normalize_scope_envelope(profile: Dict[str, Any]) -> Dict[str, Any]:
    envelope: Dict[str, Any] = {
        "estimated_minutes": _int_value(profile, "estimated_minutes"),
        "expected_files_touched": _int_value(profile, "expected_files_touched"),
        "risk_tier": str(profile.get("risk_tier") or ""),
    }
    for key in LEGACY_PROFILE_BOOL_FIELDS + SIDE_EFFECT_BOOL_FIELDS:
        envelope[key] = _bool_value(profile, key)

    grants = profile.get("continuation_grants", {})
    if grants is None:
        grants = {}
    if not isinstance(grants, dict):
        raise TaskProfileError("task_profile.continuation_grants must be a mapping")
    envelope["continuation_grants"] = grants
    return envelope


def first_match(
    patterns: Sequence[Tuple[str, re.Pattern[str]]],
    body: str,
) -> Optional[str]:
    for label, pattern in patterns:
        if pattern.search(body):
            return label
    return None


def message_sha256(
    message: Dict[str, Any],
    message_path: Optional[Path] = None,
) -> str:
    """Return the raw YAML hash when a path is available, otherwise a stable fallback."""
    if message_path is not None:
        return hashlib.sha256(message_path.read_bytes()).hexdigest()
    serialized = yaml.safe_dump(message, sort_keys=True, allow_unicode=False).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def message_expired(
    message: Dict[str, Any],
    now_utc: Optional[dt.datetime] = None,
) -> bool:
    expires_at = str(message.get("expires_at") or "").strip()
    if not expires_at:
        return False
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    # validate_message_dict enforces Z-only format; ValueError here is defensive.
    expires = dt.datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc
    )
    return now >= expires


def prior_auto_accept_exists(
    message_id: str,
    receiver: str,
    audit_dir: Optional[Path],
) -> bool:
    if not message_id or audit_dir is None or not audit_dir.is_dir():
        return False
    for audit_path in audit_dir.glob("*.yaml"):
        try:
            audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(audit, dict):
            continue
        if audit.get("message_id") != message_id:
            continue
        if audit.get("receiver") != receiver:
            continue
        if audit.get("decision") == "auto_accepted":
            return True
    return False


def side_effect_notes_for_allowed_type(body: str) -> List[Dict[str, str]]:
    notes: List[Dict[str, str]] = []
    for label, pattern in SIDE_EFFECT_VERB_PATTERNS:
        if pattern.search(body):
            notes.append({
                "code": "side_effect_verb_demoted_for_profileless_type",
                "matched_pattern": label,
            })
    return notes


def obvious_no_profile_risk(body: str) -> bool:
    patterns = (
        SIDE_EFFECT_VERB_PATTERNS
        + NON_DEMOTABLE_SIDE_EFFECT_PATTERNS
        + (
            ("pull request", re.compile(r"\bpull\s+request\b|\bPR\b")),
            ("github", re.compile(r"\bgithub\b", re.IGNORECASE)),
            ("commit", re.compile(r"\bcommit(?:s|ted|ting)?\b", re.IGNORECASE)),
        )
    )
    return first_match(patterns, body) is not None


def _grant_scope(
    grant: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    scope = grant.get("scope")
    if not isinstance(scope, dict):
        return None, "continuation_grant_missing_scope"

    normalized: Dict[str, Any] = {}
    for key in ("max_actual_minutes", "max_actual_files_touched"):
        value = scope.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, f"{key}_invalid"
        normalized[key] = value
    for key in COVERABLE_CONTINUATION_FIELDS:
        value = scope.get(key, False)
        if not isinstance(value, bool):
            return None, f"{key}_invalid"
        normalized[key] = value
    return normalized, None


def evaluate_continuation_grant(
    message: Dict[str, Any],
    envelope: Dict[str, Any],
    continuation_enabled: bool,
) -> Dict[str, Any]:
    grants = envelope.get("continuation_grants") or {}
    grant = grants.get("approved_thread_continuation")
    result: Dict[str, Any] = {
        "present": isinstance(grant, dict),
        "enabled": continuation_enabled,
        "kind": "approved_thread_continuation",
        "decision": "not_present",
        "reason_codes": [],
        "scope": None,
    }
    if not isinstance(grant, dict):
        return result

    if not continuation_enabled:
        result["decision"] = "ignored_disabled"
        result["reason_codes"] = ["continuation_grant_ignored_disabled"]
        return result

    has_thread = bool(message.get("parent_message_id") or message.get("conversation_id"))
    if not has_thread:
        result["decision"] = "invalid"
        result["reason_codes"] = ["continuation_grant_missing_thread"]
        return result

    scope, error = _grant_scope(grant)
    if error:
        result["decision"] = "invalid"
        result["reason_codes"] = [error]
        return result

    result["decision"] = "accepted"
    result["reason_codes"] = ["continuation_grant_accepted"]
    result["scope"] = scope
    return result


def _side_effect_reasons(
    envelope: Dict[str, Any],
    grant_result: Dict[str, Any],
) -> List[str]:
    reasons: List[str] = []
    grant_scope = grant_result.get("scope") if grant_result.get("decision") == "accepted" else None
    grant_covers_side_effect = isinstance(grant_scope, dict) and any(
        grant_scope.get(key) is True for key in COVERABLE_CONTINUATION_FIELDS
    )

    if envelope["external_side_effects"] and not grant_covers_side_effect:
        reasons.append("external_side_effects_pause")
    for key in COVERABLE_CONTINUATION_FIELDS:
        if not envelope[key]:
            continue
        if isinstance(grant_scope, dict) and grant_scope.get(key) is True:
            continue
        reasons.append(f"{key}_pause")
    return reasons


def _threshold_reasons(
    envelope: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> List[str]:
    reasons: List[str] = []
    if envelope["estimated_minutes"] > thresholds["max_estimated_minutes"]:
        reasons.append("estimated_minutes_exceeds_threshold")
    if envelope["expected_files_touched"] > thresholds["max_expected_files_touched"]:
        reasons.append("expected_files_touched_exceeds_threshold")
    return reasons


def _actual_side_effects(actuals: Dict[str, Any]) -> Dict[str, bool]:
    side_effects = actuals.get("side_effects_actual") or {}
    if not isinstance(side_effects, dict):
        side_effects = {}
    return {key: bool(side_effects.get(key, False)) for key in COVERABLE_CONTINUATION_FIELDS}


def evaluate_threshold_checkpoint(
    envelope: Optional[Dict[str, Any]],
    grant_result: Dict[str, Any],
    actuals: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    checkpoint: Dict[str, Any] = {
        "evaluated": False,
        "actual_minutes": None,
        "actual_files_touched": None,
        "side_effects_actual": {},
        "breached": False,
        "action": "not_evaluated",
    }
    if not actuals or not envelope:
        return checkpoint

    actual_minutes = int(actuals.get("actual_minutes") or 0)
    actual_files = int(actuals.get("actual_files_touched") or 0)
    side_effects = _actual_side_effects(actuals)
    grant_scope = grant_result.get("scope") if grant_result.get("decision") == "accepted" else None

    max_minutes = envelope["estimated_minutes"]
    max_files = envelope["expected_files_touched"]
    if isinstance(grant_scope, dict):
        max_minutes = grant_scope["max_actual_minutes"]
        max_files = grant_scope["max_actual_files_touched"]

    breached = actual_minutes > max_minutes or actual_files > max_files
    for key, actual in side_effects.items():
        if not actual:
            continue
        if envelope.get(key) is True:
            continue
        if isinstance(grant_scope, dict) and grant_scope.get(key) is True:
            continue
        breached = True

    action = "within_declared_envelope"
    if breached:
        action = "paused_for_reauthorization"
    elif isinstance(grant_scope, dict):
        action = "continued_with_grant"

    checkpoint.update({
        "evaluated": True,
        "actual_minutes": actual_minutes,
        "actual_files_touched": actual_files,
        "side_effects_actual": side_effects,
        "breached": breached,
        "action": action,
    })
    return checkpoint


def _base_result(
    final_state: str,
    completion_kind: str,
    checkpoint: Dict[str, Any],
) -> Dict[str, Any]:
    if final_state not in FINAL_STATES:
        raise ValueError(f"invalid final_state: {final_state}")
    return {
        "final_state": final_state,
        "completion_kind": completion_kind,
        "actual_minutes": checkpoint.get("actual_minutes"),
        "actual_files_touched": checkpoint.get("actual_files_touched"),
        "predicted_risk_materialized": bool(checkpoint.get("breached", False)),
        "threshold_checkpoint": checkpoint,
    }


def evaluate_autonomy(
    message: Dict[str, Any],
    config: Dict[str, Any],
    actuals: Optional[Dict[str, Any]] = None,
    message_path: Optional[Path] = None,
    audit_dir: Optional[Path] = None,
    receiver: str = "codex",
    now_utc: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Evaluate a message/config pair and return a canonical decision dict."""
    msg_hash = message_sha256(message, message_path)
    try:
        mode, policy = receiver_policy(config)
    except AutonomyConfigError:
        checkpoint = evaluate_threshold_checkpoint(None, {}, actuals)
        return {
            "decision": "paused",
            "mode": "always_pause",
            "reason_codes": ["config_malformed"],
            "message_sha256": msg_hash,
            "scope_envelope": None,
            "logged_notes": [],
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "config_malformed", checkpoint),
        }

    if mode == "always_pause":
        checkpoint = evaluate_threshold_checkpoint(None, {}, actuals)
        return {
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["mode_always_pause"],
            "message_sha256": msg_hash,
            "scope_envelope": None,
            "logged_notes": [],
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "always_pause", checkpoint),
        }

    message_errors = validate_message_dict(message)
    if message_errors:
        checkpoint = evaluate_threshold_checkpoint(None, {}, actuals)
        return {
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["message_invalid"],
            "message_sha256": msg_hash,
            "message_validation_errors": message_errors,
            "scope_envelope": None,
            "logged_notes": [],
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "message_invalid", checkpoint),
        }

    if message_expired(message, now_utc):
        checkpoint = evaluate_threshold_checkpoint(None, {}, actuals)
        return {
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["message_expired"],
            "message_sha256": msg_hash,
            "scope_envelope": None,
            "logged_notes": [],
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "message_expired", checkpoint),
        }

    if prior_auto_accept_exists(str(message.get("id") or ""), receiver, audit_dir):
        checkpoint = evaluate_threshold_checkpoint(None, {}, actuals)
        return {
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["message_replayed"],
            "message_sha256": msg_hash,
            "scope_envelope": None,
            "logged_notes": [],
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "message_replayed", checkpoint),
        }

    body = str(message.get("body") or "")
    msg_type = str(message.get("type") or "")
    allow_without_profile = msg_type in policy["allow_without_task_profile"]
    logged_notes: List[Dict[str, str]] = []

    def with_message_hash(decision: Dict[str, Any]) -> Dict[str, Any]:
        decision["message_sha256"] = msg_hash
        return decision

    profile, profile_error = extract_task_profile(body)
    if profile_error:
        checkpoint = evaluate_threshold_checkpoint(None, {}, actuals)
        return with_message_hash({
            "decision": "paused",
            "mode": mode,
            "reason_codes": [profile_error],
            "scope_envelope": None,
            "logged_notes": logged_notes,
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", profile_error, checkpoint),
        })

    if profile is None and not allow_without_profile:
        reason = "risk_obvious_no_profile" if obvious_no_profile_risk(body) else "task_profile_missing"
        checkpoint = evaluate_threshold_checkpoint(None, {}, actuals)
        return with_message_hash({
            "decision": "paused",
            "mode": mode,
            "reason_codes": [reason],
            "scope_envelope": None,
            "logged_notes": logged_notes,
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", reason, checkpoint),
        })

    envelope: Optional[Dict[str, Any]] = None
    profile_required_reason = "task_profile_present"
    if profile is None:
        profile_required_reason = "task_profile_not_required"
        logged_notes.extend(side_effect_notes_for_allowed_type(body))
    else:
        try:
            envelope = normalize_scope_envelope(profile)
        except TaskProfileError:
            checkpoint = evaluate_threshold_checkpoint(None, {}, actuals)
            return with_message_hash({
                "decision": "paused",
                "mode": mode,
                "reason_codes": ["task_profile_unparsable"],
                "scope_envelope": None,
                "logged_notes": logged_notes,
                "continuation_grant": {"present": False, "enabled": False},
                "result": _base_result("paused", "task_profile_unparsable", checkpoint),
            })

    matched = first_match(DESTRUCTIVE_PATTERNS, body)
    if matched:
        checkpoint = evaluate_threshold_checkpoint(envelope, {}, actuals)
        return with_message_hash({
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["hard_stop_destructive_command"],
            "matched_pattern": matched,
            "scope_envelope": envelope,
            "logged_notes": logged_notes,
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "hard_stop", checkpoint),
        })

    side_effect_patterns = NON_DEMOTABLE_SIDE_EFFECT_PATTERNS
    if not allow_without_profile or profile is not None:
        side_effect_patterns = SIDE_EFFECT_VERB_PATTERNS + side_effect_patterns
    matched = first_match(side_effect_patterns, body)
    if matched:
        checkpoint = evaluate_threshold_checkpoint(envelope, {}, actuals)
        return with_message_hash({
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["hard_stop_external_side_effect"],
            "matched_pattern": matched,
            "scope_envelope": envelope,
            "logged_notes": logged_notes,
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "hard_stop", checkpoint),
        })

    matched = first_match(SENSITIVE_SCOPE_PATTERNS, body)
    if matched:
        checkpoint = evaluate_threshold_checkpoint(envelope, {}, actuals)
        return with_message_hash({
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["hard_stop_sensitive_scope"],
            "matched_pattern": matched,
            "scope_envelope": envelope,
            "logged_notes": logged_notes,
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "hard_stop", checkpoint),
        })

    matched = first_match(AMBIGUOUS_SCOPE_PATTERNS, body)
    if matched:
        checkpoint = evaluate_threshold_checkpoint(envelope, {}, actuals)
        return with_message_hash({
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["file_scope_ambiguous"],
            "matched_pattern": matched,
            "scope_envelope": envelope,
            "logged_notes": logged_notes,
            "continuation_grant": {"present": False, "enabled": False},
            "result": _base_result("paused", "ambiguous_scope", checkpoint),
        })

    grant_result: Dict[str, Any] = {"present": False, "enabled": False}
    if envelope is not None:
        grant_result = evaluate_continuation_grant(
            message,
            envelope,
            bool(policy["continuation_grants_enabled"]),
        )
        hard_profile_reasons = []
        if envelope["destructive_ops"]:
            hard_profile_reasons.append("destructive_ops_pause")
        if envelope["touches_auth_config_or_secrets"]:
            hard_profile_reasons.append("auth_config_or_secrets_pause")
        if envelope["touches_dependencies"]:
            hard_profile_reasons.append("dependency_changes_pause")
        if envelope["public_visibility"]:
            hard_profile_reasons.append("public_visibility_pause")
        if hard_profile_reasons:
            checkpoint = evaluate_threshold_checkpoint(envelope, grant_result, actuals)
            return with_message_hash({
                "decision": "paused",
                "mode": mode,
                "reason_codes": hard_profile_reasons,
                "scope_envelope": envelope,
                "logged_notes": logged_notes,
                "continuation_grant": grant_result,
                "result": _base_result("paused", "hard_stop", checkpoint),
            })

        reasons = _threshold_reasons(envelope, policy["thresholds"])
        side_effect_reasons = _side_effect_reasons(envelope, grant_result)
        if grant_result.get("decision") == "ignored_disabled":
            side_effect_reasons = list(grant_result["reason_codes"]) + side_effect_reasons
        reasons.extend(side_effect_reasons)
        if reasons:
            checkpoint = evaluate_threshold_checkpoint(envelope, grant_result, actuals)
            return with_message_hash({
                "decision": "paused",
                "mode": mode,
                "reason_codes": reasons,
                "scope_envelope": envelope,
                "logged_notes": logged_notes,
                "continuation_grant": grant_result,
                "result": _base_result("paused", "auto_review_paused", checkpoint),
            })

    checkpoint = evaluate_threshold_checkpoint(envelope, grant_result, actuals)
    if checkpoint["evaluated"] and checkpoint["breached"]:
        return with_message_hash({
            "decision": "paused",
            "mode": mode,
            "reason_codes": ["threshold_checkpoint_breached"],
            "scope_envelope": envelope,
            "logged_notes": logged_notes,
            "continuation_grant": grant_result,
            "result": _base_result("paused", "threshold_checkpoint_breached", checkpoint),
        })

    reason_codes = [
        "message_valid",
        "message_not_expired",
        "message_hash_recorded",
        profile_required_reason,
        "task_type_allowed",
        "hard_stops_clear",
        "workspace_check_required",
    ]
    if envelope is not None:
        reason_codes.insert(5, "risk_threshold_passed")
    if grant_result.get("decision") == "accepted":
        reason_codes.append("continuation_grant_accepted")

    return with_message_hash({
        "decision": "auto_accepted",
        "mode": mode,
        "reason_codes": reason_codes,
        "scope_envelope": envelope,
        "logged_notes": logged_notes,
        "continuation_grant": grant_result,
        "result": _base_result("done", "auto_accepted", checkpoint),
    })


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--message", required=True, type=Path)
    parser.add_argument("--actuals", type=Path)
    parser.add_argument("--audit-dir", type=Path)
    parser.add_argument("--receiver", default="codex")
    args = parser.parse_args(argv)

    try:
        config = load_yaml_file(args.config)
        message = load_yaml_file(args.message)
        actuals = load_yaml_file(args.actuals) if args.actuals else None
        print(json.dumps(
            evaluate_autonomy(
                message,
                config,
                actuals,
                message_path=args.message,
                audit_dir=args.audit_dir,
                receiver=args.receiver,
            ),
            indent=2,
        ))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
