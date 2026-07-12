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

from _oacp_constants import REPO_SLUG_RE
from validate_message import validate_message_dict


VALID_MODES = {"always_pause", "auto_review"}
POLICY_ACTIONS = {"pause", "allow_pr_artifacts", "allow"}
AUTONOMY_AUDIT_SCHEMA_VERSION = 2
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
COMPLETE_PROFILE_FIELDS = (
    "estimated_minutes",
    "risk_tier",
    "expected_files_touched",
    *LEGACY_PROFILE_BOOL_FIELDS,
)

FINAL_STATES = {"done", "paused", "blocked", "superseded", "error"}
PINNED_REASON_CODES = frozenset({
    "auth_config_or_secrets_pause",
    "comments_on_github_invalid",
    "comments_on_github_pause",
    "commits_changes_invalid",
    "commits_changes_pause",
    "config_malformed",
    "continuation_grant_accepted",
    "continuation_grant_denied",
    "continuation_grant_ignored_disabled",
    "continuation_grant_missing_approval",
    "continuation_grant_missing_scope",
    "continuation_grant_missing_thread",
    "continuation_grant_scope_exceeded",
    "creates_or_updates_pr_invalid",
    "creates_or_updates_pr_pause",
    "declaration_error",
    "dependency_changes_pause",
    "destructive_ops_pause",
    "envelope_compile_error",
    "estimated_minutes_exceeds_threshold",
    "expected_files_touched_exceeds_threshold",
    "external_side_effects_not_pr_artifact",
    "external_side_effects_pause",
    "file_scope_ambiguous",
    "hard_stop_content_sensitivity",
    "hard_stop_destructive_command",
    "hard_stop_external_side_effect",
    "hard_stop_sensitive_scope",
    "hard_stops_clear",
    "lexical_advisory",
    "max_actual_files_touched_invalid",
    "max_actual_minutes_invalid",
    "message_expired",
    "message_hash_recorded",
    "message_invalid",
    "message_not_expired",
    "message_replayed",
    "message_valid",
    "mode_always_pause",
    "public_visibility_pause",
    "risk_obvious_no_profile",
    "risk_threshold_passed",
    "task_profile_missing",
    "task_profile_not_required",
    "task_profile_present",
    "task_profile_unparsable",
    "task_type_allowed",
    "threshold_checkpoint_breached",
    "workspace_check_required",
})

GUARDRAILS_FENCE_RE = re.compile(
    r"(?ms)^[ \t]*```oacp-guardrails[ \t]*\n"
    r"(?P<content>.*?)^[ \t]*```[ \t]*(?:\n|$)"
)
NEGATION_PREFIX_RE = re.compile(
    r"\b(?:no|not|never|do\s+not|does\s+not|don't|doesn't)\b"
    r"[^.!?;\n]{0,160}$",
    re.IGNORECASE,
)
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

DECLARATION_AWARE_SENSITIVE_PATTERNS = (
    (
        "auth",
        re.compile(r"(?<![\w/-])auth(?![\w/-])", re.IGNORECASE),
        "touches_auth_config_or_secrets",
    ),
    (
        "secrets",
        re.compile(r"(?<![\w/-])secrets?(?![\w/-])", re.IGNORECASE),
        "touches_auth_config_or_secrets",
    ),
    (
        "credentials",
        re.compile(r"(?<![\w/-])credentials?(?![\w/-])", re.IGNORECASE),
        "touches_auth_config_or_secrets",
    ),
    (
        "config",
        re.compile(
            r"\bconfig(?:uration)?\s+(?:file|files|setting|settings|template|templates|yaml|yml)\b"
            r"|\b(?:project|workspace|runtime|agent)\s+config(?:uration)?\b"
            r"|\bconfig\.ya?ml\b",
            re.IGNORECASE,
        ),
        "touches_auth_config_or_secrets",
    ),
)

CONTENT_SENSITIVITY_PATTERNS = (
    ("pricing", re.compile(r"(?<![\w/-])pricing(?![\w/-])", re.IGNORECASE)),
    ("commercial", re.compile(r"(?<![\w/-])commercial(?![\w/-])", re.IGNORECASE)),
)

