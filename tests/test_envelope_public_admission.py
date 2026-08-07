# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Admitted public-visibility tasks: the none-by-rule compile branch."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import envelope_compiler  # noqa: E402
from envelope_compiler import (  # noqa: E402
    ENFORCEMENT_REASON_PUBLIC_APPROVED,
    envelope_path,
    session_claim_path,
    write_session_claim,
)

MESSAGE_ID = "msg-20260601000000-alice-p001"
PROJECT = "pub-admission"

PUBLIC_TASK_BODY = """\
Release-class task: publish the approved cut.

task_profile:
  estimated_minutes: 30
  risk_tier: P1
  expected_files_touched: 4
  destructive_ops: false
  external_side_effects: true
  touches_auth_config_or_secrets: false
  touches_dependencies: false
  public_visibility: true
  target_repo: example-org/private-repo
  creates_or_updates_pr: true
  commits_changes: true
"""

PRIVATE_TASK_BODY = PUBLIC_TASK_BODY.replace(
    "public_visibility: true", "public_visibility: false"
)

CONFIG_TEXT = """\
autonomy:
  default_mode: auto_review
  auto_review_thresholds:
    max_estimated_minutes: 45
    max_expected_files_touched: 5
    destructive_ops: pause
    external_side_effects: allow_pr_artifacts
    auth_config_or_secrets: pause
    dependency_changes: pause
    public_visibility: pause
    git_push_or_deploy: pause
  private_repo_allowlist:
    - example-org/private-repo
"""


