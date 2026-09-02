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
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

import yaml

from _oacp_constants import (
    REPO_SLUG_RE,
    SPEC_VERSION,
    atomic_replace_bytes,
    atomic_replace_yaml,
    locked_audit,
    utc_now_iso,
)
from validate_message import validate_message_dict


VALID_MODES = {"always_pause", "auto_review"}
POLICY_ACTIONS = {"pause", "allow_pr_artifacts", "allow"}
AUTONOMY_AUDIT_SCHEMA_VERSION = 2
# The serving-model signal a session exports for audit instrumentation. This
# names the model actually serving the invoking session — never the model a
# configuration *requested*, which can be silently served by a different
# model, alias, or context variant.
RUNTIME_MODEL_ENV_VAR = "OACP_RUNTIME_MODEL"
# `model[context]` suffix form (e.g. `claude-sonnet-5[1m]`): same weights,
# different serving context window. Split at write time into the base id plus
# a separate `model_context` field so per-model grouping never divides one
# model across suffix variants.
_MODEL_CONTEXT_SUFFIX_RE = re.compile(r"^(?P<base>[^\[\]]+)\[(?P<context>[^\[\]]+)\]$")
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
    "merges_pr",
    "files_issues",
    "sends_oacp_reply_only",
)
# Declarable granular side-effect capabilities. This tuple is the single
# vocabulary for three surfaces at once — sender declarations, continuation
# grant scopes, and the checkpoint's `side_effects_actual` keys — so the
# declarable set and the observable set can never diverge.
COVERABLE_CONTINUATION_FIELDS = (
    "creates_or_updates_pr",
    "comments_on_github",
    "commits_changes",
    "merges_pr",
    "files_issues",
)
COMPLETE_PROFILE_FIELDS = (
    "estimated_minutes",
    "risk_tier",
    "expected_files_touched",
    *LEGACY_PROFILE_BOOL_FIELDS,
)
# The structured admission ledger: every envelope-derived admission axis,
# evaluated in full before any early return, in evaluation order. A pause
# taken for one reason never leaves another axis unrecorded — record
# silence means "passed", never "not evaluated". Lexical (Gate-3)
# classification is body-derived and records every match in
# `matched_patterns`; the legacy singular `matched_pattern` still names the
# first blocking match. Lexical evidence is not a ledger axis.
ADMISSION_AXES = (
    "thresholds",
    "declared_risk",
    "declaration",
    "side_effects",
    "continuation_grant",
)
# Declared-risk profile flags and the per-knob admission pause each one
# names, in the evaluation order the reason codes are recorded in.
DECLARED_RISK_REASONS = (
    ("destructive_ops", "destructive_ops_pause"),
    ("touches_auth_config_or_secrets", "auth_config_or_secrets_pause"),
    ("touches_dependencies", "dependency_changes_pause"),
    ("public_visibility", "public_visibility_pause"),
)