NON_DEMOTABLE_SENSITIVE_PATTERNS = (
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
            value = str(thresholds.get(key) or "")
            allowed = POLICY_ACTIONS if key == "external_side_effects" else {"pause"}
            if value not in allowed:
                choices = ", ".join(sorted(allowed))
                errors.append(
                    f"autonomy.auto_review_thresholds.{key} must be one of: {choices}"
                )

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

    private_repos = autonomy.get("private_repo_allowlist", [])
    if private_repos is None:
        private_repos = []
    if not isinstance(private_repos, list):
        errors.append("autonomy.private_repo_allowlist must be a list")
    else:
        for repo in private_repos:
            if not isinstance(repo, str) or not REPO_SLUG_RE.fullmatch(repo):
                errors.append(
                    "autonomy.private_repo_allowlist entries must use owner/repo"
                )

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
        "private_repo_allowlist": list(autonomy.get("private_repo_allowlist") or []),
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


def _risk_tier_value(profile: Dict[str, Any]) -> str:
    value = profile.get("risk_tier")
    if isinstance(value, str) and value in {"P0", "P1", "P2", "P3"}:
        return value
    raise TaskProfileError("task_profile.risk_tier must be P0, P1, P2, or P3")


def _target_repo_value(profile: Dict[str, Any]) -> str:
    value = profile.get("target_repo", "")
    if value in (None, ""):
        return ""
    if isinstance(value, str) and REPO_SLUG_RE.fullmatch(value):
        return value
    raise TaskProfileError("task_profile.target_repo must use owner/repo")


def normalize_scope_envelope(profile: Dict[str, Any]) -> Dict[str, Any]:
    envelope: Dict[str, Any] = {
        "estimated_minutes": _int_value(profile, "estimated_minutes"),
        "expected_files_touched": _int_value(profile, "expected_files_touched"),
        "risk_tier": _risk_tier_value(profile),
        "target_repo": _target_repo_value(profile),
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


def canonical_policy_sha256(config: Dict[str, Any]) -> str:
    """Hash parsed policy data so comments and formatting do not create drift."""
    serialized = json.dumps(
        config,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _profile_is_complete(profile: Optional[Dict[str, Any]]) -> bool:
    return isinstance(profile, dict) and all(key in profile for key in COMPLETE_PROFILE_FIELDS)


def _record_lexical_note(
    notes: List[Dict[str, str]],
    code: str,
    label: str,
) -> None:
    note = {"code": code, "matched_pattern": label}
    if note not in notes:
        notes.append(note)


def _match_is_negated(body: str, match: re.Match[str]) -> bool:
    prefix = body[:match.start()]
    boundary = max(prefix.rfind(mark) for mark in ("\n", ".", "!", "?", ";", "—", "–"))
    clause_prefix = prefix[boundary + 1:]
    return NEGATION_PREFIX_RE.search(clause_prefix) is not None


def _gate3_body(body: str, notes: List[Dict[str, str]]) -> str:
    fence_matches = list(GUARDRAILS_FENCE_RE.finditer(body))
    advisory_patterns = (
        SIDE_EFFECT_VERB_PATTERNS
        + tuple(
            (label, pattern)
            for label, pattern, _profile_field in DECLARATION_AWARE_SENSITIVE_PATTERNS
        )
        + AMBIGUOUS_SCOPE_PATTERNS
    )
    for fence_match in fence_matches:
        _record_lexical_note(notes, "guardrails_section_skipped", "oacp-guardrails")
        content = fence_match.group("content")
        for label, pattern in advisory_patterns:
            if pattern.search(content):
                _record_lexical_note(notes, "lexical_advisory_guardrails", label)
    return GUARDRAILS_FENCE_RE.sub("\n", body)


def _first_effective_match(
    patterns: Sequence[Tuple[str, re.Pattern[str]]],
    body: str,
    notes: List[Dict[str, str]],
    *,
    demote_declared: bool = False,
) -> Optional[str]:
    for label, pattern in patterns:
        for match in pattern.finditer(body):
            if _match_is_negated(body, match):
                _record_lexical_note(notes, "lexical_advisory_negated", label)
                continue
            if demote_declared:
                _record_lexical_note(notes, "lexical_advisory_declared", label)
                continue
            return label
    return None


def _first_sensitive_match(
    body: str,
    notes: List[Dict[str, str]],
    profile: Optional[Dict[str, Any]],
    envelope: Optional[Dict[str, Any]],
) -> Optional[str]:
    profile_complete = _profile_is_complete(profile)
    for label, pattern, profile_field in DECLARATION_AWARE_SENSITIVE_PATTERNS:
        for match in pattern.finditer(body):
            if _match_is_negated(body, match):
                _record_lexical_note(notes, "lexical_advisory_negated", label)
                continue
            if profile_complete and envelope is not None and not envelope[profile_field]:
                _record_lexical_note(notes, "lexical_advisory_declared", label)
                continue
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


def normalize_continuation_scope(
    scope: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
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


def _grant_scope(
    grant: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    return normalize_continuation_scope(grant.get("scope"))


def _audit_thread_matches(message: Dict[str, Any], audit: Dict[str, Any]) -> bool:
    sender = str(message.get("from") or "").strip()
    audit_sender = str(audit.get("sender") or "").strip()
    if not sender or audit_sender != sender:
        return False

    conversation_id = str(message.get("conversation_id") or "").strip()
    parent_message_id = str(message.get("parent_message_id") or "").strip()
    audit_conversation_id = str(audit.get("conversation_id") or "").strip()

    if conversation_id and audit_conversation_id == conversation_id:
        return True
    return bool(parent_message_id and audit.get("message_id") == parent_message_id)


def _prior_thread_grant(
    message: Dict[str, Any],
    audit_dir: Optional[Path],
    receiver: str,
) -> Optional[Dict[str, Any]]:
    if audit_dir is None or not audit_dir.is_dir():
        return None

    candidates: List[Tuple[str, str, Dict[str, Any]]] = []
    current_message_id = str(message.get("id") or "")
    message_created_at = dt.datetime.strptime(
        str(message.get("created_at_utc") or ""),
        "%Y-%m-%dT%H:%M:%SZ",
    )
    for audit_path in audit_dir.glob("*.yaml"):
        try:
            audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(audit, dict):
            continue
        if audit.get("schema_version") != AUTONOMY_AUDIT_SCHEMA_VERSION:
            continue
        if audit.get("receiver") != receiver:
            continue
        if audit.get("decision") != "paused":
            continue
        if audit.get("message_id") == current_message_id:
            continue
        if not _audit_thread_matches(message, audit):
            continue

        result = audit.get("result")
        outcome = result.get("human_outcome") if isinstance(result, dict) else None
        grant = outcome.get("grant") if isinstance(outcome, dict) else None
        if not isinstance(outcome, dict) or outcome.get("recorded") is not True:
            continue
        if not isinstance(grant, dict):
            continue
        grant_decision = str(grant.get("decision") or "")
        if grant_decision not in {"approved", "modified", "denied"}:
            continue
        decided_at = str(outcome.get("decided_at_utc") or "")
        try:
            decision_time = dt.datetime.strptime(decided_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        if decision_time > message_created_at:
            continue
        candidates.append((decided_at, audit_path.name, audit))

    if not candidates:
        return None

    _decided_at, audit_name, audit = sorted(candidates)[-1]
    outcome = audit["result"]["human_outcome"]
    grant = outcome["grant"]
    grant_decision = str(grant["decision"])
    source = {
        "source_audit": audit_name,
        "source_message_id": audit.get("message_id"),
        "human_decision": outcome.get("decision"),
        "grant_decision": grant_decision,
    }
    if grant_decision == "denied":
        return {
            **source,
            "decision": "denied",
            "reason_codes": ["continuation_grant_denied"],
            "scope": None,
        }

    if outcome.get("decision") not in {"approved", "modified"}:
        return {
            **source,
            "decision": "denied",
            "reason_codes": ["continuation_grant_denied"],
            "scope": None,
        }

    scope, error = normalize_continuation_scope(grant.get("granted_scope"))
    if error:
        return {
            **source,
            "decision": "invalid",
            "reason_codes": [error],
            "scope": None,
        }
    return {
        **source,
        "decision": "accepted",
        "reason_codes": ["continuation_grant_accepted"],
        "scope": scope,
    }


def evaluate_continuation_grant(
    message: Dict[str, Any],
    envelope: Dict[str, Any],
    continuation_enabled: bool,
    audit_dir: Optional[Path] = None,
    receiver: str = "codex",
) -> Dict[str, Any]:
    grants = envelope.get("continuation_grants") or {}
    grant = grants.get("approved_thread_continuation")
    request_present = isinstance(grant, dict)
    result: Dict[str, Any] = {
        "present": request_present,
        "request_present": request_present,
        "standing_grant_found": False,
        "enabled": continuation_enabled,
        "kind": "approved_thread_continuation",
        "decision": "not_present",
        "reason_codes": [],
        "requested_scope": None,
        "scope": None,
        "source_audit": None,
        "source_message_id": None,
    }

    if not continuation_enabled:
        if request_present:
            result["decision"] = "ignored_disabled"
            result["reason_codes"] = ["continuation_grant_ignored_disabled"]
        return result

    has_thread = bool(message.get("parent_message_id") or message.get("conversation_id"))
    if not has_thread:
        if request_present:
            result["decision"] = "invalid"
            result["reason_codes"] = ["continuation_grant_missing_thread"]
        return result

    if request_present:
        requested_scope, error = _grant_scope(grant)
        if error:
            result["decision"] = "invalid"
            result["reason_codes"] = [error]
            return result
        result["requested_scope"] = requested_scope

    prior = _prior_thread_grant(message, audit_dir, receiver)
    if prior is not None:
        result.update(prior)
        result["present"] = True
        result["standing_grant_found"] = True
        return result

    if request_present:
        result["decision"] = "missing_approval"
        result["reason_codes"] = ["continuation_grant_missing_approval"]
    return result


def continuation_scope_breaches(
    envelope: Dict[str, Any],
    grant_result: Dict[str, Any],
) -> List[str]:
    scope = grant_result.get("scope")
    if grant_result.get("decision") != "accepted" or not isinstance(scope, dict):
        return []

    breached: List[str] = []
    if envelope["estimated_minutes"] > scope["max_actual_minutes"]:
        breached.append("task_profile.estimated_minutes")
    if envelope["expected_files_touched"] > scope["max_actual_files_touched"]:
        breached.append("task_profile.expected_files_touched")

    declared_effects = [
        key for key in COVERABLE_CONTINUATION_FIELDS if envelope[key]
    ]
    if envelope["external_side_effects"] and not declared_effects:
        breached.append("task_profile.external_side_effects")
    for key in declared_effects:
        if scope.get(key) is not True:
            breached.append(f"task_profile.{key}")
    return breached


def _side_effect_reasons(
    envelope: Dict[str, Any],
    grant_result: Dict[str, Any],
    external_policy: str,
    private_repo_allowlist: Sequence[str],
) -> List[str]:
    reasons: List[str] = []
    grant_scope = grant_result.get("scope") if grant_result.get("decision") == "accepted" else None
    grant_covers_side_effect = isinstance(grant_scope, dict) and any(
        grant_scope.get(key) is True for key in COVERABLE_CONTINUATION_FIELDS
    )
    uncovered_fields = [
        key
        for key in COVERABLE_CONTINUATION_FIELDS
        if envelope[key]
        and not (isinstance(grant_scope, dict) and grant_scope.get(key) is True)
    ]

    if external_policy == "allow":
        return reasons
    if external_policy == "allow_pr_artifacts":
        is_private_pr_artifact = (
            envelope["external_side_effects"]
            and not envelope["public_visibility"]
            and envelope["target_repo"] in private_repo_allowlist
            and (envelope["creates_or_updates_pr"] or envelope["comments_on_github"])
        )
        if is_private_pr_artifact:
            return reasons
        if (
            (envelope["external_side_effects"] and not grant_covers_side_effect)
            or uncovered_fields
        ):
            return ["external_side_effects_not_pr_artifact"]
        return reasons

    if envelope["external_side_effects"] and not grant_covers_side_effect:
        reasons.append("external_side_effects_pause")
    for key in uncovered_fields:
        reasons.append(f"{key}_pause")
    return reasons


def _profile_declaration_errors(envelope: Dict[str, Any]) -> List[str]:
    breaches: List[str] = []
    artifact_fields = [key for key in COVERABLE_CONTINUATION_FIELDS if envelope[key]]
    if artifact_fields and not envelope["external_side_effects"]:
        breaches.append("task_profile.external_side_effects")
    if envelope["sends_oacp_reply_only"] and artifact_fields:
        breaches.append("task_profile.sends_oacp_reply_only")
    return breaches


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
        raise ValueError("actuals.side_effects_actual must be a mapping")
    normalized: Dict[str, bool] = {}
    for key in COVERABLE_CONTINUATION_FIELDS:
        value = side_effects.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"actuals.side_effects_actual.{key} must be boolean")
        normalized[key] = value
    return normalized


def _actual_nonnegative_int(actuals: Dict[str, Any], key: str) -> int:
    value = actuals.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"actuals.{key} must be a non-negative integer")
    return value


def _completed_at_utc(actuals: Dict[str, Any]) -> Optional[str]:
    value = str(actuals.get("completed_at_utc") or "")
    if not value:
        return None
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(
            "actuals.completed_at_utc must use YYYY-MM-DDTHH:MM:SSZ"
        ) from exc
    return value


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
        "breached_fields": [],
        "declaration_errors": [],
        "action": "not_evaluated",
        "predicted_risk_materialized": False,
        "completed_at_utc": None,
    }
    if not actuals or not envelope:
        return checkpoint

    actual_minutes = _actual_nonnegative_int(actuals, "actual_minutes")
    actual_files = _actual_nonnegative_int(actuals, "actual_files_touched")
    side_effects = _actual_side_effects(actuals)
    grant_scope = grant_result.get("scope") if grant_result.get("decision") == "accepted" else None

    max_minutes = envelope["estimated_minutes"]
    max_files = envelope["expected_files_touched"]
    if isinstance(grant_scope, dict):
        max_minutes = grant_scope["max_actual_minutes"]
        max_files = grant_scope["max_actual_files_touched"]

    breached_fields: List[str] = []
    declaration_errors: List[str] = []
    if actual_minutes > max_minutes:
        breached_fields.append("actual_minutes")
    if actual_files > max_files:
        breached_fields.append("actual_files_touched")
    for key, actual in side_effects.items():
        if not actual:
            continue
        if envelope.get(key) is True:
            continue
        if isinstance(grant_scope, dict) and grant_scope.get(key) is True:
            continue
        field = f"side_effects_actual.{key}"
        breached_fields.append(field)
        declaration_errors.append(field)

    breached = bool(breached_fields)

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
        "breached_fields": breached_fields,
        "declaration_errors": declaration_errors,
        "action": action,
        "predicted_risk_materialized": (
            actuals["predicted_risk_materialized"]
            if isinstance(actuals.get("predicted_risk_materialized"), bool)
            else breached
        ),
        "completed_at_utc": _completed_at_utc(actuals),
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
        "predicted_risk_materialized": bool(
            checkpoint.get("predicted_risk_materialized", False)
        ),
        "completed_at_utc": checkpoint.get("completed_at_utc"),
        # Runtime adapters (envelope hooks) upgrade this to "hooks" after a
        # successful `oacp envelope compile`; "none" means pickup-gate-only
        # enforcement. Degradation must never be silent.
        "envelope_enforcement": "none",
        "threshold_checkpoint": checkpoint,
        "human_outcome": {
            "recorded": False,
            "actor": None,
            "decision": None,
            "decided_at_utc": None,
            "decision_latency_seconds": None,
            "pause_reason_codes": [],
            "grant": {
                "decision": "not_recorded",
                "request_present": False,
                "request_error": None,
                "requested_scope": None,
                "granted_scope": None,
            },
        },
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
    policy_hash = canonical_policy_sha256(config)
    logged_notes: List[Dict[str, str]] = []
    profile_snapshot: Optional[Dict[str, Any]] = None

    def finish(decision: Dict[str, Any]) -> Dict[str, Any]:
        reason_codes = list(decision.get("reason_codes") or [])
        unknown_codes = sorted(set(reason_codes) - PINNED_REASON_CODES)
        if unknown_codes:
            raise ValueError(f"unpinned autonomy reason code(s): {', '.join(unknown_codes)}")
        decision["message_sha256"] = msg_hash
        decision["policy_sha256"] = policy_hash
        decision["schema_version"] = AUTONOMY_AUDIT_SCHEMA_VERSION
        decision["spec_version"] = "0.3.5"
        decision["receiver"] = receiver
        decision["sender"] = message.get("from")
        decision["message_id"] = message.get("id")
        decision["message_type"] = message.get("type")
        decision["conversation_id"] = message.get("conversation_id")
        decision["parent_message_id"] = message.get("parent_message_id")
        decision.setdefault("task_profile", profile_snapshot)
        decision.setdefault(
            "breached",
            reason_codes if decision.get("decision") == "paused" else [],
        )
        return decision

    def paused(
        mode: str,
        reason_codes: List[str],
        completion_kind: str,
        *,
        envelope: Optional[Dict[str, Any]] = None,
        grant_result: Optional[Dict[str, Any]] = None,
        matched_pattern: Optional[str] = None,
        validation_errors: Optional[List[str]] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        breached: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        grant = grant_result or {"present": False, "enabled": False}
        resolved_checkpoint = checkpoint or evaluate_threshold_checkpoint(
            envelope,
            grant,
            actuals,
        )
        decision: Dict[str, Any] = {
            "decision": "paused",
            "mode": mode,
            "reason_codes": reason_codes,
            "scope_envelope": envelope,
            "logged_notes": logged_notes,
            "continuation_grant": grant,
            "result": _base_result("paused", completion_kind, resolved_checkpoint),
        }
        if matched_pattern is not None:
            decision["matched_pattern"] = matched_pattern
        if validation_errors is not None:
            decision["message_validation_errors"] = validation_errors
        if breached is not None:
            decision["breached"] = breached
        return finish(decision)

    try:
        mode, policy = receiver_policy(config)
    except AutonomyConfigError:
        return paused("always_pause", ["config_malformed"], "config_malformed")

    if mode == "always_pause":
        return paused(mode, ["mode_always_pause"], "always_pause")

    message_errors = validate_message_dict(message)
    if message_errors:
        return paused(
            mode,
            ["message_invalid"],
            "message_invalid",
            validation_errors=message_errors,
        )

    if message_expired(message, now_utc):
        return paused(mode, ["message_expired"], "message_expired")

    if prior_auto_accept_exists(str(message.get("id") or ""), receiver, audit_dir):
        return paused(mode, ["message_replayed"], "message_replayed")

    body = str(message.get("body") or "")
    msg_type = str(message.get("type") or "")
    allow_without_profile = msg_type in policy["allow_without_task_profile"]

    profile, profile_error = extract_task_profile(body)
    profile_snapshot = profile
    if profile_error:
        return paused(mode, [profile_error], profile_error)

    if profile is None and not allow_without_profile:
        reason = "risk_obvious_no_profile" if obvious_no_profile_risk(body) else "task_profile_missing"
        return paused(mode, [reason], reason)

    envelope: Optional[Dict[str, Any]] = None
    profile_required_reason = "task_profile_present"
    if profile is None:
        profile_required_reason = "task_profile_not_required"
        logged_notes.extend(side_effect_notes_for_allowed_type(body))
    else:
        try:
            envelope = normalize_scope_envelope(profile)
        except TaskProfileError:
            return paused(
                mode,
                ["task_profile_unparsable"],
                "task_profile_unparsable",
            )

    gate3_body = _gate3_body(body, logged_notes)

    matched = first_match(DESTRUCTIVE_PATTERNS, body)
    if matched:
        return paused(
            mode,
            ["hard_stop_destructive_command"],
            "hard_stop",
            envelope=envelope,
            matched_pattern=matched,
        )

    external_policy = str(policy["thresholds"]["external_side_effects"])
    if profile is not None:
        demote_side_effect = (
            _profile_is_complete(profile)
            and envelope is not None
            and not envelope["external_side_effects"]
        ) or (
            external_policy == "allow"
            and envelope is not None
            and envelope["external_side_effects"]
        )
        matched = _first_effective_match(
            SIDE_EFFECT_VERB_PATTERNS,
            gate3_body,
            logged_notes,
            demote_declared=demote_side_effect,
        )
        if matched:
            return paused(
                mode,
                ["hard_stop_external_side_effect"],
                "hard_stop",
                envelope=envelope,
                matched_pattern=matched,
            )

    git_push_or_deploy_policy = str(policy["thresholds"]["git_push_or_deploy"])
    matched = None
    if git_push_or_deploy_policy == "pause":
        matched = first_match(NON_DEMOTABLE_SIDE_EFFECT_PATTERNS, body)
    if matched:
        return paused(
            mode,
            ["hard_stop_external_side_effect"],
            "hard_stop",
            envelope=envelope,
            matched_pattern=matched,
        )

    matched = _first_sensitive_match(gate3_body, logged_notes, profile, envelope)
    if matched:
        return paused(
            mode,
            ["hard_stop_sensitive_scope"],
            "hard_stop",
            envelope=envelope,
            matched_pattern=matched,
        )

    matched = first_match(CONTENT_SENSITIVITY_PATTERNS, body)
    if matched:
        return paused(
            mode,
            ["hard_stop_content_sensitivity"],
            "content_sensitivity",
            envelope=envelope,
            matched_pattern=matched,
        )

    matched = first_match(NON_DEMOTABLE_SENSITIVE_PATTERNS, body)
    if matched:
        return paused(
            mode,
            ["hard_stop_sensitive_scope"],
            "hard_stop",
            envelope=envelope,
            matched_pattern=matched,
        )

    matched = _first_effective_match(
        AMBIGUOUS_SCOPE_PATTERNS,
        gate3_body,
        logged_notes,
    )
    if matched:
        return paused(
            mode,
            ["file_scope_ambiguous"],
            "ambiguous_scope",
            envelope=envelope,
            matched_pattern=matched,
        )

    grant_result: Dict[str, Any] = {"present": False, "enabled": False}
    if envelope is not None:
        grant_result = evaluate_continuation_grant(
            message,
            envelope,
            bool(policy["continuation_grants_enabled"]),
            audit_dir=audit_dir,
            receiver=receiver,
        )
        declaration_breaches = _profile_declaration_errors(envelope)
        if declaration_breaches:
            return paused(
                mode,
                ["declaration_error"],
                "declaration_error",
                envelope=envelope,
                grant_result=grant_result,
                breached=declaration_breaches,
            )

        grant_breaches = continuation_scope_breaches(envelope, grant_result)
        if grant_breaches:
            return paused(
                mode,
                ["continuation_grant_scope_exceeded"],
                "continuation_grant_scope_exceeded",
                envelope=envelope,
                grant_result=grant_result,
                breached=grant_breaches,
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
            return paused(
                mode,
                hard_profile_reasons,
                "hard_stop",
                envelope=envelope,
                grant_result=grant_result,
            )

        reasons = []
        if grant_result.get("decision") not in {"accepted", "not_present"}:
            reasons.extend(grant_result.get("reason_codes") or [])
        reasons.extend(_threshold_reasons(envelope, policy["thresholds"]))
        side_effect_reasons = _side_effect_reasons(
            envelope,
            grant_result,
            external_policy,
            policy["private_repo_allowlist"],
        )
        reasons.extend(side_effect_reasons)
        if reasons:
            return paused(
                mode,
                reasons,
                "auto_review_paused",
                envelope=envelope,
                grant_result=grant_result,
            )

    checkpoint = evaluate_threshold_checkpoint(envelope, grant_result, actuals)
    if checkpoint["evaluated"] and checkpoint["breached"]:
        has_declaration_error = bool(checkpoint["declaration_errors"])
        reason = "declaration_error" if has_declaration_error else "threshold_checkpoint_breached"
        completion = "declaration_error" if has_declaration_error else reason
        return paused(
            mode,
            [reason],
            completion,
            envelope=envelope,
            grant_result=grant_result,
            checkpoint=checkpoint,
            breached=list(checkpoint["breached_fields"]),
        )

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
    if any(
        note.get("code", "").startswith("lexical_advisory")
        or note.get("code") == "guardrails_section_skipped"
        or note.get("code") == "side_effect_verb_demoted_for_profileless_type"
        for note in logged_notes
    ):
        reason_codes.insert(-1, "lexical_advisory")
    if grant_result.get("decision") == "accepted":
        reason_codes.append("continuation_grant_accepted")

    return finish({
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