def _write_message(path: Path, body: str) -> None:
    message = {
        "id": MESSAGE_ID,
        "from": "alice",
        "to": "claude",
        "type": "task_request",
        "priority": "P1",
        "created_at_utc": "2026-06-01T00:00:00Z",
        "subject": "Approved public cut",
        "body": body,
    }
    path.write_text(
        yaml.safe_dump(message, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _workspace(tmp_path: Path, body: str) -> dict:
    home = tmp_path / "oacp_home"
    receiver_dir = home / "projects" / PROJECT / "agents" / "claude"
    (receiver_dir / "inbox").mkdir(parents=True)
    audit_dir = receiver_dir / "audit" / "autonomy_decisions"
    audit_dir.mkdir(parents=True)
    (receiver_dir / "config.yaml").write_text(CONFIG_TEXT, encoding="utf-8")
    message_path = receiver_dir / "inbox" / "task.yaml"
    _write_message(message_path, body)
    return {
        "home": home,
        "message_path": message_path,
        "message_sha256": hashlib.sha256(message_path.read_bytes()).hexdigest(),
        "audit_dir": audit_dir,
        "envelope_path": envelope_path(home, PROJECT, "claude"),
    }


def _write_audit(
    workspace: dict,
    *,
    decision: str,
    message_id: str = MESSAGE_ID,
    message_sha256: str = "",
    gate_decision: str = "paused",
    completion_kind: str = "admission_paused",
    schema_version: str = "1.4",
    directory: Path = None,
) -> Path:
    """Write an admission audit record shaped like the gate's own output."""
    audit = {
        "schema_version": schema_version,
        "message_id": message_id,
        "receiver": "claude",
        "decision": gate_decision,
        "message_sha256": message_sha256 or workspace["message_sha256"],
        "result": {
            "final_state": "paused",
            "completion_kind": completion_kind,
            "envelope_enforcement": "none",
            "human_outcome": {
                "recorded": decision != "none",
                "actor": "alice",
                "decision": None if decision == "none" else decision,
            },
        },
    }
    target_dir = directory if directory is not None else workspace["audit_dir"]
    path = target_dir / "20260601T000100Z_admission.yaml"
    path.write_text(
        yaml.safe_dump(audit, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _compile(workspace: dict, *extra: str) -> tuple:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = envelope_compiler.main(
            [
                "compile",
                str(workspace["message_path"]),
                "--receiver", "claude",
                "--oacp-dir", str(workspace["home"]),
                *extra,
            ]
        )
    return exit_code, stdout.getvalue(), stderr.getvalue()


def test_approved_public_task_takes_none_by_rule_branch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(workspace, decision="approved")

    exit_code, stdout, stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0, stderr
    assert ENFORCEMENT_REASON_PUBLIC_APPROVED in stdout
    # The v0.4.1-cut sequence is retired: no envelope compiles, so there is
    # no compile -> hook-deny -> human-operator hand-clear chain.
    assert not workspace["envelope_path"].exists()

    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert audit["result"]["envelope_enforcement"] == "none"
    assert (
        audit["result"]["envelope_enforcement_reason"]
        == ENFORCEMENT_REASON_PUBLIC_APPROVED
    )


def test_modified_outcome_also_qualifies(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(workspace, decision="modified")
    exit_code, _stdout, _stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0
    assert not workspace["envelope_path"].exists()


def test_unapproved_public_task_still_compiles_denying_envelope(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(workspace, decision="declined")

    exit_code, _stdout, stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0, stderr
    envelope = yaml.safe_load(workspace["envelope_path"].read_text(encoding="utf-8"))
    assert envelope["constraints"]["public_visibility"] is True

    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


def test_public_task_without_audit_still_compiles(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    exit_code, _stdout, stderr = _compile(workspace)
    assert exit_code == 0, stderr
    assert workspace["envelope_path"].exists()


def test_mismatched_audit_record_falls_through_to_compile(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(
        workspace, decision="approved", message_id="msg-other-task"
    )
    exit_code, _stdout, _stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0
    assert workspace["envelope_path"].exists()
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


def test_private_task_with_approved_audit_is_unaffected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PRIVATE_TASK_BODY)
    audit_path = _write_audit(workspace, decision="approved")
    exit_code, _stdout, _stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0
    envelope = yaml.safe_load(workspace["envelope_path"].read_text(encoding="utf-8"))
    assert envelope["constraints"]["public_visibility"] is False
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


# ---------------------------------------------------------------------------
# Authorization binding: only the gate's own admission record qualifies
# ---------------------------------------------------------------------------


def test_audit_outside_canonical_directory_is_refused(tmp_path: Path) -> None:
    """An arbitrary readable YAML must never authorize skipping enforcement."""
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    stray_dir = tmp_path / "elsewhere"
    stray_dir.mkdir()
    stray = _write_audit(workspace, decision="approved", directory=stray_dir)

    exit_code, _stdout, stderr = _compile(workspace, "--audit", str(stray))
    assert exit_code != 0
    assert "canonical admission audit directory" in stderr
    # Fail closed without altering either artifact.
    assert not workspace["envelope_path"].exists()
    audit = yaml.safe_load(stray.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


def test_wrong_message_hash_falls_through_to_compile(tmp_path: Path) -> None:
    """The record must bind to the exact verified message snapshot."""
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(
        workspace, decision="approved", message_sha256="0" * 64
    )
    exit_code, _stdout, _stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0
    assert workspace["envelope_path"].exists()
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


def test_wrong_phase_record_falls_through_to_compile(tmp_path: Path) -> None:
    """Only an admission-paused record qualifies — not an auto-accept."""
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(
        workspace,
        decision="approved",
        gate_decision="auto_accepted",
        completion_kind="auto_accepted",
    )
    exit_code, _stdout, _stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0
    assert workspace["envelope_path"].exists()
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


def test_record_without_schema_version_falls_through(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(workspace, decision="approved", schema_version="")
    exit_code, _stdout, _stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0
    assert workspace["envelope_path"].exists()


def test_replacement_race_stamps_only_the_locked_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """Eligibility and the stamp consume ONE locked read.

    The record is swapped to an unapproved one at the moment the audit
    lock is acquired — a split eligibility-then-stamp design would have
    approved the old read and stamped the new record; the single locked
    read must see the swap and fall through to the normal compile.
    """
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(workspace, decision="approved")
    original_locked_audit = envelope_compiler.locked_audit

    @contextlib.contextmanager
    def swapping_lock(path: Path):
        with original_locked_audit(path):
            if Path(path) == audit_path:
                _write_audit(workspace, decision="declined")
            yield

    monkeypatch.setattr(envelope_compiler, "locked_audit", swapping_lock)
    exit_code, _stdout, _stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0
    assert workspace["envelope_path"].exists()
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


# ---------------------------------------------------------------------------
# Envelope lifecycle: none-by-rule must mean NO envelope governs the receiver
# ---------------------------------------------------------------------------


def _compile_active_envelope(workspace: dict) -> None:
    exit_code, _stdout, stderr = _compile(workspace)
    assert exit_code == 0, stderr
    assert workspace["envelope_path"].exists()


def test_existing_envelope_same_message_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    _compile_active_envelope(workspace)
    before = workspace["envelope_path"].read_bytes()
    audit_path = _write_audit(workspace, decision="approved")

    exit_code, _stdout, stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code != 0
    assert "must not coexist" in stderr
    assert workspace["envelope_path"].read_bytes() == before
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


def test_existing_envelope_different_message_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    _compile_active_envelope(workspace)
    # Re-point the workspace at a different admitted message.
    other = workspace["message_path"].with_name("other.yaml")
    other.write_text(
        workspace["message_path"]
        .read_text(encoding="utf-8")
        .replace(MESSAGE_ID, "msg-20260601000000-alice-p002"),
        encoding="utf-8",
    )
    workspace["message_path"] = other
    workspace["message_sha256"] = hashlib.sha256(other.read_bytes()).hexdigest()
    audit_path = _write_audit(
        workspace,
        decision="approved",
        message_id="msg-20260601000000-alice-p002",
    )

    exit_code, _stdout, stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code != 0
    assert "must not coexist" in stderr
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert "envelope_enforcement_reason" not in audit["result"]


def test_none_by_rule_consumes_pending_session_claim(tmp_path: Path) -> None:
    """A deliberate no-envelope success must not leave a dangling claim."""
    workspace = _workspace(tmp_path, PUBLIC_TASK_BODY)
    audit_path = _write_audit(workspace, decision="approved")
    target = workspace["envelope_path"]
    write_session_claim(target, "sess-a", workspace["message_path"].name)
    claim_file = session_claim_path(target, "sess-a")
    assert claim_file.exists()

    exit_code, _stdout, stderr = _compile(workspace, "--audit", str(audit_path))
    assert exit_code == 0, stderr
    assert not target.exists()
    assert not claim_file.exists()

    # A later, unrelated compile must come up unbound — never bound to the
    # consumed claim.
    other = workspace["message_path"].with_name("later.yaml")
    other.write_text(
        workspace["message_path"]
        .read_text(encoding="utf-8")
        .replace(MESSAGE_ID, "msg-20260601000000-alice-p003")
        .replace("public_visibility: true", "public_visibility: false"),
        encoding="utf-8",
    )
    workspace["message_path"] = other
    exit_code, _stdout, stderr = _compile(workspace)
    assert exit_code == 0, stderr
    envelope = json.loads(target.read_text(encoding="utf-8"))
    assert envelope["session_id"] is None
