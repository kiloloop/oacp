#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Generate the documented audit example with the real evaluator and writer.

Development-only tooling; it is deliberately outside the packaged scripts/.
Run ``make docs`` to update the marked block, or use ``--check`` to detect drift.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import difflib
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT_PATH = REPO_ROOT / "docs" / "protocol" / "autonomy.md"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from autonomy_gate import (  # noqa: E402
    RUNTIME_EFFORT_ENV_VAR,
    RUNTIME_MODEL_ENV_VAR,
    evaluate_autonomy,
    write_audit_record,
)

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED: autonomy audit — do not edit by hand; run `make docs` -->"
)
END_MARKER = "<!-- END GENERATED: autonomy audit -->"
EXAMPLE_TIME = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def write_example(workspace: Path) -> Path:
    """Evaluate and persist one synthetic admission inside an isolated workspace."""
    # The format is realistic; all names, identifiers, and task data are synthetic.
    profile = {
        "estimated_minutes": 20,
        "risk_tier": "P3",
        "expected_files_touched": 1,
        "destructive_ops": False,
        "external_side_effects": False,
        "touches_auth_config_or_secrets": False,
        "touches_dependencies": False,
        "public_visibility": False,
        "creates_or_updates_pr": False,
        "comments_on_github": False,
        "commits_changes": False,
        "merges_pr": False,
        "files_issues": False,
        "sends_oacp_reply_only": False,
    }
    message = {
        "id": "msg-20260101000000-author-example",
        "from": "author",
        "to": "reviewer",
        "type": "task_request",
        "priority": "P3",
        "created_at_utc": "2026-01-01T00:00:00Z",
        "subject": "Clarify a usage paragraph",
        "conversation_id": "conv-20260101-author-001",
        "parent_message_id": None,
        "body": yaml.safe_dump(
            {
                "description": "Clarify one documentation paragraph.",
                "task_profile": profile,
            },
            sort_keys=False,
        ),
    }
    config = {
        "autonomy": {
            "default_mode": "auto_review",
            "auto_review_thresholds": {
                "max_estimated_minutes": 45,
                "max_expected_files_touched": 5,
                "destructive_ops": "pause",
                "external_side_effects": "pause",
                "auth_config_or_secrets": "pause",
                "dependency_changes": "pause",
                "public_visibility": "pause",
                "git_push_or_deploy": "pause",
            },
        },
    }
    agent_dir = workspace / "agents" / "reviewer"
    message_path = agent_dir / "inbox" / "20260101000000_author_task_request.yaml"
    policy_path = agent_dir / "config.yaml"
    message_path.parent.mkdir(parents=True)
    message_raw = yaml.safe_dump(message, sort_keys=False).encode("utf-8")
    message_path.write_bytes(message_raw)
    policy_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    # A synthetic example must not inherit a host's serving-model or effort
    # signal. Let the actual writer produce its explicit-unknown block,
    # without a fake model or level.
    with patch.dict("os.environ", {RUNTIME_MODEL_ENV_VAR: "", RUNTIME_EFFORT_ENV_VAR: ""}):
        decision = evaluate_autonomy(
            message,
            config,
            message_path=message_path,
            message_raw=message_raw,
            receiver="reviewer",
            now_utc=EXAMPLE_TIME,
        )
        return write_audit_record(
            agent_dir / "audit" / "autonomy_decisions",
            decision,
            config=config,
            message=message,
            message_path=message_path,
            policy_path=policy_path,
            receiver="reviewer",
            now_utc=EXAMPLE_TIME,
        )


def normalize_provenance(record: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    """Normalize only host-dependent scalar provenance; retain every writer key."""
    normalized = copy.deepcopy(record)
    for field in ("message_path", "policy_path"):
        normalized[field] = Path(record[field]).relative_to(workspace).as_posix()
    normalized["evaluator"]["content_sha256"] = "<sha256 of evaluator file bytes>"
    normalized["evaluator"]["git_sha"] = None
    return normalized


def render_block() -> str:
    with tempfile.TemporaryDirectory(prefix="oacp-audit-example-") as temporary:
        workspace = Path(temporary)
        audit_path = write_example(workspace)
        record = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        example = yaml.safe_dump(
            normalize_provenance(record, workspace),
            sort_keys=False,
            allow_unicode=True,
        ).rstrip("\n")
    return "\n".join(
        [
            BEGIN_MARKER,
            "Synthetic admission generated by the actual evaluator and audit writer.",
            "Temporary paths are relative; evaluator hash and Git provenance are normalized.",
            "The absent serving-model and effort signals are recorded by the writer as unknown.",
            "",
            "```yaml",
            example,
            "```",
            END_MARKER,
        ]
    )


def extract_block(document: str) -> str:
    if document.count(BEGIN_MARKER) != 1 or document.count(END_MARKER) != 1:
        raise ValueError("expected exactly one generated autonomy-audit marker pair")
    start = document.index(BEGIN_MARKER)
    end = document.index(END_MARKER)
    if end < start:
        raise ValueError("autonomy-audit markers are reversed")
    return document[start : end + len(END_MARKER)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", type=Path, default=DOCUMENT_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="update the marked example")
    mode.add_argument("--check", action="store_true", help="exit 1 on example drift")
    args = parser.parse_args(argv)
    expected = render_block()
    if not (args.write or args.check):
        print(expected)
        return 0
    document = args.document.read_text(encoding="utf-8")
    current = extract_block(document)
    if current == expected:
        print("OK: autonomy audit example matches the evaluator and writer")
        return 0
    if args.write:
        args.document.write_text(
            document.replace(current, expected, 1), encoding="utf-8"
        )
        print("Regenerated autonomy audit example")
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=f"{args.document.name} (current)",
            tofile="audit writer (expected)",
        )
    )
    print("DRIFT: autonomy audit example is stale; run `make docs`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