FINAL_STATES = {"done", "paused", "blocked", "superseded", "error"}
# `result.completion_kind` names the terminal shape of the EVALUATION only —
# one axis, enumerated. The pause cause lives in `reason_codes` (already
# pinned), the run state in `result.final_state`, and human decisions in
# `result.human_outcome`. Receiver-composed values outside this enum are
# non-conforming; receivers must copy the evaluator's kind verbatim and
# never overwrite it at terminal update time.
PINNED_COMPLETION_KINDS = frozenset({
    "auto_accepted",
    "admission_paused",
    "checkpoint_paused",
    "config_malformed",
})
# Checkpoint breach basis: `realized` when the actuals record work that
# already happened; `declared_intent` when the checkpoint fired
# prospectively — the undeclared action was caught before it materialized
# (§E: mandatory before performing ANY newly discovered outward action).
BREACH_BASES = ("declared_intent", "realized")
# Checkpoint breach sub-basis: refines a `realized` breach on the time axis
# only. `waiting_on_peer` marks a checkpoint whose clock overran while the
# receiver was waiting on a peer reply (a review round, a re-authorization
# answer) rather than working, so the ledger can separate review latency
# from working time when calibrating the time axis.
BREACH_SUB_BASES = ("waiting_on_peer",)
# Checkpoint re-authorization channels, highest precedence first. Precedence
# is by channel rank, never arrival order. GH comments are consultable but
# never authoritative: they sit outside the protocol's identity and
# verification boundary, so any decision they carry is recorded as advisory.
REAUTH_CHANNEL_PRECEDENCE = ("receiver_human", "sender_reply", "gh_comment")
REAUTH_GOVERNING_CHANNELS = ("receiver_human", "sender_reply")
REAUTH_DECISIONS = frozenset({"approved", "modified", "declined"})
# Merge authority is never sender-grantable at a checkpoint, regardless of
# the receiver's external-side-effect policy: it always passes the
# receiver-side human (mirrors merges_pr_pause at admission). Every other
# boundary action a sender may grant is derived from the receiver's own
# admission predicate, never from a standalone vocabulary.
SENDER_UNGRANTABLE_REAUTH_FIELDS = frozenset({"merges_pr"})
# Review-loop lifecycle traffic. Reviewer-output types (`review_feedback`,
# `review_lgtm`) never start reviewer work — they are context-only at the
# receiver. Continuation grants can authorize only the trigger types, and
# `review_addressed` only when a grant lists it explicitly (the
# manual-continuation shape); by default it folds into a newer
# `review_request` as context.
REVIEW_LIFECYCLE_TYPES = (
    "review_request",
    "review_feedback",
    "review_addressed",
    "review_lgtm",
)
REVIEW_CONTINUATION_GRANTABLE_TYPES = frozenset({
    "review_request",
    "review_addressed",
})
# Side effects a granted review invocation may produce. This is the review
# surface's own vocabulary — a review round never commits, merges, or files
# issues, so the task-side COVERABLE_CONTINUATION_FIELDS do not apply here.
REVIEW_SIDE_EFFECT_FIELDS = (
    "writes_findings_packet",
    "sends_oacp_reply",
    "comments_on_github",
    "submits_github_review",
)
# The review side effects a round performs when the request declares none:
# every reviewer invocation writes a findings packet and answers on the
# OACP channel. Anything beyond that must be requested and granted.
DEFAULT_REVIEW_SIDE_EFFECTS = ("writes_findings_packet", "sends_oacp_reply")
PINNED_REASON_CODES = frozenset({
    "auth_config_or_secrets_pause",
    "checkpoint_reauthorization_stale",
    "checkpoint_reauthorized",
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
    "files_issues_invalid",
    "files_issues_pause",
    "hard_stop_content_sensitivity",
    "hard_stop_destructive_command",
    "hard_stop_external_side_effect",
    "hard_stop_sensitive_scope",
    "hard_stops_clear",
    "lexical_advisory",
    "max_actual_files_touched_invalid",
    "max_actual_minutes_invalid",
    "merges_pr_invalid",
    "merges_pr_pause",
    "message_expired",
    "message_hash_recorded",
    "message_invalid",
    "message_not_expired",
    "message_replayed",
    "message_valid",
    "mode_always_pause",
    "policy_auth_invalid",
    "public_visibility_pause",
    "review_continuation_accepted",
    "review_continuation_confirmation_required",
    "review_continuation_context_only",
    "review_continuation_expired",
    "review_continuation_head_mismatch",
    "review_continuation_ignored_disabled",
    "review_continuation_missing_approval",
    "review_continuation_revoked",
    "review_continuation_round_exceeded",
    "review_continuation_scope_exceeded",
    "review_loop_invalid",
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
NEGATION_TERM_PATTERN = (
    r"no|not|never|do\s+not|does\s+not|don't|doesn't|"
    r"out\s+of\s+scope|exclude(?:s|d)?|avoid|refrain\s+from|"
    r"prohibited|forbidden|skip|without"
)
NEGATION_TERM_RE = re.compile(
    rf"\b(?:{NEGATION_TERM_PATTERN})\b",
    re.IGNORECASE,
)
NEGATION_PREFIX_RE = re.compile(
    rf"\b(?:{NEGATION_TERM_PATTERN})\b[^.!?;\n]{{0,160}}$",
    re.IGNORECASE,
)
BLOCK_NEGATION_PREFIX_RE = re.compile(
    r"\b(?:"
    r"no|not|never|do\s+not|does\s+not|don't|doesn't|"
    r"out\s+of\s+scope|exclude(?:s|d)?|avoid|refrain\s+from|"
    r"prohibited|forbidden"
    r")\b"
    r"[^.!?;\n]{0,160}$",
    re.IGNORECASE,
)
ATX_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")
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

INSTALL_VERB_PATTERN = r"\binstall(?:s|ing)?\b"
INSTALL_VERB_RE = re.compile(INSTALL_VERB_PATTERN, re.IGNORECASE)
INSTALL_DEPENDENCY_RE = re.compile(
    rf"{INSTALL_VERB_PATTERN}"
    rf"(?:(?!{INSTALL_VERB_PATTERN})[^.!?;\n—–])*?"
    r"\bdependenc(?:y|ies)\b",
    re.IGNORECASE,
)
# Fail-closed positive governance forms for the proven false positives. These
# patterns prove that a negation governs the target occurrence; they are not an
# enumeration of words that might terminate some unrelated negation.
INSTALL_NEGATION_GOVERNANCE_RE = re.compile(
    rf"\b(?:{NEGATION_TERM_PATTERN})\b"
    r"(?:\s*:\s*|\s+(?:run\s+package\s+)?)$",
    re.IGNORECASE,
)

NON_DEMOTABLE_SIDE_EFFECT_PATTERNS = (
    ("push to main", re.compile(r"\bpush(?:es|ing)?\s+to\s+main\b", re.IGNORECASE)),
    (
        "rotate credentials",
        re.compile(r"\brotat(?:e|es|ing)\s+credentials?\b", re.IGNORECASE),
    ),
    (
        "install dependency",
        INSTALL_DEPENDENCY_RE,
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

PUBLIC_REPO_RE = re.compile(
    r"\bpublic\s+repositor(?:y|ies)|\bpublic\s+repo\b",
    re.IGNORECASE,
)
# Public-repository demotion uses the same positive-governance rule: only the
# narrow direct and out-of-scope forms supported by field evidence are advisory.
PUBLIC_REPO_NEGATION_GOVERNANCE_RE = re.compile(
    rf"\b(?:{NEGATION_TERM_PATTERN})\b"
    r"(?:"
    r"\s*:\s*(?:anything\s+(?:on|in)\s+)?(?:the\s+)?"
    r"|\s+(?:(?:any|all)\s+)?(?:the\s+)?"
    r"|\s+publish(?:ing)?(?:\s+(?:the\s+)?"
    r"(?:docs?|documentation|content|changes?|files?))?"
    r"\s+to\s+(?:the\s+)?"
    r"|\s+(?:touch(?:ing)?|edit(?:ing)?|updat(?:e|ing)|"
    r"chang(?:e|ing)|modify(?:ing)?|us(?:e|ing))\s+(?:the\s+)?"
    r")$",
    re.IGNORECASE,
)

NON_DEMOTABLE_SENSITIVE_PATTERNS = (
    (
        "public repo",
        PUBLIC_REPO_RE,
    ),
    (
        "memory SSOT",
        re.compile(r"\bmemory\s+ssot\b|\bmemory\s+single\s+source\b", re.IGNORECASE),
    ),
)

AMBIGUOUS_SCOPE_PATTERNS = (
    ("all files", re.compile(r"\ball\s+files\b", re.IGNORECASE)),
)

PROFILELESS_ONLY_RISK_PATTERNS = (
    ("pull request", re.compile(r"\bpull\s+request\b|\bPR\b")),
    ("github", re.compile(r"\bgithub\b", re.IGNORECASE)),
    ("commit", re.compile(r"\bcommit(?:s|ted|ting)?\b", re.IGNORECASE)),
)

LEXICAL_DEMOTION_BASES = frozenset({
    "affirmative",
    "guardrails_fence",
    "negated",
    "non_demotable",
    "policy_allowed",
    "profile_false",
    "profile_true",
    "profileless_risk",
    "profileless_type",
    "reference_only",
    "reply_only_advisory",
})


class AutonomyConfigError(ValueError):
    """Raised when receiver autonomy config is malformed."""


class TaskProfileError(ValueError):
    """Raised when a task_profile block exists but cannot be normalized."""


def load_yaml_file(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


class DuplicateKeyError(ValueError):
    """A YAML mapping key appeared twice in one document."""


class _StrictYamlLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    Plain PyYAML silently keeps the later value, so a duplicated
    ``logged_notes:`` key can shadow the populated one with an empty list
    and the loss is invisible to every reader. Any writer that re-serializes
    a record MUST load it through this loader first — rewriting a
    plain-loaded mapping collapses the duplicate-key evidence for good.
    """


def _strict_construct_mapping(
    loader: _StrictYamlLoader, node: Any, deep: bool = False
) -> Dict[Any, Any]:
    mapping: Dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(
                f"duplicate mapping key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_construct_mapping
)


def load_yaml_strict(path: Path) -> Dict[str, Any]:
    """Load a YAML mapping, refusing duplicate keys and non-mappings."""
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictYamlLoader)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _parse_yaml_mapping(raw: bytes, path: Path) -> Dict[str, Any]:
    """Parse a mapping from an already-read snapshot (never re-reads *path*)."""
    data = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def normalize_runtime_model(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(normalized model id, context marker)`` for a raw model value.

    Write-time normalization covers the drift classes that corrupt per-model
    grouping: case variants fold to lowercase, and a ``[context]`` suffix
    splits into the base id plus a separate context marker. Empty or
    whitespace-only input normalizes to ``(None, None)``.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return None, None
    context: Optional[str] = None
    match = _MODEL_CONTEXT_SUFFIX_RE.fullmatch(text)
    if match:
        text = match.group("base").strip()
        context = match.group("context").strip().lower() or None
    return text.lower() or None, context


def resolve_runtime_block(
    supplied: Any,
    receiver: str,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Resolve the audit record's ``runtime`` block at the writer.

    The serving model resolves caller-first (an explicit ``runtime.model``
    already on the decision), then from the ``OACP_RUNTIME_MODEL``
    environment variable the invoking session exports; both are normalized
    before the record is written, with ``model_source`` naming the
    provenance and ``model_raw`` preserving any input the normalization
    changed. With no signal the field is an explicit unknown — ``None``
    plus a ``model_unknown_reason`` — never a silent default. The
    *requested* model (harness configuration, settings files) is
    deliberately never consulted: a request can be served by a different
    model, and filling from it would reintroduce the confound this field
    exists to remove.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    runtime: Dict[str, Any] = dict(supplied) if isinstance(supplied, dict) else {}
    runtime["agent"] = str(runtime.get("agent") or receiver)
    for stale_key in ("model_source", "model_context", "model_raw", "model_unknown_reason"):
        runtime.pop(stale_key, None)

    raw = runtime.get("model")
    source: Optional[str] = None
    if str(raw if raw is not None else "").strip():
        source = "caller"
    else:
        raw = env_map.get(RUNTIME_MODEL_ENV_VAR)
        if str(raw if raw is not None else "").strip():
            source = f"env:{RUNTIME_MODEL_ENV_VAR}"

    model, context = normalize_runtime_model(raw)
    if model is None:
        runtime["model"] = None
        runtime["model_source"] = None
        runtime["model_unknown_reason"] = (
            "no serving-model signal: decision carried no runtime.model and "
            f"{RUNTIME_MODEL_ENV_VAR} is unset; the requested model is never "
            "used as a fallback"
        )
        return runtime

    raw_text = str(raw)
    runtime["model"] = model
    runtime["model_source"] = source
    if context is not None:
        runtime["model_context"] = context
    if raw_text != model:
        runtime["model_raw"] = raw_text
    return runtime


def write_audit_record(
    audit_dir: Path,
    decision: Dict[str, Any],
    *,
    config: Dict[str, Any],
    message: Dict[str, Any],
    message_path: Path,
    policy_path: Path,
    receiver: str,
    now_utc: Optional[dt.datetime] = None,
    hold_lock: Optional[contextlib.ExitStack] = None,
) -> Path:
    """Persist a documented audit event without mutating evaluator stdout.

    The evaluator's result block is admission-time state. Receivers still own
    terminal result updates, human outcomes, and message-auth attachment.
    """
    result_block = decision.get("result")
    completion_kind = (
        result_block.get("completion_kind") if isinstance(result_block, dict) else None
    )
    if completion_kind not in PINNED_COMPLETION_KINDS:
        # Caller-supplied keys merge into evaluator-written records; without
        # this write-time check an off-enum kind lands in the durable record
        # and every downstream reader must special-case it.
        raise ValueError(
            "refusing to write audit record: result.completion_kind "
            f"{completion_kind!r} is not a pinned completion kind "
            f"({', '.join(sorted(PINNED_COMPLETION_KINDS))})"
        )
    if decision.get("decision") == "auto_accepted" and decision.get("scope_envelope") is None:
        # An admitted decision always carries a bound: profiled admissions
        # envelope from the profile, profileless admissions from the
        # documented default, and review-loop continuations the granted
        # review_loop scope. Null-on-admitted with no bound at all is a
        # schema violation, not a persistable state.
        review_block = decision.get("review_continuation")
        review_bound = (
            isinstance(review_block, dict)
            and review_block.get("decision") == "accepted"
            and isinstance(review_block.get("scope"), dict)
        )
        if not review_bound:
            raise ValueError(
                "refusing to write audit record: an admitted decision must "
                "carry a scope bound — a task scope_envelope or an accepted "
                "review_continuation scope (null with neither is a schema "
                "violation)"
            )
    audit_dir.mkdir(parents=True, exist_ok=True)
    created_at = utc_now_iso(now_utc)
    autonomy = config.get("autonomy")
    raw_thresholds = (
        autonomy.get("auto_review_thresholds")
        if isinstance(autonomy, dict)
        else None
    )
    thresholds = {
        key: raw_thresholds.get(key) if isinstance(raw_thresholds, dict) else None
        for key in NUMERIC_THRESHOLD_KEYS
    }
    audit_record = dict(decision)
    audit_record.setdefault("created_at_utc", created_at)
    audit_record.setdefault("message_subject", message.get("subject"))
    audit_record.setdefault("message_path", str(message_path))
    audit_record.setdefault("policy_path", str(policy_path))
    audit_record.setdefault("thresholds", thresholds)
    audit_record["runtime"] = resolve_runtime_block(
        audit_record.get("runtime"), receiver=receiver
    )

    message_id = str(decision.get("message_id") or "missing-message-id")
    # Every persisted evaluation carries an identity; re-evaluations of the
    # same message reference their predecessor through it instead of
    # accumulating indistinguishable duplicates.
    audit_record.setdefault(
        "evaluation_id",
        evaluation_identity(
            receiver,
            message_id,
            str(decision.get("message_sha256") or ""),
            created_at,
        ),
    )
    audit_record.setdefault("supersedes_evaluation_id", None)
    safe_message_id = _safe_message_id(message_id)
    stamp = created_at.replace(":", "").replace("-", "")
    audit_path = audit_dir / f"{stamp}_{safe_message_id}.yaml"
    if audit_path.exists():
        # A distinct evaluation of the same message in the same second
        # (e.g. an amended body superseding its predecessor) is legitimate
        # now that logical duplicates adopt or supersede before reaching
        # this writer — disambiguate by evaluation identity instead of
        # refusing. A true duplicate shares the identity and still trips
        # the in-lock existence check below.
        eval_suffix = str(audit_record["evaluation_id"]).replace("eval-", "")[:8]
        audit_path = audit_dir / f"{stamp}_{safe_message_id}_{eval_suffix}.yaml"
    content = yaml.safe_dump(audit_record, sort_keys=False, allow_unicode=True)

    def _publish_locked() -> None:
        temp_path: Optional[Path] = None
        if audit_path.exists():
            raise FileExistsError(f"audit record already exists: {audit_path}")
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=audit_dir,
                prefix=f".{audit_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, audit_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    if hold_lock is not None:
        # Transactional caller: the record's lock is entered on the
        # caller's stack BEFORE publication and stays held until the
        # caller's whole transaction commits or rolls back, so a
        # per-record writer can never commit an update to this record
        # that a rollback would then delete.
        hold_lock.enter_context(locked_audit(audit_path))
        _publish_locked()
    else:
        with locked_audit(audit_path):
            _publish_locked()

    return audit_path


def validate_receiver_config(config: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if "autonomy" not in config:
        return []

    autonomy = config["autonomy"]
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

    autonomy = config.get("autonomy", {"default_mode": "always_pause"})
    assert isinstance(autonomy, dict)  # Guaranteed by validation above.
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
    unfenced_candidates: List[int] = []
    fenced_candidates: List[int] = []
    fence_char: Optional[str] = None
    fence_length = 0
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        if fence_char is not None:
            if re.fullmatch(
                rf"{re.escape(fence_char)}{{{fence_length},}}[ \t]*",
                stripped,
            ):
                fence_char = None
                fence_length = 0
                continue
            candidates = fenced_candidates
        else:
            fence_match = re.match(r"(?P<marker>`{3,}|~{3,})", stripped)
            if fence_match:
                marker = fence_match.group("marker")
                fence_char = marker[0]
                fence_length = len(marker)
                continue
            candidates = unfenced_candidates

        if re.match(r"^(\s*)task_profile:\s*(.*)$", line):
            candidates.append(index)

    # Fences lower a candidate's priority instead of deleting it. This keeps
    # fenced-only declarations (including after an unclosed opener) operative
    # while preventing a fenced example from shadowing an unfenced declaration.
    for index in unfenced_candidates + fenced_candidates:
        line = lines[index]
        match = re.match(r"^(\s*)task_profile:\s*(.*)$", line)
        assert match is not None

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


# Documented default bounds for profileless admitted requests
# (brainstorm-class): reply-only work, every risk flag false. The
# profile exemption is admission-only — the sender needn't author a
# profile, but the bound always exists.
DEFAULT_PROFILELESS_ENVELOPE_MINUTES = 25
DEFAULT_PROFILELESS_ENVELOPE_FILES = 2
SCOPE_ENVELOPE_SOURCE_PROFILE = "task_profile"
SCOPE_ENVELOPE_SOURCE_DEFAULT = "default_profileless"


def default_scope_envelope(message: Dict[str, Any]) -> Dict[str, Any]:
    """Construct the documented default envelope for a profileless request.

    Bounds: 25 minutes / 2 files / reply-only (`sends_oacp_reply_only`
    true, every other capability and risk flag false). `risk_tier` mirrors
    the message's own declared `priority` when it is a valid tier — it is
    the sender's severity claim — else `P2`. A sender that legitimately
    needs more attaches a voluntary task_profile (the supported override
    path); the profile envelope then replaces this default entirely.
    """
    priority = str(message.get("priority") or "").strip()
    envelope: Dict[str, Any] = {
        "estimated_minutes": DEFAULT_PROFILELESS_ENVELOPE_MINUTES,
        "expected_files_touched": DEFAULT_PROFILELESS_ENVELOPE_FILES,
        "risk_tier": priority if priority in {"P0", "P1", "P2", "P3"} else "P2",
        "target_repo": "",
    }
    for key in LEGACY_PROFILE_BOOL_FIELDS + SIDE_EFFECT_BOOL_FIELDS:
        envelope[key] = False
    envelope["sends_oacp_reply_only"] = True
    envelope["continuation_grants"] = {}
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
    """Hash parsed policy data so comments and formatting do not create drift.

    The ``auth`` trailer key is excluded: it is authorization metadata, and
    the hash must name the same policy content signed or unsigned.
    """
    serialized = json.dumps(
        {key: value for key, value in config.items() if key != "auth"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _profile_is_complete(profile: Optional[Dict[str, Any]]) -> bool:
    return isinstance(profile, dict) and all(key in profile for key in COMPLETE_PROFILE_FIELDS)


# Every side-effect flag other than `sends_oacp_reply_only`: the legacy
# aggregate plus each granular capability. All must be false for a profile
# to have the reply-only shape.
REPLY_ONLY_OTHER_SIDE_EFFECT_FIELDS = (
    "external_side_effects",
    *COVERABLE_CONTINUATION_FIELDS,
)


def _reply_only_profile_shape(
    profile: Optional[Dict[str, Any]],
    envelope: Optional[Dict[str, Any]],
) -> bool:
    """The reply-only profile shape: a complete declared profile whose
    ``sends_oacp_reply_only`` is true and every other side-effect flag —
    ``external_side_effects`` and each granular capability — is false.

    Declaration-only by construction: the profileless default envelope is
    reply-only too, but it declares nothing, and the content-sensitivity
    carve-out keys on what the sender declared. A missing, unparsable, or
    contradictory profile, or one that omits ``sends_oacp_reply_only``,
    does not have the shape.
    """
    if envelope is None or not _profile_is_complete(profile):
        return False
    if not envelope.get("sends_oacp_reply_only"):
        return False
    return not any(envelope.get(field) for field in REPLY_ONLY_OTHER_SIDE_EFFECT_FIELDS)


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
    if NEGATION_PREFIX_RE.search(clause_prefix) is not None:
        return True

    match_line_start = prefix.rfind("\n") + 1
    preceding_lines = body[:match_line_start].splitlines()
    for line in reversed(preceding_lines):
        stripped = line.strip()
        if not stripped:
            return False
        is_heading = bool(ATX_HEADING_RE.match(line)) or stripped.endswith(":")
        if is_heading:
            return BLOCK_NEGATION_PREFIX_RE.search(stripped) is not None
    return False


def _contextual_match_is_negated(
    label: str,
    body: str,
    match: re.Match[str],
) -> bool:
    """Require an independent negation basis for each hard-stop occurrence."""
    if not _match_is_negated(body, match):
        return False
    if NEGATION_TERM_RE.search(match.group(0)) is not None:
        return False

    prefix = body[:match.start()]
    boundary = max(prefix.rfind(mark) for mark in ("\n", ".", "!", "?", ";", "—", "–"))
    clause_prefix = prefix[boundary + 1:]
    if label == "install dependency":
        cue_pattern = INSTALL_VERB_RE
        governance_pattern = INSTALL_NEGATION_GOVERNANCE_RE
    elif label == "public repo":
        cue_pattern = PUBLIC_REPO_RE
        governance_pattern = PUBLIC_REPO_NEGATION_GOVERNANCE_RE
    else:
        return False
    prior_occurrences = list(cue_pattern.finditer(clause_prefix))
    governance_prefix = (
        clause_prefix[prior_occurrences[-1].end():]
        if prior_occurrences
        else clause_prefix
    )
    governance_match = governance_pattern.search(governance_prefix)
    if governance_match is None:
        return False
    return NEGATION_TERM_RE.search(
        governance_prefix[:governance_match.start()]
    ) is None


def _match_is_reference_only(
    label: str,
    body: str,
    match: re.Match[str],
) -> bool:
    """Recognize only the descriptive lexical contexts proven in field data.

    These patterns are intentionally narrow. They do not make a whole class
    demotable: they identify the repository's merge-method setting and
    dependency-introspection wording that describe a token instead of asking
    the receiver to perform its action.
    """
    if not _match_is_clause_bounded(match):
        return False
    left = max(body.rfind(mark, 0, match.start()) for mark in ("\n", ".", "!", "?", ";"))
    right_candidates = [
        position
        for mark in ("\n", ".", "!", "?", ";")
        if (position := body.find(mark, match.end())) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(body)
    clause_start = left + 1
    clause = body[clause_start:right]

    if label == "merge":
        reference_patterns = (
            re.compile(
                r"\bmerge(?:[- ]commit)?\s+"
                r"(?:method|setting|settings|strategy|mode)\b",
                re.IGNORECASE,
            ),
        )
    elif label == "install dependency":
        reference_patterns = (
            re.compile(
                r"\bwhat\s+(?:a\s+)?(?:pip\s+)?install\b"
                r"[^.!?;\n—–]{0,160}?\b"
                r"(?:pulls?|installs?|includes?|requires?)\b"
                r"[^.!?;\n—–]{0,160}?\bdependenc(?:y|ies)\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:install/build|package\s+installs?)\b"
                r"[^.!?;\n—–]{0,80}?\bread\s+as\b"
                r"[^.!?;\n—–]{0,80}?\bdependenc(?:y|ies)(?:-class)?\b",
                re.IGNORECASE,
            ),
        )
    else:
        return False

    for reference_pattern in reference_patterns:
        for reference in reference_pattern.finditer(clause):
            reference_start = clause_start + reference.start()
            reference_end = clause_start + reference.end()
            if reference_start <= match.start() and match.end() <= reference_end:
                return True
    return False


def _match_is_clause_bounded(match: re.Match[str]) -> bool:
    return re.search(r"[.!?;\n—–]", match.group(0)) is None


def _contextual_non_demotable_basis(
    label: str,
    body: str,
    match: re.Match[str],
) -> str:
    """Return the shared verdict/provenance disposition for one hard match."""
    if not _match_is_clause_bounded(match):
        return "non_demotable"
    if label in {
        "install dependency",
        "public repo",
    } and _contextual_match_is_negated(label, body, match):
        return "negated"
    if label == "install dependency" and _match_is_reference_only(
        label, body, match
    ):
        return "reference_only"
    return "non_demotable"


def _guardrails_content_spans(body: str) -> List[Tuple[int, int]]:
    return [match.span("content") for match in GUARDRAILS_FENCE_RE.finditer(body)]


def _match_is_in_spans(
    match: re.Match[str],
    spans: Sequence[Tuple[int, int]],
) -> bool:
    return any(start <= match.start() and match.end() <= end for start, end in spans)


def _collect_lexical_provenance(
    body: str,
    profile: Optional[Dict[str, Any]],
    envelope: Optional[Dict[str, Any]],
    policy: Optional[Dict[str, Any]],
    msg_type: str,
) -> List[Dict[str, Any]]:
    """Return every lexical match with its source span and disposition basis.

    Spans are zero-based, end-exclusive Unicode-code-point offsets into the
    original message body. The list is source ordered; overlapping patterns
    remain separate evidence because they belong to distinct policy classes.
    """
    fence_spans = _guardrails_content_spans(body)
    profile_complete = _profile_is_complete(profile)
    external_policy = None
    if isinstance(policy, dict) and isinstance(policy.get("thresholds"), dict):
        external_policy = policy["thresholds"].get("external_side_effects")
    content_reply_only = _reply_only_profile_shape(profile, envelope)
    profileless_type = (
        profile is None
        and isinstance(policy, dict)
        and msg_type in policy.get("allow_without_task_profile", ())
    )
    profileless_risk = (
        profile is None
        and isinstance(policy, dict)
        and msg_type not in policy.get("allow_without_task_profile", ())
        and msg_type not in REVIEW_LIFECYCLE_TYPES
    )

    def basis(
        category: str,
        label: str,
        match: re.Match[str],
        profile_field: Optional[str] = None,
    ) -> str:
        fenced = _match_is_in_spans(match, fence_spans)
        if category == "destructive_command":
            return "non_demotable"
        if category == "side_effect":
            if profileless_risk:
                return "profileless_risk"
            if fenced:
                return "guardrails_fence"
            if _match_is_negated(body, match):
                return "negated"
            if _match_is_reference_only(label, body, match):
                return "reference_only"
            if profileless_type:
                return "profileless_type"
            if profile_complete and envelope is not None and not envelope["external_side_effects"]:
                return "profile_false"
            if external_policy == "allow" and envelope is not None and envelope["external_side_effects"]:
                return "policy_allowed"
            if label == "merge" and envelope is not None and envelope.get("merges_pr"):
                return "profile_true"
            return "affirmative"
        if category == "non_demotable_side_effect":
            if profileless_risk:
                return "profileless_risk"
            return _contextual_non_demotable_basis(label, body, match)
        if category == "sensitive_scope":
            if fenced:
                return "guardrails_fence"
            if _match_is_negated(body, match):
                return "negated"
            if (
                profile_field is not None
                and profile_complete
                and envelope is not None
                and not envelope[profile_field]
            ):
                return "profile_false"
            return "affirmative"
        if category == "content_sensitivity":
            return "reply_only_advisory" if content_reply_only else "non_demotable"
        if category == "non_demotable_sensitive_scope":
            return _contextual_non_demotable_basis(label, body, match)
        if category == "ambiguous_scope":
            if fenced:
                return "guardrails_fence"
            if _match_is_negated(body, match):
                return "negated"
            return "affirmative"
        return "profileless_risk"

    hits: List[Dict[str, Any]] = []

    def append_matches(
        category: str,
        patterns: Sequence[Tuple[str, re.Pattern[str]]],
    ) -> None:
        for label, pattern in patterns:
            for match in pattern.finditer(body):
                hits.append({
                    "pattern": label,
                    "category": category,
                    "span": {"start": match.start(), "end": match.end()},
                    "demotion_basis": basis(category, label, match),
                })

    append_matches("destructive_command", DESTRUCTIVE_PATTERNS)
    append_matches("side_effect", SIDE_EFFECT_VERB_PATTERNS)
    append_matches("non_demotable_side_effect", NON_DEMOTABLE_SIDE_EFFECT_PATTERNS)
    for label, pattern, profile_field in DECLARATION_AWARE_SENSITIVE_PATTERNS:
        for match in pattern.finditer(body):
            hits.append({
                "pattern": label,
                "category": "sensitive_scope",
                "span": {"start": match.start(), "end": match.end()},
                "demotion_basis": basis(
                    "sensitive_scope", label, match, profile_field
                ),
            })
    append_matches("content_sensitivity", CONTENT_SENSITIVITY_PATTERNS)
    append_matches(
        "non_demotable_sensitive_scope", NON_DEMOTABLE_SENSITIVE_PATTERNS
    )
    append_matches("ambiguous_scope", AMBIGUOUS_SCOPE_PATTERNS)
    if profileless_risk:
        append_matches("profileless_risk", PROFILELESS_ONLY_RISK_PATTERNS)

    unknown_bases = {
        hit["demotion_basis"] for hit in hits
    } - LEXICAL_DEMOTION_BASES
    if unknown_bases:
        raise ValueError(
            "unregistered lexical demotion basis: "
            + ", ".join(sorted(unknown_bases))
        )

    return sorted(
        hits,
        key=lambda hit: (
            hit["span"]["start"],
            hit["span"]["end"],
            hit["category"],
            hit["pattern"],
        ),
    )


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
    demote_labels: FrozenSet[str] = frozenset(),
) -> Optional[str]:
    for label, pattern in patterns:
        for match in pattern.finditer(body):
            if _match_is_negated(body, match):
                _record_lexical_note(notes, "lexical_advisory_negated", label)
                continue
            if _match_is_reference_only(label, body, match):
                _record_lexical_note(
                    notes, "lexical_advisory_reference_only", label
                )
                continue
            if demote_declared or label in demote_labels:
                _record_lexical_note(notes, "lexical_advisory_declared", label)
                continue
            return label
    return None


def _first_contextual_non_demotable_match(
    patterns: Sequence[Tuple[str, re.Pattern[str]]],
    body: str,
    notes: List[Dict[str, str]],
    *,
    contextual_labels: FrozenSet[str],
) -> Optional[str]:
    """Keep a class hard while demoting only documented contextual matches."""
    for label, pattern in patterns:
        for match in pattern.finditer(body):
            if label in contextual_labels:
                match_basis = _contextual_non_demotable_basis(label, body, match)
                if match_basis == "negated":
                    _record_lexical_note(notes, "lexical_advisory_negated", label)
                    continue
                if match_basis == "reference_only":
                    _record_lexical_note(
                        notes, "lexical_advisory_reference_only", label
                    )
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
    message_raw: Optional[bytes] = None,
) -> str:
    """Hash the message: snapshot bytes first, then path, then a stable fallback.

    ``message_raw`` is the caller's verified snapshot — when provided, the
    recorded hash names exactly the bytes that were verified and parsed,
    never a fresh (swappable) read of the path.
    """
    if message_raw is not None:
        return hashlib.sha256(message_raw).hexdigest()
    if message_path is not None:
        return hashlib.sha256(message_path.read_bytes()).hexdigest()
    serialized = yaml.safe_dump(message, sort_keys=True, allow_unicode=False).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


_EVALUATOR_SOURCE = "scripts/autonomy_gate.py"
_evaluator_provenance_cache: Optional[Dict[str, Any]] = None


def _git_sha_if_committed(path: Path) -> Optional[str]:
    """HEAD short SHA, only when this file's bytes match the committed blob.

    A SHA naming a commit whose gate code is NOT what ran (dirty tree,
    local-ahead checkout, untracked copy under someone's repo) is worse
    than no SHA — `content_sha256` remains the load-bearing identity.
    """
    import subprocess

    try:
        directory = str(path.parent)
        tracked = subprocess.run(
            ["git", "-C", directory, "ls-files", "--error-unmatch", str(path)],
            capture_output=True, timeout=5,
        )
        if tracked.returncode != 0:
            return None
        unchanged = subprocess.run(
            ["git", "-C", directory, "diff", "--quiet", "HEAD", "--", str(path)],
            capture_output=True, timeout=5,
        )
        if unchanged.returncode != 0:
            return None
        head = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if head.returncode != 0:
            return None
        return head.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def evaluator_provenance() -> Dict[str, Any]:
    """Self-stamped identity of the gate code producing a decision.

    Receivers copy this block verbatim into the audit record instead of
    composing an `evaluator` field by hand. Hand evaluation (no executed
    gate) is the only case a receiver authors itself: `executed: false`
    with no hash.
    """
    global _evaluator_provenance_cache
    if _evaluator_provenance_cache is None:
        path = Path(__file__).resolve()
        _evaluator_provenance_cache = {
            "source": _EVALUATOR_SOURCE,
            "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "git_sha": _git_sha_if_committed(path),
            "executed": True,
        }
    return dict(_evaluator_provenance_cache)


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


def _safe_message_id(message_id: str) -> str:
    """Filesystem-safe form of a message id (never trusted as identity)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", message_id).strip("._")
    return safe[:200] or "missing-message-id"


def evaluation_identity(
    receiver: str,
    message_id: str,
    message_sha256: str,
    created_at_utc: str,
) -> str:
    """Deterministic identity for one evaluation event.

    Derived from what makes an evaluation distinct — who evaluated, which
    message, which exact bytes, and when — so re-running the writer on the
    same event reproduces the same id instead of minting a fresh one.
    Distinct evaluations of the same message (an amended body, a later
    re-evaluation) differ in ``message_sha256`` or ``created_at_utc`` and
    get distinct ids, which is what supersession chains reference.
    """
    digest = hashlib.sha256(
        "\n".join((receiver, message_id, message_sha256, created_at_utc)).encode("utf-8")
    ).hexdigest()
    return f"eval-{digest[:16]}"


def find_prior_evaluations(
    audit_dir: Optional[Path],
    receiver: str,
    message_id: str,
) -> List[Tuple[Path, Dict[str, Any]]]:
    """Return every readable audit record matching this logical identity."""
    if not message_id or audit_dir is None or not audit_dir.is_dir():
        return []
    priors: List[Tuple[Path, Dict[str, Any]]] = []
    for audit_path in sorted(audit_dir.glob("*.yaml")):
        try:
            audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(audit, dict):
            continue
        if audit.get("message_id") == message_id and audit.get("receiver") == receiver:
            priors.append((audit_path, audit))
    return priors


def _prior_evaluation_id(prior: Mapping[str, Any], receiver: str) -> str:
    """A prior's on-disk evaluation_id, or the deterministic identity it
    will be stamped with (``supersede_audit_record`` and adoption both use
    the same derivation, so a computed reference always resolves)."""
    return str(
        prior.get("evaluation_id")
        or evaluation_identity(
            receiver,
            str(prior.get("message_id") or ""),
            str(prior.get("message_sha256") or ""),
            str(prior.get("created_at_utc") or ""),
        )
    )


def supersede_audit_record(
    audit_path: Path,
    *,
    superseded_by: str,
    now_utc: Optional[dt.datetime] = None,
) -> None:
    """Close a stale evaluation in favor of a newer one, non-destructively.

    A state update under the audit lock: ``final_state`` moves to
    ``superseded``, the superseding evaluation is referenced, and a
    completion stamp closes the record. Admission history — decision,
    reason codes, checkpoint blocks, human outcomes — is preserved
    verbatim; supersession is how off-vocabulary or stale records leave
    the live corpus without rewriting what they said.
    """
    with locked_audit(audit_path):
        _supersede_audit_record_locked(
            audit_path, superseded_by=superseded_by, now_utc=now_utc
        )


def _supersede_audit_record_locked(
    audit_path: Path,
    *,
    superseded_by: str,
    now_utc: Optional[dt.datetime] = None,
) -> None:
    """The lock-free half of ``supersede_audit_record``.

    The caller MUST already hold ``locked_audit(audit_path)`` —
    ``persist_evaluation`` pre-acquires every affected record's lock for
    its whole capture/mutate/restore span (flock is not reentrant, so it
    cannot call the locking wrapper).
    """
    # Strict load: automatic supersession re-serializes the record, and
    # rewriting a plain-loaded mapping would silently collapse
    # duplicate-key evidence (PyYAML keeps only the later value). Fail
    # closed and leave malformed evidence untouched instead.
    record = load_yaml_strict(audit_path)
    result = record.setdefault("result", {})
    if not isinstance(result, dict):
        raise ValueError(f"{audit_path}: result must be a mapping")
    if result.get("final_state") == "superseded":
        return
    if result.get("final_state") not in FINAL_STATES | {"pending"}:
        # Preserve legacy off-enum run state as history before the
        # overwrite — the original value is part of what supersession
        # documents.
        result["legacy_final_state"] = result.get("final_state")
    result["final_state"] = "superseded"
    if not result.get("completed_at_utc"):
        result["completed_at_utc"] = utc_now_iso(now_utc)
    if not record.get("evaluation_id"):
        record["evaluation_id"] = evaluation_identity(
            str(record.get("receiver") or ""),
            str(record.get("message_id") or ""),
            str(record.get("message_sha256") or ""),
            str(record.get("created_at_utc") or ""),
        )
    record["superseded_by_evaluation_id"] = superseded_by
    atomic_replace_yaml(audit_path, record)


def persist_evaluation(
    audit_dir: Path,
    decision: Dict[str, Any],
    *,
    config: Dict[str, Any],
    message: Dict[str, Any],
    message_path: Path,
    policy_path: Path,
    receiver: str,
) -> Dict[str, Any]:
    """Persist a decision as one logical-identity transaction.

    The prior scan, the adopt-or-write choice, and predecessor supersession
    are serialized under a stable lock keyed by (receiver, message id) —
    per-file locks alone cannot prevent two concurrent evaluations of the
    same logical message from both scanning an empty directory and both
    staying live. Adoption also self-heals a crashed predecessor
    transaction: any other live prior of the adopted identity is superseded
    on the next evaluation, so exactly one evaluation per logical message
    stays live even across a writer that died between its write and its
    supersession pass.

    Returns ``{"action": "adopted"|"written", "audit_path", "evaluation_id",
    "superseded": [paths]}``. The transaction pre-acquires every affected
    record's ``locked_audit`` in deterministic path order and holds them
    across byte capture, mutation, and any restore — including the newly
    created successor, whose lock is entered before publication and held
    until the transaction commits or rolls back — so per-record audit
    writers serialize with it instead of losing a committed update to its
    rollback; the authoritative record view is re-read under those locks.
    It fails closed and is rollback-capable for every failure class,
    validation and filesystem alike: every live predecessor is
    strict-load preflighted BEFORE anything is written, so a predecessor
    that cannot be superseded automatically (e.g. duplicate-key YAML —
    evidence is preserved, never normalized) raises with nothing
    persisted; any later failure restores every already-mutated
    predecessor byte-for-byte (read-back verified) and removes a
    just-written successor under the same locks before raising.
    Reporting success while a second evaluation stays live — or leaving a
    predecessor linked to a successor that was rolled back — would defeat
    the transaction's one-live-record invariant.
    """
    message_id = str(message.get("id") or "")
    audit_dir.mkdir(parents=True, exist_ok=True)
    identity_anchor = audit_dir / f".identity_{receiver}_{_safe_message_id(message_id)}"

    # Original bytes of every file this transaction has mutated, in
    # mutation order. Any failure — validation OR filesystem — restores
    # them all (and removes a just-written successor) before raising, so
    # the caller never observes duplicate live evaluations or a
    # predecessor linked to a successor that no longer exists.
    mutated: List[Tuple[Path, bytes]] = []

    def _restore_pre_call_state(successor_path: Optional[Path]) -> None:
        problems: List[str] = []
        for path, original in mutated:
            try:
                atomic_replace_bytes(path, original)
                if path.read_bytes() != original:
                    problems.append(f"{path.name}: read-back mismatch after restore")
            except OSError as exc:
                problems.append(f"{path.name}: restore failed ({exc})")
        if successor_path is not None:
            try:
                successor_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                problems.append(
                    f"{successor_path.name}: rollback unlink failed ({exc})"
                )
            if successor_path.exists():
                problems.append(
                    f"{successor_path.name}: still present after rollback"
                )
        if problems:
            raise OSError(
                "logical-identity transaction rollback incomplete — manual "
                "repair required: " + "; ".join(problems)
            )

    def _supersede_all_or_restore(
        targets: List[Path],
        successor_id: str,
        successor_path: Optional[Path],
    ) -> List[Path]:
        superseded: List[Path] = []
        for prior_path in targets:
            try:
                original = prior_path.read_bytes()
                mutated.append((prior_path, original))
                _supersede_audit_record_locked(
                    prior_path, superseded_by=successor_id
                )
            except Exception as exc:
                _restore_pre_call_state(successor_path)
                raise ValueError(
                    "logical-identity transaction failed: live predecessor "
                    f"{prior_path.name} could not be superseded ({exc}) — "
                    "pre-call state restored; repair it manually before "
                    "re-evaluating this message"
                ) from exc
            superseded.append(prior_path)
        return superseded

    def _stamp_adopted_links(
        prior_path: Path, adopted_id: str, superseded_ids: List[str]
    ) -> None:
        """Make an adopted survivor a strictly resolvable successor.

        The strict resolver requires the successor file to carry its
        evaluation_id and to reference every predecessor it absorbed —
        a pre-identity adopted record satisfies neither, which would
        leave its healed predecessors dangling. The caller already holds
        this record's ``locked_audit`` for the whole transaction span.
        """
        record = load_yaml_strict(prior_path)
        changed = False
        if not record.get("evaluation_id"):
            record["evaluation_id"] = adopted_id
            changed = True
        existing = record.get("superseded_evaluation_ids")
        merged = list(existing) if isinstance(existing, list) else []
        for superseded_id in superseded_ids:
            if superseded_id not in merged:
                merged.append(superseded_id)
                changed = True
        if changed:
            record["superseded_evaluation_ids"] = merged
            atomic_replace_yaml(prior_path, record)

    with locked_audit(identity_anchor), contextlib.ExitStack() as record_locks:
        scan = find_prior_evaluations(audit_dir, receiver, message_id)
        candidate_paths = sorted({
            path
            for path, prior in scan
            if (prior.get("result") or {}).get("final_state") != "superseded"
        })
        # Deterministic-order lock acquisition over every record this
        # transaction may mutate, held across byte capture, mutation, and
        # any restore — per-record audit writers (human-outcome recording,
        # message_auth attachment) serialize with the transaction instead
        # of having a committed update erased by its rollback.
        for path in candidate_paths:
            record_locks.enter_context(locked_audit(path))
        # Authoritative view: re-read under the held locks (the unlocked
        # scan can be stale against a concurrent per-record writer). This
        # is also the strict preflight — fail the whole transaction while
        # nothing has changed.
        live: List[Tuple[Path, Dict[str, Any]]] = []
        preflight_failures: List[Tuple[Path, str]] = []
        for path in candidate_paths:
            try:
                strict = load_yaml_strict(path)
                strict_result = strict.get("result")
                if strict_result is not None and not isinstance(strict_result, dict):
                    raise ValueError("result must be a mapping")
            except FileNotFoundError:
                continue  # removed since the scan — nothing left to close
            except ValueError as exc:
                preflight_failures.append((path, str(exc)))
                continue
            if (strict.get("result") or {}).get("final_state") == "superseded":
                continue  # closed since the scan — already resolved
            live.append((path, strict))
        if preflight_failures:
            details = "; ".join(
                f"{path.name}: {reason}" for path, reason in preflight_failures
            )
            raise ValueError(
                "logical-identity transaction failed: live predecessor(s) "
                f"could not be superseded ({details}) — evidence preserved, "
                "nothing written; repair them manually before re-evaluating "
                "this message"
            )
        adoptable = [
            (path, prior)
            for path, prior in live
            if prior.get("message_sha256") == decision.get("message_sha256")
            and prior.get("policy_sha256") == decision.get("policy_sha256")
            and prior.get("decision") == decision.get("decision")
        ]
        if adoptable:
            prior_path, prior = max(adoptable, key=lambda item: item[0].name)
            adopted_id = _prior_evaluation_id(prior, receiver)
            others = [
                (path, other) for path, other in live if path != prior_path
            ]
            if others:
                try:
                    original = prior_path.read_bytes()
                    mutated.append((prior_path, original))
                    _stamp_adopted_links(
                        prior_path,
                        adopted_id,
                        [_prior_evaluation_id(o, receiver) for _p, o in others],
                    )
                except Exception as exc:
                    _restore_pre_call_state(None)
                    raise ValueError(
                        "logical-identity transaction failed: adopted "
                        f"evaluation {prior_path.name} could not be stamped "
                        f"as successor ({exc}) — pre-call state restored"
                    ) from exc
            superseded = _supersede_all_or_restore(
                [path for path, _o in others], adopted_id, None
            )
            return {
                "action": "adopted",
                "audit_path": prior_path,
                "evaluation_id": adopted_id,
                "superseded": superseded,
            }

        if live:
            # The newest predecessor keeps the single-valued back-pointer
            # (each writer stamped it against the then-newest live record);
            # the full list is what lets the strict resolver follow every
            # predecessor of a multi-prior self-heal back to this successor.
            ordered = sorted(live, key=lambda item: item[0].name)
            decision["supersedes_evaluation_id"] = _prior_evaluation_id(
                ordered[-1][1], receiver
            )
            decision["superseded_evaluation_ids"] = [
                _prior_evaluation_id(prior, receiver) for _path, prior in ordered
            ]
        audit_path = write_audit_record(
            audit_dir,
            decision,
            config=config,
            message=message,
            message_path=message_path,
            policy_path=policy_path,
            receiver=receiver,
            hold_lock=record_locks,
        )
        written = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        new_id = str(written.get("evaluation_id"))
        superseded = _supersede_all_or_restore(
            [path for path, _prior in live], new_id, audit_path
        )
        return {
            "action": "written",
            "audit_path": audit_path,
            "evaluation_id": new_id,
            "superseded": superseded,
        }


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
        + PROFILELESS_ONLY_RISK_PATTERNS
    )
    return first_match(patterns, body) is not None


def normalize_review_loop_scope(
    block: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normalize a grant scope's ``review_loop`` sub-block.

    Every bound is explicit: the repository and PR pin the thread's subject,
    ``allowed_types`` pins which inbound lifecycle types may trigger a round,
    ``max_round`` and ``expires_at_utc`` bound depth and wall clock, and
    ``permitted_side_effects`` pins what the granted invocation may produce.
    A malformed block invalidates the whole grant surface — a partially
    understood authority is never honored.
    """
    if not isinstance(block, dict):
        return None, "review_loop_invalid"
    repository = block.get("repository")
    if not isinstance(repository, str) or not REPO_SLUG_RE.fullmatch(repository):
        return None, "review_loop_invalid"
    pr_number = block.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
        return None, "review_loop_invalid"
    allowed = block.get("allowed_types")
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(
            not isinstance(item, str)
            or item not in REVIEW_CONTINUATION_GRANTABLE_TYPES
            for item in allowed
        )
    ):
        return None, "review_loop_invalid"
    max_round = block.get("max_round")
    if not isinstance(max_round, int) or isinstance(max_round, bool) or max_round < 1:
        return None, "review_loop_invalid"
    expires = block.get("expires_at_utc")
    if not isinstance(expires, str):
        return None, "review_loop_invalid"
    try:
        dt.datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None, "review_loop_invalid"
    effects_in = block.get("permitted_side_effects", {})
    if effects_in is None:
        effects_in = {}
    if not isinstance(effects_in, dict) or any(
        key not in REVIEW_SIDE_EFFECT_FIELDS for key in effects_in
    ):
        return None, "review_loop_invalid"
    effects: Dict[str, bool] = {}
    for key in REVIEW_SIDE_EFFECT_FIELDS:
        value = effects_in.get(key, False)
        if not isinstance(value, bool):
            return None, "review_loop_invalid"
        effects[key] = value
    return {
        "repository": repository.lower(),
        "pr_number": pr_number,
        "allowed_types": sorted(set(allowed)),
        "max_round": max_round,
        "expires_at_utc": expires,
        "permitted_side_effects": effects,
    }, None


def normalize_continuation_scope(
    scope: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(scope, dict):
        return None, "continuation_grant_missing_scope"

    normalized: Dict[str, Any] = {}
    has_review_loop = "review_loop" in scope
    for key in ("max_actual_minutes", "max_actual_files_touched"):
        value = scope.get(key)
        if value is None and has_review_loop:
            # A review-only grant may omit the task budget keys; they
            # default to zero so the grant carries no task-continuation
            # authority (any task follow-up breaches immediately).
            normalized[key] = 0
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, f"{key}_invalid"
        normalized[key] = value
    for key in COVERABLE_CONTINUATION_FIELDS:
        value = scope.get(key, False)
        if not isinstance(value, bool):
            return None, f"{key}_invalid"
        normalized[key] = value
    if has_review_loop:
        review_scope, error = normalize_review_loop_scope(scope.get("review_loop"))
        if error:
            return None, error
        normalized["review_loop"] = review_scope
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


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _aware_utc(value: Optional[dt.datetime]) -> dt.datetime:
    """Normalize an optional caller-supplied clock to timezone-aware UTC.

    Mirrors ``message_expired``: a missing clock reads the real time, a
    naive one is taken as already-UTC, and an aware one is converted — so
    every comparison in the review path happens on one timeline.
    """
    now = value if value is not None else dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def _parse_utc_z(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc
    )


def _extract_review_context(message: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the review-loop declaration out of a lifecycle message body.

    Review bodies are YAML mappings by schema (``pr:``, ``branch:``,
    ``diff_summary:`` …). Declarations a grant match consumes are collected
    here; anything unreadable lands in ``errors`` so scope matching fails
    closed instead of guessing.
    """
    body = str(message.get("body") or "")
    try:
        parsed = yaml.safe_load(body)
    except yaml.YAMLError:
        parsed = None
    data = parsed if isinstance(parsed, dict) else {}

    context: Dict[str, Any] = {
        "repository": None,
        "pr_number": None,
        "round": None,
        "declared_head": None,
        "side_effects": list(DEFAULT_REVIEW_SIDE_EFFECTS),
        "grant_claim_present": "continuation_grants" in data,
        "errors": [],
    }

    repo = data.get("repo")
    if isinstance(repo, str) and REPO_SLUG_RE.fullmatch(repo.strip()):
        context["repository"] = repo.strip().lower()
    elif repo is not None:
        context["errors"].append("repo")

    pr_value = data.get("pr", message.get("related_pr"))
    if isinstance(pr_value, bool):
        context["errors"].append("pr")
    elif isinstance(pr_value, int) and pr_value >= 1:
        context["pr_number"] = pr_value
    elif isinstance(pr_value, str) and pr_value.strip().isdigit():
        context["pr_number"] = int(pr_value.strip())
    elif pr_value not in (None, ""):
        context["errors"].append("pr")

    round_value = data.get("round", 1)
    if isinstance(round_value, int) and not isinstance(round_value, bool) and round_value >= 1:
        context["round"] = round_value
    else:
        context["errors"].append("round")

    declared_head = data.get("declared_head")
    if isinstance(declared_head, str) and declared_head.strip():
        context["declared_head"] = declared_head.strip()
    elif declared_head not in (None, ""):
        context["errors"].append("declared_head")

    side_effects = data.get("side_effects")
    if side_effects is not None:
        if isinstance(side_effects, list) and all(
            isinstance(item, str) and item in REVIEW_SIDE_EFFECT_FIELDS
            for item in side_effects
        ):
            context["side_effects"] = sorted(set(side_effects))
        else:
            context["errors"].append("side_effects")

    return context


def _review_head_check(
    declared_head: Optional[str],
    actuals: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    """Compare the sender-declared head against the observed live head.

    The declared value is never trusted: equality is exact full-string
    comparison of complete SHAs, so a declared value sharing a prefix with
    the live head is still a mismatch. A mismatch (or a missing
    declaration) is recorded and the live head stays authoritative — the
    round still runs, because the guard is that the reviewer validates the
    live ref, not that the sender declared it correctly.
    """
    review_observed = (actuals or {}).get("review")
    live_head = None
    if isinstance(review_observed, dict):
        value = review_observed.get("live_head")
        if isinstance(value, str) and value.strip():
            live_head = value.strip().lower()

    declared = declared_head.lower() if isinstance(declared_head, str) else None
    if declared is None:
        status = "undeclared" if live_head else "unverified"
    elif live_head is None:
        status = "unverified"
    elif declared == live_head and _FULL_SHA_RE.fullmatch(declared):
        status = "match"
    else:
        status = "mismatch"
    return {
        "declared_head": declared_head,
        "live_head": live_head,
        "status": status,
    }, status == "mismatch"


def _audit_consumed_round(audit: Dict[str, Any]) -> bool:
    """True when this audit records a reviewer invocation that actually ran.

    Only executed rounds charge the grant's round budget: an auto-admitted
    continuation, or a pause whose recorded human outcome authorized the
    manual round. A declined or never-answered request started nothing —
    charging it would let dead asks exhaust ``max_round`` and defeat the
    promised re-grant path.
    """
    if audit.get("decision") == "auto_accepted":
        return True
    result = audit.get("result")
    outcome = result.get("human_outcome") if isinstance(result, dict) else None
    return (
        isinstance(outcome, dict)
        and outcome.get("recorded") is True
        and str(outcome.get("decision") or "") in {"approved", "modified"}
    )


def _prior_review_grant(
    message: Dict[str, Any],
    audit_dir: Optional[Path],
    receiver: str,
    now_utc: Optional[dt.datetime] = None,
) -> Tuple[Optional[Dict[str, Any]], int]:
    """Locate the governing review-loop grant for this thread.

    Returns ``(grant_state, prior_round_audits)``. Authorization and
    revocation are arbitrated on different clocks: an approval can govern
    only requests created after it (authority is never retroactive), while
    a denial takes effect the moment it is recorded — a denial decided
    before *evaluation* revokes queued work even when the request predates
    it. On a tie or a newer denial, the denial wins. ``prior_round_audits``
    counts this receiver's earlier round-consuming audit records in the
    thread — invocations that actually ran: an ``auto_accepted``
    continuation round, or a pause whose recorded human outcome authorized
    the manual round. Declined, unanswered, and context-only records
    consume nothing. This is the receiver-side floor for the effective
    round, so a sender cannot under-declare the round number to stay
    inside ``max_round`` — while a denied or never-answered request can
    never burn budget a later re-grant was promised to have.
    """
    if audit_dir is None or not audit_dir.is_dir():
        return None, 0

    approvals: List[Tuple[str, str, Dict[str, Any]]] = []
    denials: List[Tuple[str, str, Dict[str, Any]]] = []
    prior_round_audits = 0
    current_message_id = str(message.get("id") or "")
    message_created_at = _parse_utc_z(str(message.get("created_at_utc") or ""))
    now = _aware_utc(now_utc)
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
        if audit.get("message_id") == current_message_id:
            continue
        if not _audit_thread_matches(message, audit):
            continue
        audit_type = str(audit.get("message_type") or "")
        if (
            audit_type in REVIEW_CONTINUATION_GRANTABLE_TYPES
            and _audit_consumed_round(audit)
        ):
            prior_round_audits += 1

        result = audit.get("result")
        outcome = result.get("human_outcome") if isinstance(result, dict) else None
        grant = outcome.get("grant") if isinstance(outcome, dict) else None
        if not isinstance(outcome, dict) or outcome.get("recorded") is not True:
            continue
        if not isinstance(grant, dict):
            continue
        if str(grant.get("decision") or "") not in {"approved", "modified", "denied"}:
            continue
        decided_at = str(outcome.get("decided_at_utc") or "")
        try:
            decision_time = _parse_utc_z(decided_at)
        except ValueError:
            continue
        is_denial = str(grant.get("decision")) == "denied" or str(
            outcome.get("decision") or ""
        ) not in {"approved", "modified"}
        if is_denial:
            if decision_time <= now:
                denials.append((decided_at, audit_path.name, audit))
        elif decision_time <= message_created_at:
            approvals.append((decided_at, audit_path.name, audit))

    governing = sorted(approvals)[-1] if approvals else None
    latest_denial = sorted(denials)[-1] if denials else None
    if latest_denial is not None and (
        governing is None or latest_denial[0] >= governing[0]
    ):
        _decided_at, audit_name, audit = latest_denial
        return {
            "source_audit": audit_name,
            "source_message_id": audit.get("message_id"),
            "state": "revoked",
            "scope": None,
        }, prior_round_audits
    if governing is None:
        return None, prior_round_audits

    _decided_at, audit_name, audit = governing
    outcome = audit["result"]["human_outcome"]
    grant = outcome["grant"]
    source = {
        "source_audit": audit_name,
        "source_message_id": audit.get("message_id"),
    }
    granted_scope = grant.get("granted_scope")
    review_block = (
        granted_scope.get("review_loop") if isinstance(granted_scope, dict) else None
    )
    if review_block is None:
        # A standing task-continuation grant with no review_loop block
        # carries no review authority — the explicit-confirmation default
        # applies, it is not a drift.
        return {**source, "state": "absent", "scope": None}, prior_round_audits
    scope, error = normalize_review_loop_scope(review_block)
    if error:
        return {**source, "state": "invalid", "scope": None}, prior_round_audits
    return {**source, "state": "accepted", "scope": scope}, prior_round_audits


# Maps the review_continuation block decision to its pinned pause reason.
_REVIEW_PAUSE_CODES = {
    "confirmation_required": "review_continuation_confirmation_required",
    "context_only": "review_continuation_context_only",
    "ignored_disabled": "review_continuation_ignored_disabled",
    "missing_approval": "review_continuation_missing_approval",
    "revoked": "review_continuation_revoked",
    "invalid": "review_loop_invalid",
    "scope_exceeded": "review_continuation_scope_exceeded",
    "round_exceeded": "review_continuation_round_exceeded",
    "expired": "review_continuation_expired",
}


def evaluate_review_continuation(
    message: Dict[str, Any],
    continuation_enabled: bool,
    audit_dir: Optional[Path] = None,
    receiver: str = "codex",
    actuals: Optional[Dict[str, Any]] = None,
    now_utc: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Evaluate a review-lifecycle message against standing review grants.

    The verdict authorizes *running* one reviewer round, never its outcome:
    the reviewer still fetches and validates the live PR head, runs the
    quality gate, and independently chooses feedback or LGTM. Check order
    is pinned (type → repo → PR → declared context → side effects → round →
    wall clock) with early-out on the first failure, mirroring the task
    gates. Lexical hard-stop scanning deliberately does not run here: a
    granted reviewer invocation executes a pinned workflow whose side
    effects are bounded by ``permitted_side_effects``, and review bodies
    quote diffs and commands by design.
    """
    msg_type = str(message.get("type") or "")
    context = _extract_review_context(message)
    head_check, head_mismatch = _review_head_check(
        context["declared_head"], actuals
    )
    block: Dict[str, Any] = {
        "enabled": continuation_enabled,
        "kind": "approved_thread_continuation",
        "surface": "review_loop",
        "decision": "confirmation_required",
        "grant_found": False,
        "requested": {
            "message_type": msg_type,
            "repository": context["repository"],
            "pr_number": context["pr_number"],
            "round": context["round"],
            "side_effects": context["side_effects"],
            "declared_head": context["declared_head"],
        },
        "scope": None,
        "effective_round": None,
        "exceeded_fields": [],
        "head_check": head_check,
        "source_audit": None,
        "source_message_id": None,
    }
    if head_mismatch:
        block["head_mismatch"] = True

    if msg_type in {"review_feedback", "review_lgtm"}:
        # Reviewer-output types carry results, never work to start.
        block["decision"] = "context_only"
        return block

    if not continuation_enabled:
        block["decision"] = "ignored_disabled"
        return block

    has_thread = bool(
        message.get("parent_message_id") or message.get("conversation_id")
    )
    if not has_thread:
        return block

    prior, prior_round_audits = _prior_review_grant(
        message, audit_dir, receiver, now_utc=now_utc
    )
    if prior is None:
        if context["grant_claim_present"]:
            # A sender-declared grant claim is a request, never proof.
            block["decision"] = "missing_approval"
        return block

    block["source_audit"] = prior["source_audit"]
    block["source_message_id"] = prior["source_message_id"]
    if prior["state"] == "revoked":
        block["decision"] = "revoked"
        return block
    if prior["state"] == "absent":
        if context["grant_claim_present"]:
            block["decision"] = "missing_approval"
        return block
    if prior["state"] == "invalid":
        block["decision"] = "invalid"
        return block

    scope = prior["scope"]
    block["grant_found"] = True
    block["scope"] = scope

    if msg_type not in scope["allowed_types"]:
        # review_addressed stays context-only unless the grant explicitly
        # lists it (the manual-continuation shape); an unlisted
        # review_request is outside the granted scope.
        block["decision"] = (
            "context_only" if msg_type == "review_addressed" else "scope_exceeded"
        )
        if msg_type != "review_addressed":
            block["exceeded_fields"].append("allowed_types")
        return block

    exceeded: List[str] = []
    if context["repository"] != scope["repository"]:
        exceeded.append("repository")
    if context["pr_number"] != scope["pr_number"]:
        exceeded.append("pr_number")
    exceeded.extend(f"declared.{field}" for field in context["errors"])
    permitted = scope["permitted_side_effects"]
    for effect in context["side_effects"]:
        if permitted.get(effect) is not True:
            exceeded.append(f"permitted_side_effects.{effect}")
    if exceeded:
        block["decision"] = "scope_exceeded"
        block["exceeded_fields"] = exceeded
        return block

    declared_round = context["round"] or 1
    effective_round = max(declared_round, 1 + prior_round_audits)
    block["effective_round"] = effective_round
    if effective_round > scope["max_round"]:
        block["decision"] = "round_exceeded"
        return block

    # The grant must be live when the work would RUN, not merely when the
    # sender stamped the request: created_at_utc is sender-controlled, so
    # expiry is checked against evaluation time as well. Either clock past
    # the bound expires the grant.
    message_created_at = _parse_utc_z(str(message.get("created_at_utc") or ""))
    now = _aware_utc(now_utc)
    expires_at = _parse_utc_z(scope["expires_at_utc"])
    if message_created_at > expires_at or now > expires_at:
        block["decision"] = "expired"
        return block

    block["decision"] = "accepted"
    return block


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
            and (
                envelope["creates_or_updates_pr"]
                or envelope["comments_on_github"]
                or envelope["files_issues"]
            )
        )
        # merges_pr never rides the artifact class: merge authority always
        # passes a human at least once (admission pause), unless a prior
        # human-approved continuation grant covers it explicitly.
        if "merges_pr" in uncovered_fields:
            reasons.append("merges_pr_pause")
        if not is_private_pr_artifact and (
            (envelope["external_side_effects"] and not grant_covers_side_effect)
            or uncovered_fields
        ):
            reasons.append("external_side_effects_not_pr_artifact")
        return reasons

    if envelope["external_side_effects"] and not grant_covers_side_effect:
        reasons.append("external_side_effects_pause")
    for key in uncovered_fields:
        reasons.append(f"{key}_pause")
    return reasons


def _profile_declaration_details(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Admission-time declaration errors with their cause.

    Each entry names the contradicted field, what it declared, and the
    declared fields it conflicts with — so a ``declaration_error`` pause
    records which axis failed and why, not just that one did.
    """
    details: List[Dict[str, Any]] = []
    artifact_fields = [
        f"task_profile.{key}" for key in COVERABLE_CONTINUATION_FIELDS if envelope[key]
    ]
    if artifact_fields and not envelope["external_side_effects"]:
        details.append({
            "field": "task_profile.external_side_effects",
            "declared": False,
            "conflicts_with": artifact_fields,
        })
    if envelope["sends_oacp_reply_only"] and artifact_fields:
        details.append({
            "field": "task_profile.sends_oacp_reply_only",
            "declared": True,
            "conflicts_with": artifact_fields,
        })
    return details


def _profile_declaration_errors(envelope: Dict[str, Any]) -> List[str]:
    return [entry["field"] for entry in _profile_declaration_details(envelope)]


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


def _declared_risk_reasons(envelope: Dict[str, Any]) -> List[str]:
    return [code for key, code in DECLARED_RISK_REASONS if envelope[key]]


def not_evaluated_admission_ledger() -> Dict[str, Any]:
    """The ledger shape for a pause taken before a scope envelope exists.

    Nothing envelope-derived can be evaluated without an envelope, and the
    record says so explicitly: ``evaluated: false`` with every axis null
    is distinguishable from an evaluated ledger whose axes all passed.
    """
    ledger: Dict[str, Any] = {"evaluated": False}
    for axis in ADMISSION_AXES:
        ledger[axis] = None
    ledger["declaration_errors"] = None
    return ledger


def admission_ledger(
    envelope: Dict[str, Any],
    policy: Dict[str, Any],
    grant_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Evaluate every envelope-derived admission axis, independent of order.

    Each axis lists the pinned reason codes that held (empty = passed).
    The ledger is pure evidence: the verdict still comes from the first
    failing axis in evaluation order, and ``reason_codes`` keep that
    pinned first-failure shape; everything else the ledger holds surfaces
    through ``co_occurring_reason_codes``.
    """
    thresholds = policy["thresholds"]
    grant_codes: List[str] = []
    if grant_result.get("decision") not in {"accepted", "not_present"}:
        grant_codes.extend(grant_result.get("reason_codes") or [])
    if continuation_scope_breaches(envelope, grant_result):
        grant_codes.append("continuation_grant_scope_exceeded")
    declaration_errors = _profile_declaration_details(envelope)
    return {
        "evaluated": True,
        "thresholds": _threshold_reasons(envelope, thresholds),
        "declared_risk": _declared_risk_reasons(envelope),
        "declaration": ["declaration_error"] if declaration_errors else [],
        "side_effects": _side_effect_reasons(
            envelope,
            grant_result,
            str(thresholds["external_side_effects"]),
            policy["private_repo_allowlist"],
        ),
        "continuation_grant": grant_codes,
        "declaration_errors": declaration_errors,
    }


def admission_ledger_codes(ledger: Dict[str, Any]) -> List[str]:
    """Every reason code an evaluated ledger holds, deduplicated, axis order."""
    codes: List[str] = []
    for axis in ADMISSION_AXES:
        for code in ledger.get(axis) or []:
            if code not in codes:
                codes.append(code)
    return codes


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


def _actual_utc_text(actuals: Dict[str, Any], key: str) -> Optional[str]:
    value = str(actuals.get(key) or "")
    if not value:
        return None
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"actuals.{key} must use YYYY-MM-DDTHH:MM:SSZ") from exc
    return value


def _breach_basis(actuals: Dict[str, Any]) -> Optional[str]:
    value = actuals.get("breach_basis")
    if value is None:
        return None
    if value not in BREACH_BASES:
        choices = " or ".join(BREACH_BASES)
        raise ValueError(f"actuals.breach_basis must be {choices}")
    return str(value)


def _breach_sub_basis(actuals: Dict[str, Any]) -> Optional[str]:
    value = actuals.get("breach_sub_basis")
    if value is None:
        return None
    if value not in BREACH_SUB_BASES:
        choices = " or ".join(BREACH_SUB_BASES)
        raise ValueError(f"actuals.breach_sub_basis must be {choices}")
    return str(value)


def _declared_intent_fields(actuals: Dict[str, Any]) -> List[str]:
    """Validated prospective-breach input.

    A declaration correction caught before anything materialized cannot be
    expressed through realized effects — marking a side effect true would
    assert an outward action that never happened. The receiver instead
    names the declared-profile fields the correction invalidated
    (``task_profile.<risk capability field>``); each drives the checkpoint
    breach directly while every realized effect stays false. The
    vocabulary is the monotone risky capability booleans only —
    ``sends_oacp_reply_only`` is reverse-polarity (restrictive), so a
    false-to-true flip on it cannot represent a risky correction and is
    refused.
    """
    value = actuals.get("declared_intent_fields")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(
            "actuals.declared_intent_fields must be a list of "
            "task_profile field paths"
        )
    valid_keys = LEGACY_PROFILE_BOOL_FIELDS + COVERABLE_CONTINUATION_FIELDS
    fields: List[str] = []
    for item in value:
        prefix, _, key = item.partition(".")
        if prefix != "task_profile" or key not in valid_keys:
            raise ValueError(
                f"actuals.declared_intent_fields entry {item!r} is not a "
                "task_profile risk-capability field path"
            )
        if item not in fields:
            fields.append(item)
    return fields


def normalize_reauthorization_scope(
    scope: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normalize a re-authorization answer's granted scope.

    Unlike a continuation-grant scope, both numerics are optional: an
    answer may extend only one budget, only grant boundary actions, or
    carry no scope at all (a scope-less approval clears exactly the pause
    it answers). Present numerics must be non-negative ints; boundary
    actions use the coverable-field vocabulary.
    """
    if scope is None:
        return None, None
    if not isinstance(scope, dict):
        return None, "reauthorization scope must be a mapping"
    valid_keys = {"max_actual_minutes", "max_actual_files_touched"} | set(
        COVERABLE_CONTINUATION_FIELDS
    )
    unknown = sorted(set(scope) - valid_keys)
    if unknown:
        return None, f"reauthorization scope has unknown key(s): {', '.join(unknown)}"
    normalized: Dict[str, Any] = {}
    for key in ("max_actual_minutes", "max_actual_files_touched"):
        if key not in scope:
            continue
        value = scope[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, f"reauthorization scope {key} must be a non-negative integer"
        normalized[key] = value
    for key in COVERABLE_CONTINUATION_FIELDS:
        value = scope.get(key, False)
        if not isinstance(value, bool):
            return None, f"reauthorization scope {key} must be boolean"
        normalized[key] = value
    return normalized, None


def _parse_reauthorization(
    actuals: Dict[str, Any],
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Validated `actuals.reauthorization` input.

    The receiver presents the channel answers it has observed for the
    checkpoint being re-evaluated: `receiver_human` from its own audit
    record's recorded outcome, `sender_reply` from a signature-verified
    sender message threaded to the checkpoint notification, `gh_comment`
    from the related PR/issue. Verification of each channel's provenance
    (signatures, threading, recording) happens at the receiver before the
    answer may be presented here — this input is the arbitration surface,
    not the trust boundary.
    """
    raw = actuals.get("reauthorization")
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ValueError("actuals.reauthorization must be a non-empty mapping")
    unknown = sorted(set(raw) - set(REAUTH_CHANNEL_PRECEDENCE))
    if unknown:
        raise ValueError(
            "actuals.reauthorization has unknown channel(s): " + ", ".join(unknown)
        )
    parsed: Dict[str, Dict[str, Any]] = {}
    for channel in REAUTH_CHANNEL_PRECEDENCE:
        if channel not in raw:
            continue
        answer = raw[channel]
        if not isinstance(answer, dict):
            raise ValueError(f"actuals.reauthorization.{channel} must be a mapping")
        decision = answer.get("decision")
        if decision not in REAUTH_DECISIONS:
            choices = ", ".join(sorted(REAUTH_DECISIONS))
            raise ValueError(
                f"actuals.reauthorization.{channel}.decision must be one of: {choices}"
            )
        entry: Dict[str, Any] = {"decision": str(decision)}
        decided_at = str(answer.get("decided_at_utc") or "")
        if channel in REAUTH_GOVERNING_CHANNELS:
            if not decided_at:
                raise ValueError(
                    f"actuals.reauthorization.{channel}.decided_at_utc is required"
                )
        if decided_at:
            try:
                dt.datetime.strptime(decided_at, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError as exc:
                raise ValueError(
                    f"actuals.reauthorization.{channel}.decided_at_utc "
                    "must use YYYY-MM-DDTHH:MM:SSZ"
                ) from exc
        entry["decided_at_utc"] = decided_at or None
        if channel == "receiver_human":
            entry["actor"] = str(answer.get("actor") or "") or None
        if channel == "sender_reply":
            source = str(answer.get("source_message_id") or "")
            if not source:
                raise ValueError(
                    "actuals.reauthorization.sender_reply.source_message_id "
                    "is required"
                )
            entry["source_message_id"] = source
        if channel == "gh_comment":
            entry["author"] = str(answer.get("author") or "") or None
            if answer.get("scope") is not None:
                raise ValueError(
                    "actuals.reauthorization.gh_comment cannot carry scope — "
                    "GH comments are advisory, never authoritative"
                )
        scope, scope_error = normalize_reauthorization_scope(answer.get("scope"))
        if scope_error:
            raise ValueError(f"actuals.reauthorization.{channel}: {scope_error}")
        if decision == "modified" and scope is None:
            raise ValueError(
                f"actuals.reauthorization.{channel}: decision modified "
                "requires an explicit scope"
            )
        entry["scope"] = scope
        parsed[channel] = entry
    return parsed


def _reauth_numeric_budget(
    channel: str,
    scope: Optional[Dict[str, Any]],
    scope_key: str,
    policy: Optional[Dict[str, Any]],
    threshold_key: str,
) -> Optional[int]:
    """Effective numeric budget an answer's scope grants on one channel.

    The receiver-side human is unbounded. Sender authority is bounded by
    the receiver's own admission policy — a sender re-authorization can
    never authorize more than the receiver's thresholds would have
    auto-accepted at admission (the party whose under-declaration caused
    the breach cannot self-serve unlimited scope). Without a resolvable
    policy the sender channel extends nothing (fail closed).
    """
    granted = scope.get(scope_key) if isinstance(scope, dict) else None
    if channel == "receiver_human":
        return granted if isinstance(granted, int) else None
    if channel != "sender_reply" or not isinstance(granted, int):
        return None
    if not isinstance(policy, dict):
        return None
    cap = policy.get("thresholds", {}).get(threshold_key)
    if not isinstance(cap, int):
        return None
    return min(granted, cap)


def _sender_may_grant_field(
    key: str,
    policy: Optional[Dict[str, Any]],
    envelope: Optional[Dict[str, Any]],
) -> bool:
    """Whether the sender channel may grant one boundary action.

    The bound is the receiver's own admission predicate, reused verbatim:
    the grant is honored only when an envelope declaring this capability
    would itself auto-accept under the receiver's external-side-effect
    policy (for `allow_pr_artifacts`: a private target on the receiver's
    allowlist carrying an artifact-class anchor — a standalone
    `commits_changes` or an unlisted/public target stays paused).
    `merges_pr` is categorically excluded on top of that, whatever the
    policy: merge authority always passes the receiver-side human. So is
    every key outside the coverable boundary-action vocabulary — the
    legacy risk fields (destructive ops, auth/config/secrets, dependency,
    public visibility) are receiver-side authority only, and the
    side-effect admission predicate below never evaluates them.
    """
    if key not in COVERABLE_CONTINUATION_FIELDS:
        return False
    if key in SENDER_UNGRANTABLE_REAUTH_FIELDS:
        return False
    if not isinstance(policy, dict) or not isinstance(envelope, dict):
        return False
    external_policy = str(policy.get("thresholds", {}).get("external_side_effects"))
    allowlist = policy.get("private_repo_allowlist") or []
    hypothetical = dict(envelope)
    hypothetical[key] = True
    hypothetical["external_side_effects"] = True
    return not _side_effect_reasons(
        hypothetical,
        {"present": False},
        external_policy,
        allowlist,
    )


def _reauth_boolean_grants(
    channel: str,
    scope: Optional[Dict[str, Any]],
    policy: Optional[Dict[str, Any]],
    envelope: Optional[Dict[str, Any]],
) -> FrozenSet[str]:
    """Boundary actions an answer's scope grants on one channel."""
    if not isinstance(scope, dict):
        return frozenset()
    granted = {key for key in COVERABLE_CONTINUATION_FIELDS if scope.get(key) is True}
    if channel == "receiver_human":
        return frozenset(granted)
    if channel != "sender_reply":
        return frozenset()
    return frozenset(
        key for key in granted if _sender_may_grant_field(key, policy, envelope)
    )


def _reauth_effective_scope(
    channel: str,
    scope: Optional[Dict[str, Any]],
    policy: Optional[Dict[str, Any]],
    envelope: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """The policy-capped scope an answer actually grants.

    This — never the raw request — is the durable value later checkpoints
    and envelope recompiles consume: sender numerics are capped at the
    receiver's thresholds and sender booleans filtered to the admission
    predicate, while the receiver-side human's scope passes through
    unchanged. Returns None for a scope-less answer (nothing durable).
    """
    if scope is None:
        return None
    effective: Dict[str, Any] = {}
    for scope_key, threshold_key in (
        ("max_actual_minutes", "max_estimated_minutes"),
        ("max_actual_files_touched", "max_expected_files_touched"),
    ):
        if scope_key not in scope:
            continue
        budget = _reauth_numeric_budget(
            channel, scope, scope_key, policy, threshold_key
        )
        if budget is not None:
            effective[scope_key] = budget
    grants = _reauth_boolean_grants(channel, scope, policy, envelope)
    for key in COVERABLE_CONTINUATION_FIELDS:
        effective[key] = key in grants
    return effective


def _default_reauthorization_block() -> Dict[str, Any]:
    return {
        "presented": False,
        "channel": None,
        "decision": None,
        "decided_at_utc": None,
        "actor": None,
        "source_message_id": None,
        "requested_scope": None,
        "scope": None,
        "disposition": None,
        "cleared_paused_at_utc": None,
        "advisory": [],
    }


def _arbitrate_reauthorization(
    reauth_input: Dict[str, Dict[str, Any]],
    block: Dict[str, Any],
    breached_fields: List[str],
    actual_minutes: int,
    actual_files: int,
    paused_at: str,
    policy: Optional[Dict[str, Any]],
    envelope: Optional[Dict[str, Any]],
) -> None:
    """Arbitrate channel answers against the current pause, in place.

    Precedence: receiver_human > sender_reply > gh_comment, by rank and
    never by arrival order. The governing answer is the highest-ranked
    authoritative-capable channel bearing one; every lower-ranked answer
    is recorded as advisory. Coverage per breached field: boundary-action
    grants (scope booleans) are durable for the task — no freshness
    requirement; numeric budgets clear a pause only up to the granted
    scope. A fresh scope-less answer clears exactly the current pause —
    numeric and boundary fields alike, within the answering channel's
    grant bounds — without creating anything durable. A spent answer
    presented against a newer breach it does not cover is stale (the
    pinned grant-reuse rejection).
    """
    governing = next(
        (c for c in REAUTH_GOVERNING_CHANNELS if c in reauth_input), None
    )
    for channel in REAUTH_CHANNEL_PRECEDENCE:
        if channel not in reauth_input or channel == governing:
            continue
        reason = "never_authoritative"
        if channel in REAUTH_GOVERNING_CHANNELS:
            reason = "overridden_by_receiver_human"
        block["advisory"].append({
            "channel": channel,
            "decision": reauth_input[channel]["decision"],
            "reason": reason,
        })
    if governing is None:
        block["disposition"] = "advisory_only"
        return

    answer = reauth_input[governing]
    scope = answer.get("scope")
    block["channel"] = governing
    block["decision"] = answer["decision"]
    block["decided_at_utc"] = answer["decided_at_utc"]
    block["actor"] = answer.get("actor")
    block["source_message_id"] = answer.get("source_message_id")
    # The requested scope is provenance; the effective (policy-capped)
    # scope is the durable value later checkpoints consume. They diverge
    # exactly when a sender asked past the receiver's own policy.
    block["requested_scope"] = scope
    block["scope"] = _reauth_effective_scope(governing, scope, policy, envelope)

    if answer["decision"] == "declined":
        block["disposition"] = "declined"
        return

    decided = dt.datetime.strptime(answer["decided_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    paused = dt.datetime.strptime(paused_at, "%Y-%m-%dT%H:%M:%SZ")
    fresh = decided >= paused
    scope_less = scope is None
    boolean_grants = _reauth_boolean_grants(governing, scope, policy, envelope)
    files_cap = (
        policy.get("thresholds", {}).get("max_expected_files_touched")
        if isinstance(policy, dict)
        else None
    )
    minutes_cap = (
        policy.get("thresholds", {}).get("max_estimated_minutes")
        if isinstance(policy, dict)
        else None
    )

    def numeric_covered(actual: int, scope_key: str, threshold_key: str) -> bool:
        budget = _reauth_numeric_budget(
            governing, scope, scope_key, policy, threshold_key
        )
        if budget is not None:
            return actual <= budget
        if not (scope_less and fresh):
            return False
        # A fresh scope-less approval clears exactly the extent recorded at
        # the pause it answers — unbounded for the receiver-side human,
        # within the receiver's admission caps for the sender.
        if governing == "receiver_human":
            return True
        cap = minutes_cap if threshold_key == "max_estimated_minutes" else files_cap
        return isinstance(cap, int) and actual <= cap

    def boundary_covered(key: str) -> bool:
        if key in boolean_grants:
            return True
        # A fresh scope-less approval also clears the boundary fields of
        # the pause it answers, within the channel's grant bounds; nothing
        # durable is recorded (scope stays None).
        if not (scope_less and fresh):
            return False
        if governing == "receiver_human":
            return True
        return _sender_may_grant_field(key, policy, envelope)

    uncovered: List[str] = []
    for field in breached_fields:
        if field == "actual_minutes":
            covered = numeric_covered(
                actual_minutes, "max_actual_minutes", "max_estimated_minutes"
            )
        elif field == "actual_files_touched":
            covered = numeric_covered(
                actual_files, "max_actual_files_touched", "max_expected_files_touched"
            )
        else:
            covered = boundary_covered(field.rpartition(".")[2])
        if not covered:
            uncovered.append(field)

    if uncovered:
        block["disposition"] = "insufficient" if fresh else "stale"
        return
    block["disposition"] = "resumed"
    block["cleared_paused_at_utc"] = answer["decided_at_utc"]


def evaluate_threshold_checkpoint(
    envelope: Optional[Dict[str, Any]],
    grant_result: Dict[str, Any],
    actuals: Optional[Dict[str, Any]],
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    checkpoint: Dict[str, Any] = {
        "evaluated": False,
        "actual_minutes": None,
        "actual_files_touched": None,
        "side_effects_actual": {},
        "breached": False,
        "breached_fields": [],
        "declaration_errors": [],
        "breach_basis": None,
        "breach_sub_basis": None,
        "paused_at_utc": None,
        "action": "not_evaluated",
        "predicted_risk_materialized": False,
        "completed_at_utc": None,
        "reauthorization": _default_reauthorization_block(),
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

    intent_fields = _declared_intent_fields(actuals)

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
    if intent_fields:
        # The prospective shape is all-or-nothing: a checkpoint labeled
        # declared_intent must not silently contain realized breach
        # sources, and a "correction" for a capability the envelope
        # already declares corrects nothing. Mixed inputs are rejected
        # before an ambiguous audit record can be written — record the
        # realized breach on its own, then re-evaluate the correction.
        if breached_fields:
            raise ValueError(
                "actuals.declared_intent_fields cannot combine with "
                f"realized breach sources ({', '.join(breached_fields)})"
            )
        realized_true = sorted(
            key for key, value in side_effects.items() if value
        )
        if realized_true:
            raise ValueError(
                "actuals.declared_intent_fields requires all-false "
                f"side_effects_actual (true: {', '.join(realized_true)})"
            )
        for field in intent_fields:
            key = field.partition(".")[2]
            already_authorized = envelope.get(key) is True or (
                isinstance(grant_scope, dict) and grant_scope.get(key) is True
            )
            if already_authorized:
                raise ValueError(
                    f"actuals.declared_intent_fields entry {field!r} is "
                    "already declared true in the envelope or covered by "
                    "an accepted continuation grant — nothing to correct "
                    "prospectively"
                )
        # Prospective corrections breach on the declared field itself —
        # the realized effects stay all-false (nothing materialized).
        for field in intent_fields:
            breached_fields.append(field)
            declaration_errors.append(field)

    breached = bool(breached_fields)

    action = "within_declared_envelope"
    if breached:
        action = "paused_for_reauthorization"
    elif isinstance(grant_scope, dict):
        action = "continued_with_grant"

    # The pause moment and the breach basis are only meaningful on a breach.
    # `paused_at_utc` is what human-decision latency measures from: the
    # receiver passes the moment the checkpoint actually fired (e.g. the
    # sender-notification timestamp) and the evaluator stamps "now" only as
    # the fallback. `breach_basis` follows the breach source: realized work
    # by default, `declared_intent` exactly when declared_intent_fields
    # drive the breach — a mismatched explicit basis is rejected rather
    # than recorded (the two shapes must stay distinguishable).
    explicit_basis = _breach_basis(actuals)
    if intent_fields and explicit_basis == "realized":
        raise ValueError(
            "actuals.breach_basis realized is inconsistent with "
            "declared_intent_fields"
        )
    if not intent_fields and explicit_basis == "declared_intent":
        raise ValueError(
            "actuals.breach_basis declared_intent requires "
            "declared_intent_fields"
        )
    paused_at = None
    basis = None
    if breached:
        paused_at = _actual_utc_text(actuals, "paused_at_utc") or utc_now_iso()
        basis = "declared_intent" if intent_fields else "realized"

    # A sub-basis refines a realized time-axis breach only: `waiting_on_peer`
    # says the clock overran while the receiver waited on a peer reply, not
    # while working. It is rejected anywhere it cannot mean that — on an
    # unbreached checkpoint, a prospective (declared_intent) breach, or a
    # breach that does not include the time axis — so the ledger can trust
    # the split instead of recording a stray label.
    sub_basis = _breach_sub_basis(actuals)
    if sub_basis is not None:
        if not breached:
            raise ValueError(
                "actuals.breach_sub_basis requires a breached checkpoint"
            )
        if basis != "realized":
            raise ValueError(
                "actuals.breach_sub_basis requires breach_basis realized"
            )
        if "actual_minutes" not in breached_fields:
            raise ValueError(
                f"actuals.breach_sub_basis {sub_basis} requires a time-axis "
                "breach (actual_minutes in breached_fields)"
            )

    # Re-authorization arbitration: channel answers presented by the
    # receiver are arbitrated against THIS pause. A resumed disposition is
    # the only one that clears the breach; everything else leaves the
    # checkpoint paused with the answers recorded.
    reauth_input = _parse_reauthorization(actuals)
    reauth_block = _default_reauthorization_block()
    reauth_block["presented"] = reauth_input is not None
    if reauth_input is None:
        if breached:
            reauth_block["disposition"] = "unanswered"
    elif not breached:
        reauth_block["disposition"] = "not_required"
    else:
        _arbitrate_reauthorization(
            reauth_input,
            reauth_block,
            breached_fields,
            actual_minutes,
            actual_files,
            paused_at,
            policy,
            envelope,
        )
        if reauth_block["disposition"] == "resumed":
            action = "resumed_after_reauthorization"
        elif reauth_block["disposition"] == "declined":
            action = "reauthorization_declined"

    # A declared_intent breach is by definition caught BEFORE the risk
    # materialized — its materialization metric is pinned false and an
    # explicit true is rejected as self-contradictory. Realized breaches
    # keep the breach-derived default.
    predicted = actuals.get("predicted_risk_materialized")
    if intent_fields:
        if predicted is True:
            raise ValueError(
                "actuals.predicted_risk_materialized cannot be true on a "
                "declared_intent checkpoint — the correction was caught "
                "before the risk materialized"
            )
        predicted_value = False
    else:
        predicted_value = predicted if isinstance(predicted, bool) else breached

    checkpoint.update({
        "evaluated": True,
        "actual_minutes": actual_minutes,
        "actual_files_touched": actual_files,
        "side_effects_actual": side_effects,
        "breached": breached,
        "breached_fields": breached_fields,
        "declaration_errors": declaration_errors,
        "breach_basis": basis,
        "breach_sub_basis": sub_basis,
        "paused_at_utc": paused_at,
        "action": action,
        "predicted_risk_materialized": predicted_value,
        "completed_at_utc": _actual_utc_text(actuals, "completed_at_utc"),
        "reauthorization": reauth_block,
    })
    return checkpoint


def _base_result(
    final_state: str,
    completion_kind: str,
    checkpoint: Dict[str, Any],
) -> Dict[str, Any]:
    if final_state not in FINAL_STATES:
        raise ValueError(f"invalid final_state: {final_state}")
    if completion_kind not in PINNED_COMPLETION_KINDS:
        raise ValueError(f"unpinned completion_kind: {completion_kind}")
    return {
        "final_state": final_state,
        "completion_kind": completion_kind,
        "actual_minutes": checkpoint.get("actual_minutes"),
        "actual_files_touched": checkpoint.get("actual_files_touched"),
        "work_started_at_utc": None,
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
    policy_auth: Optional[Dict[str, Any]] = None,
    message_raw: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Evaluate a message/config pair and return a canonical decision dict.

    ``policy_auth`` is the policy-file authorization block produced by
    `policy_signing.verify_policy_data` on the receiver config snapshot.
    When supplied it is recorded into the decision, and an ``invalid``
    status fails closed before any gate consumes the (untrusted) config —
    the record then carries ``policy_auth_invalid``, distinguishable from
    both a missing policy and a merely malformed one. ``message_raw`` is
    the verified message snapshot; when supplied, the recorded
    ``message_sha256`` names those exact bytes.
    """
    msg_hash = message_sha256(message, message_path, message_raw)
    policy_hash = canonical_policy_sha256(config)
    body = str(message.get("body") or "")
    msg_type = str(message.get("type") or "")
    logged_notes: List[Dict[str, str]] = []
    profile_snapshot: Optional[Dict[str, Any]] = None
    envelope_source: Optional[str] = None
    # Resolved below; pre-bound so early pauses (malformed config) can
    # evaluate checkpoints with sender authority failing closed.
    policy: Optional[Dict[str, Any]] = None
    # The structured admission ledger. Stays `evaluated: false` on every
    # pause taken before a scope envelope exists — there is nothing to
    # evaluate against, and the record says so instead of reading as
    # "all passed".
    admission_axes: Dict[str, Any] = not_evaluated_admission_ledger()

    def finish(decision: Dict[str, Any]) -> Dict[str, Any]:
        reason_codes = list(decision.get("reason_codes") or [])
        co_occurring = list(decision.get("co_occurring_reason_codes") or [])
        unknown_codes = sorted(
            (set(reason_codes) | set(co_occurring)) - PINNED_REASON_CODES
        )
        if unknown_codes:
            raise ValueError(f"unpinned autonomy reason code(s): {', '.join(unknown_codes)}")
        decision["message_sha256"] = msg_hash
        decision["policy_sha256"] = policy_hash
        decision["schema_version"] = AUTONOMY_AUDIT_SCHEMA_VERSION
        decision["spec_version"] = SPEC_VERSION
        decision["evaluator"] = evaluator_provenance()
        if policy_auth is not None:
            # The authorized-policy identity: together with policy_sha256
            # this commits the record to WHO authorized the policy, not just
            # which bytes ran.
            decision["policy_auth"] = {
                key: policy_auth.get(key)
                for key in ("status", "signer_agent", "signer_kid", "reason")
            }
        decision["receiver"] = receiver
        decision["sender"] = message.get("from")
        decision["message_id"] = message.get("id")
        decision["message_type"] = message.get("type")
        decision["conversation_id"] = message.get("conversation_id")
        decision["parent_message_id"] = message.get("parent_message_id")
        decision.setdefault("task_profile", profile_snapshot)
        # Every envelope names where it came from; a null envelope (only
        # ever legitimate on a pause taken before construction) names
        # nothing.
        decision["scope_envelope_source"] = (
            envelope_source if decision.get("scope_envelope") is not None else None
        )
        decision.setdefault(
            "breached",
            reason_codes if decision.get("decision") == "paused" else [],
        )
        decision.setdefault("co_occurring_reason_codes", [])
        decision["admission_axes"] = admission_axes
        decision["matched_patterns"] = _collect_lexical_provenance(
            body,
            profile_snapshot,
            decision.get("scope_envelope"),
            policy,
            msg_type,
        )
        return decision

    def paused(
        mode: str,
        reason_codes: List[str],
        completion_kind: str = "admission_paused",
        *,
        envelope: Optional[Dict[str, Any]] = None,
        grant_result: Optional[Dict[str, Any]] = None,
        matched_pattern: Optional[str] = None,
        validation_errors: Optional[List[str]] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        breached: Optional[List[str]] = None,
        co_occurring: Optional[List[str]] = None,
        review_continuation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        grant = grant_result or {"present": False, "enabled": False}
        resolved_checkpoint = checkpoint or evaluate_threshold_checkpoint(
            envelope,
            grant,
            actuals,
            policy=policy,
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
        if review_continuation is not None:
            decision["review_continuation"] = review_continuation
        if matched_pattern is not None:
            decision["matched_pattern"] = matched_pattern
        if validation_errors is not None:
            decision["message_validation_errors"] = validation_errors
        if breached is not None:
            decision["breached"] = breached
        if co_occurring:
            # Reasons that also held but did not drive the verdict: an
            # early-out must not make "not evaluated" indistinguishable
            # from "passed" in the record (threshold analytics read these).
            decision["co_occurring_reason_codes"] = sorted(
                set(co_occurring) - set(reason_codes)
            )
        return finish(decision)

    if policy_auth is not None and policy_auth.get("status") == "invalid":
        # Tampered policy fails closed before anything reads it — including
        # its own autonomy block and verify mode.
        return paused("always_pause", ["policy_auth_invalid"], "config_malformed")

    try:
        mode, policy = receiver_policy(config)
    except AutonomyConfigError:
        return paused("always_pause", ["config_malformed"], "config_malformed")

    if mode == "always_pause":
        return paused(mode, ["mode_always_pause"])

    message_errors = validate_message_dict(message)
    if message_errors:
        return paused(
            mode,
            ["message_invalid"],
            validation_errors=message_errors,
        )

    if message_expired(message, now_utc):
        return paused(mode, ["message_expired"])

    if prior_auto_accept_exists(str(message.get("id") or ""), receiver, audit_dir):
        return paused(mode, ["message_replayed"])

    if msg_type in REVIEW_LIFECYCLE_TYPES:
        # Review-loop lifecycle admission: the four task gates do not run —
        # these messages carry no task profile, and a granted reviewer
        # invocation executes a pinned workflow bounded by the grant's
        # permitted_side_effects, not by sender prose. Auto-continue
        # requires a standing human-approved grant in this sender/thread
        # chain; everything else keeps the explicit-confirmation default.
        review_block = evaluate_review_continuation(
            message,
            bool(policy["continuation_grants_enabled"]),
            audit_dir=audit_dir,
            receiver=receiver,
            actuals=actuals,
            now_utc=now_utc,
        )
        if review_block["decision"] != "accepted":
            return paused(
                mode,
                [_REVIEW_PAUSE_CODES[review_block["decision"]]],
                review_continuation=review_block,
            )
        reason_codes = [
            "message_valid",
            "message_not_expired",
            "message_hash_recorded",
            "review_continuation_accepted",
        ]
        if review_block.get("head_mismatch"):
            # Recorded, never blocking: the live head is authoritative and
            # the reviewer must resolve the declared value against it
            # before any terminal verdict.
            reason_codes.append("review_continuation_head_mismatch")
        review_checkpoint = evaluate_threshold_checkpoint(
            None, {"present": False, "enabled": False}, None, policy=policy
        )
        decision = {
            "decision": "auto_accepted",
            "mode": mode,
            "reason_codes": reason_codes,
            "scope_envelope": None,
            "logged_notes": logged_notes,
            "continuation_grant": {"present": False, "enabled": False},
            "review_continuation": review_block,
            "result": _base_result("done", "auto_accepted", review_checkpoint),
        }
        return finish(decision)

    allow_without_profile = msg_type in policy["allow_without_task_profile"]

    profile, profile_error = extract_task_profile(body)
    profile_snapshot = profile
    if profile_error:
        return paused(mode, [profile_error])

    if profile is None and not allow_without_profile:
        reason = "risk_obvious_no_profile" if obvious_no_profile_risk(body) else "task_profile_missing"
        return paused(mode, [reason])

    envelope: Optional[Dict[str, Any]] = None
    profile_required_reason = "task_profile_present"
    if profile is None:
        # Admission-only exemption: the exempt type skips the authoring
        # requirement, never the bound — the receiver constructs the
        # documented default envelope instead of running unbounded.
        profile_required_reason = "task_profile_not_required"
        logged_notes.extend(side_effect_notes_for_allowed_type(body))
        envelope = default_scope_envelope(message)
        envelope_source = SCOPE_ENVELOPE_SOURCE_DEFAULT
    else:
        try:
            envelope = normalize_scope_envelope(profile)
        except TaskProfileError:
            return paused(mode, ["task_profile_unparsable"])
        envelope_source = SCOPE_ENVELOPE_SOURCE_PROFILE

    gate3_body = _gate3_body(body, logged_notes)

    grant_result: Dict[str, Any] = {"present": False, "enabled": False}
    if envelope is not None and envelope_source == SCOPE_ENVELOPE_SOURCE_PROFILE:
        # Continuation grants are a sender-declared surface; a default
        # envelope declares nothing, so grant interplay is reachable on
        # exempt types only through a voluntary profile (the supported
        # override path). Resolved ahead of Gate 3 because the ledger's
        # side-effect axis is grant-aware.
        grant_result = evaluate_continuation_grant(
            message,
            envelope,
            bool(policy["continuation_grants_enabled"]),
            audit_dir=audit_dir,
            receiver=receiver,
        )

    # Every envelope-derived admission axis is evaluated here, before any
    # Gate-3 early-out, and recorded as the admission ledger: a pause taken
    # for one reason (most often a lexical hard stop) must not leave another
    # axis unrecorded — record silence means "passed", never "not
    # evaluated". The ledger never changes the verdict: `reason_codes` keep
    # their pinned first-failure shape, and every other axis that held
    # lands in `co_occurring_reason_codes`.
    masked_admission_reasons: List[str] = []
    if envelope is not None:
        admission_axes = admission_ledger(envelope, policy, grant_result)
        masked_admission_reasons = admission_ledger_codes(admission_axes)

    # The one carve-out from the always-hard content-sensitivity class: a
    # complete profile declaring reply-only work with every other
    # side-effect flag false. For that shape the category records — every
    # matching term, as an advisory carrying the term — and never pauses.
    # Recorded here, ahead of every Gate-3 early return, so the advisory
    # survives whichever hard stop or axis governs the verdict; the
    # hard-stop branch below stays in place for every other shape.
    content_reply_only = _reply_only_profile_shape(profile, envelope)
    if content_reply_only:
        for label, pattern in CONTENT_SENSITIVITY_PATTERNS:
            if pattern.search(body):
                _record_lexical_note(
                    logged_notes, "lexical_advisory_reply_only", label
                )

    matched = first_match(DESTRUCTIVE_PATTERNS, body)
    if matched:
        return paused(
            mode,
            ["hard_stop_destructive_command"],
            envelope=envelope,
            grant_result=grant_result,
            matched_pattern=matched,
            co_occurring=masked_admission_reasons,
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
        # An explicitly declared merge must reach the granular policy path
        # (merges_pr_pause on first admission; a human-approved continuation
        # grant covering merges_pr can admit the follow-up) — otherwise the
        # documented one-human-pass flow is unreachable whenever the task
        # uses the literal word "merge". Undeclared merge wording stays a
        # lexical hard stop.
        demote_labels: FrozenSet[str] = frozenset()
        if (
            _profile_is_complete(profile)
            and envelope is not None
            and envelope["merges_pr"]
        ):
            demote_labels = frozenset({"merge"})
        matched = _first_effective_match(
            SIDE_EFFECT_VERB_PATTERNS,
            gate3_body,
            logged_notes,
            demote_declared=demote_side_effect,
            demote_labels=demote_labels,
        )
        if matched:
            return paused(
                mode,
                ["hard_stop_external_side_effect"],
                envelope=envelope,
                grant_result=grant_result,
                matched_pattern=matched,
                co_occurring=masked_admission_reasons,
            )

    git_push_or_deploy_policy = str(policy["thresholds"]["git_push_or_deploy"])
    matched = None
    if git_push_or_deploy_policy == "pause":
        matched = _first_contextual_non_demotable_match(
            NON_DEMOTABLE_SIDE_EFFECT_PATTERNS,
            body,
            logged_notes,
            contextual_labels=frozenset({"install dependency"}),
        )
    if matched:
        return paused(
            mode,
            ["hard_stop_external_side_effect"],
            envelope=envelope,
            grant_result=grant_result,
            matched_pattern=matched,
            co_occurring=masked_admission_reasons,
        )

    matched = _first_sensitive_match(gate3_body, logged_notes, profile, envelope)
    if matched:
        return paused(
            mode,
            ["hard_stop_sensitive_scope"],
            envelope=envelope,
            grant_result=grant_result,
            matched_pattern=matched,
            co_occurring=masked_admission_reasons,
        )

    matched = first_match(CONTENT_SENSITIVITY_PATTERNS, body)
    if matched and not content_reply_only:
        # Fence and negation never demote this class; only the declared
        # reply-only shape (recorded above) does.
        return paused(
            mode,
            ["hard_stop_content_sensitivity"],
            envelope=envelope,
            grant_result=grant_result,
            matched_pattern=matched,
            co_occurring=masked_admission_reasons,
        )

    matched = _first_contextual_non_demotable_match(
        NON_DEMOTABLE_SENSITIVE_PATTERNS,
        body,
        logged_notes,
        contextual_labels=frozenset({"public repo"}),
    )
    if matched:
        return paused(
            mode,
            ["hard_stop_sensitive_scope"],
            envelope=envelope,
            grant_result=grant_result,
            matched_pattern=matched,
            co_occurring=masked_admission_reasons,
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
            envelope=envelope,
            grant_result=grant_result,
            matched_pattern=matched,
            co_occurring=masked_admission_reasons,
        )

    if envelope is not None:
        # Gate-2 admission verdict, read off the ledger in the pinned
        # evaluation order — the first failing axis names `reason_codes`;
        # every other axis that held is already in the ledger and lands in
        # `co_occurring_reason_codes`.
        if admission_axes["declaration"]:
            return paused(
                mode,
                ["declaration_error"],
                envelope=envelope,
                grant_result=grant_result,
                breached=[
                    entry["field"] for entry in admission_axes["declaration_errors"]
                ],
                co_occurring=masked_admission_reasons,
            )

        if "continuation_grant_scope_exceeded" in admission_axes["continuation_grant"]:
            return paused(
                mode,
                ["continuation_grant_scope_exceeded"],
                envelope=envelope,
                grant_result=grant_result,
                breached=continuation_scope_breaches(envelope, grant_result),
                co_occurring=masked_admission_reasons,
            )

        if admission_axes["declared_risk"]:
            # Declared-risk-flag pauses are admission pauses, not lexical
            # hard stops: the cause is already named by the per-knob reason
            # codes.
            return paused(
                mode,
                list(admission_axes["declared_risk"]),
                envelope=envelope,
                grant_result=grant_result,
                co_occurring=masked_admission_reasons,
            )

        reasons = [
            *admission_axes["continuation_grant"],
            *admission_axes["thresholds"],
            *admission_axes["side_effects"],
        ]
        if reasons:
            return paused(
                mode,
                reasons,
                envelope=envelope,
                grant_result=grant_result,
                co_occurring=masked_admission_reasons,
            )

    checkpoint = evaluate_threshold_checkpoint(
        envelope, grant_result, actuals, policy=policy
    )
    reauth_disposition = checkpoint["reauthorization"].get("disposition")
    if (
        checkpoint["evaluated"]
        and checkpoint["breached"]
        and reauth_disposition != "resumed"
    ):
        has_declaration_error = bool(checkpoint["declaration_errors"])
        reason = "declaration_error" if has_declaration_error else "threshold_checkpoint_breached"
        reasons = [reason]
        if reauth_disposition == "stale":
            # The pinned grant-reuse rejection: the only governing answer
            # on file was decided against an earlier pause and does not
            # cover this one.
            reasons.append("checkpoint_reauthorization_stale")
        return paused(
            mode,
            reasons,
            "checkpoint_paused",
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
    if reauth_disposition == "resumed":
        reason_codes.append("checkpoint_reauthorized")

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
    """Exit codes: 0 decision evaluated (including fail-closed pauses) ·
    2 usage/IO error · 3 intake rejected under ``verify_mode: enforce``
    (message quarantined, nothing evaluated)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--message", required=True, type=Path)
    parser.add_argument("--actuals", type=Path)
    parser.add_argument("--audit-dir", type=Path)
    parser.add_argument("--receiver", default="codex")
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (keystore + policy trust anchor)",
    )
    args = parser.parse_args(argv)

    try:
        # Single-read snapshot discipline: every security-sensitive input
        # (config, message) is read exactly once into a bounded snapshot;
        # verification and evaluation both consume THAT snapshot. Nothing
        # after this point re-reads a path — a file swapped on disk after
        # its verification cannot reach the decision logic.
        import policy_signing
        from _oacp_env import resolve_oacp_home
        from message_verify import (
            intake_verify,
            read_message_bounded,
            verify_mode_from_config,
        )

        home = (
            resolve_oacp_home(args.oacp_dir)
            if args.oacp_dir
            else resolve_oacp_home()
        )
        # Policy authorization runs before ANYTHING parses the config: a
        # tampered config must not get to choose its own verify mode.
        config_context = policy_signing.derive_policy_context(
            args.config,
            home,
            receiver=args.receiver,
            kind=policy_signing.POLICY_KIND_RECEIVER_CONFIG,
        )
        config_raw = policy_signing.read_policy_bounded(args.config)
        policy_auth = policy_signing.verify_policy_data(
            config_raw, home, receiver=args.receiver, context=config_context
        )
        config = _parse_yaml_mapping(config_raw, args.config)
        # The auth trailer is authorization metadata, not policy content —
        # policy_sha256 must name the same bytes signed and unsigned.
        config.pop("auth", None)

        message_raw = read_message_bounded(args.message)
        if policy_auth["status"] != policy_signing.POLICY_STATUS_INVALID:
            # Verified intake runs under the (now authorized) config
            # snapshot. A rejected message is quarantined, never evaluated.
            intake = intake_verify(
                args.message,
                args.config,
                receiver=args.receiver,
                oacp_dir=args.oacp_dir,
                message_raw=message_raw,
                verify_mode=verify_mode_from_config(config),
            )
            if intake["annotation"]:
                print(intake["annotation"], file=sys.stderr)
            if intake["action"] == "reject":
                print(
                    json.dumps(
                        {
                            "decision": "intake_rejected",
                            "verify_mode": intake["mode"],
                            "receiver": args.receiver,
                            "message_path": str(args.message),
                            "message_auth": intake["message_auth"],
                            "quarantine_copy": intake["quarantine_copy"],
                        },
                        indent=2,
                    )
                )
                return 3

        message = _parse_yaml_mapping(message_raw, args.message)
        actuals = load_yaml_file(args.actuals) if args.actuals else None
        decision = evaluate_autonomy(
            message,
            config,
            actuals,
            message_path=args.message,
            audit_dir=args.audit_dir,
            receiver=args.receiver,
            policy_auth=policy_auth,
            message_raw=message_raw,
        )
        if args.audit_dir is not None and decision.get("reason_codes") != [
            "message_replayed"
        ]:
            # Re-evaluations supersede rather than duplicate: an identical
            # evaluation (same bytes, same policy, same verdict) adopts the
            # existing record, and a changed one closes every live
            # predecessor as superseded so exactly one evaluation per
            # logical message stays live. The whole sequence is one
            # logical-identity transaction inside persist_evaluation.
            outcome = persist_evaluation(
                args.audit_dir,
                decision,
                config=config,
                message=message,
                message_path=args.message,
                policy_path=args.config,
                receiver=args.receiver,
            )
            decision["evaluation_id"] = outcome["evaluation_id"]
            if outcome["action"] == "adopted":
                decision["adopted_audit_record"] = str(outcome["audit_path"])
                print(
                    f"NOTE: adopted existing evaluation "
                    f"{outcome['evaluation_id']}; no duplicate record written",
                    file=sys.stderr,
                )
            if outcome["superseded"]:
                print(
                    f"NOTE: superseded {len(outcome['superseded'])} prior "
                    "evaluation(s) for this message",
                    file=sys.stderr,
                )
        elif args.audit_dir is not None:
            print("NOTE: replay detected; audit record not written", file=sys.stderr)
        print(json.dumps(decision, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
