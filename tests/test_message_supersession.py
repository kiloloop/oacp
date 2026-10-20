# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Never-admitted review closure; all identities and message content are synthetic."""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import finalize_autonomy_record as finalizer  # noqa: E402
from _oacp_constants import locked_audit, atomic_replace_yaml  # noqa: E402
from message_signing import CRYPTO_AVAILABLE, generate_keypair, load_signers, sign_and_append  # noqa: E402
from send_inbox_message import build_message_dict, render_yaml  # noqa: E402

pytestmark = pytest.mark.skipif(not CRYPTO_AVAILABLE, reason="cryptography unavailable")


@pytest.fixture
def setup(tmp_path):
    receiver = tmp_path / "projects/demo/agents/reviewer"
    audit = receiver / "audit/autonomy_decisions/original.yaml"
    audit.parent.mkdir(parents=True)
    inbox = receiver / "inbox"
    inbox.mkdir()
    (inbox / "archive").mkdir()
    (receiver / "config.yaml").write_text("signing:\n  verify_mode: enforce\n")
    report = generate_keypair("author", tmp_path)
    stub = json.loads(Path(report["public_stub_path"]).read_text())
    pins = receiver / "trust/allowed_signers.yaml"
    pins.parent.mkdir()
    pins.write_text(yaml.safe_dump({"version": 1, "signers": [
        {"agent": "author", "kid": stub["kid"], "jwk": stub["jwk"], "status": "active"}]}))
    message = build_message_dict(sender="author", recipient="reviewer", msg_type="review_request",
                                 subject="Replacement review", body="pr: 1\n")
    message["conversation_id"] = "conv-20260101-author-01"
    signers = load_signers("author", tmp_path)
    replacement = inbox / "replacement.yaml"
    replacement.write_bytes(sign_and_append(render_yaml(message).encode(), signers))
    record = {"schema_version": 2, "spec_version": "0.5.2", "receiver": "reviewer", "sender": "author",
              "message_id": "msg-20260101000000-author-abcd", "message_type": "review_request", "message_sha256": "a" * 64,
              "conversation_id": message["conversation_id"], "decision": "paused",
              "created_at_utc": "2026-01-01T00:00:00Z",
              "result": {"final_state": "paused", "completion_kind": "admission_paused",
                         "actual_minutes": None, "actual_files_touched": None, "completed_at_utc": None}}
    audit.write_text(yaml.safe_dump(record))
    return audit, record, replacement, message, signers


def invoke(audit, replacement, *extra):
    return finalizer.main([str(audit), "--final-state", "superseded",
                           "--superseded-by-message", str(replacement), "--json", *extra])


def test_signed_replacement_closes_without_admitting_either(setup):
    audit, original, replacement, _, _ = setup
    replacement_before = replacement.read_bytes()
    assert invoke(audit, replacement) == 0
    stored = finalizer.load_audit_strict(audit)
    assert stored["decision"] == "paused"
    assert stored["message_sha256"] == original["message_sha256"]
    assert stored["result"]["completion_kind"] == "admission_paused"
    assert stored["result"]["final_state"] == "superseded"
    assert "human_outcome" not in stored["result"]
    receipt = stored["result"]["superseded_by_message"]
    assert receipt["message_sha256"] == hashlib.sha256(replacement_before).hexdigest()
    assert replacement.read_bytes() == replacement_before
    assert finalizer.validate_audit_record(stored) == []
    assert finalizer.sweep_audit_dir(audit.parent)["records"][audit.name] == []
    assert invoke(audit, replacement) == 2


@pytest.mark.parametrize("change", [
    {"decision": "auto_accepted"}, {"message_type": "task_request"}, {"schema_version": 1},
    {"result": {"human_outcome": {"recorded": True, "decision": "approved"}}},
    {"result": {"work_started_at_utc": "2026-01-01T00:01:00Z"}},
    {"result": {"actual_minutes": 1}}, {"result": {"actual_files_touched": 1}},
    {"result": {"completion_kind": "checkpoint_paused"}},
    {"result": {"completed_at_utc": "2026-01-01T00:01:00Z"}},
    {"result": {"threshold_checkpoint": {"evaluated": True}}},
    {"result": {"reply_message_id": "msg-20260101000100-reviewer-abcd"}},
    {"result": {"artifacts": ["result.txt"]}},
    {"result": {"predicted_risk_materialized": True}},
])
def test_ineligible_record_preserved(setup, change):
    audit, record, replacement, _, _ = setup
    for key, value in change.items():
        if key == "result":
            record[key].update(value)
        else:
            record[key] = value
    audit.write_text(yaml.safe_dump(record))
    before = audit.read_bytes()
    assert invoke(audit, replacement) == 2
    assert audit.read_bytes() == before


@pytest.mark.parametrize("field,value", [("id", "msg-20260101000000-author-abcd"), ("to", "other"),
                                         ("conversation_id", "conv-20260101-author-02"), ("type", "notification")])
def test_wrong_replacement_identity(setup, field, value):
    audit, _, replacement, message, signers = setup
    message[field] = value
    replacement.write_bytes(sign_and_append(render_yaml(message).encode(), signers))
    before = audit.read_bytes()
    assert invoke(audit, replacement) == 2
    assert audit.read_bytes() == before


def test_unsigned_warn_mode_cannot_supply_sender_authority(setup):
    audit, _, replacement, message, _ = setup
    (audit.parents[2] / "config.yaml").write_text("signing:\n  verify_mode: warn\n")
    replacement.write_text(render_yaml(message))
    assert invoke(audit, replacement) == 2


def test_wrong_sender_refused(setup):
    audit, record, replacement, _, _ = setup
    record["sender"] = "other-author"
    audit.write_text(yaml.safe_dump(record))
    assert invoke(audit, replacement) == 2


def test_deleted_source_before_commit_is_refused(setup, monkeypatch):
    audit, _, replacement, _, _ = setup
    original = finalizer.finalize_audit_record
    before = audit.read_bytes()

    def delete_after_verification(*args, **kwargs):
        result = original(*args, **kwargs)
        replacement.unlink()
        return result

    monkeypatch.setattr(finalizer, "finalize_audit_record", delete_after_verification)
    assert invoke(audit, replacement) == 2
    assert audit.read_bytes() == before


def test_changed_source_before_commit_is_refused(setup, monkeypatch):
    audit, _, replacement, _, _ = setup
    original = finalizer.finalize_audit_record
    before = audit.read_bytes()

    def change_after_verification(*args, **kwargs):
        result = original(*args, **kwargs)
        replacement.write_bytes(b"changed")
        return result

    monkeypatch.setattr(finalizer, "finalize_audit_record", change_after_verification)
    assert invoke(audit, replacement) == 2
    assert audit.read_bytes() == before


def test_duplicate_keys_refused_after_verification(setup):
    audit, _, replacement, message, signers = setup
    raw = render_yaml(message).encode() + b"conversation_id: conv-20260101-author-02\n"
    replacement.write_bytes(sign_and_append(raw, signers))
    assert invoke(audit, replacement) == 2


def test_approval_winning_shared_lock_prevents_closure(setup):
    import select

    audit, record, replacement, _, _ = setup
    # The sentinel establishes that the child reached the lock boundary, not
    # merely that Python startup exceeded the observation timeout.
    wrapper = """import sys
sys.path.insert(0, sys.argv.pop(1))
import finalize_autonomy_record as f
real_lock = f.locked_audit
def lock(path):
    print('at-lock', flush=True)
    return real_lock(path)
f.locked_audit = lock
raise SystemExit(f.main())
"""
    process = None
    try:
        with locked_audit(audit):
            process = subprocess.Popen([sys.executable, "-c", wrapper, str(Path(finalizer.__file__).parent),
                                        str(audit), "--final-state", "superseded",
                                        "--superseded-by-message", str(replacement)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert select.select([process.stdout], [], [], 10)[0]
            assert process.stdout.readline().strip() == b"at-lock"
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
            record["result"]["human_outcome"] = {"recorded": True, "decision": "approved"}
            atomic_replace_yaml(audit, record)
            before = audit.read_bytes()
        process.communicate(timeout=10)
        assert process.returncode == 2
        assert audit.read_bytes() == before
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()


def test_readback_rejects_forged_receipt_and_work(setup):
    audit, _, replacement, _, _ = setup
    assert invoke(audit, replacement) == 0
    stored = finalizer.load_audit_strict(audit)
    for field, value in [("sender", "other"), ("message_sha256", "invalid"),
                         ("disposition_at_utc", "2026-01-01T00:00:00Z")]:
        changed = copy.deepcopy(stored)
        changed["result"]["superseded_by_message"][field] = value
        assert finalizer.validate_audit_record(changed)
    changed = copy.deepcopy(stored)
    changed["result"]["actual_minutes"] = 1
    assert finalizer.validate_audit_record(changed)


@pytest.mark.parametrize("extra", [["--superseded-by", "eval-0123456789abcdef"],
                                   ["--started-at", "2026-01-01T00:00:00Z"],
                                   ["--actual-files-touched", "1"], ["--replace"]])
def test_incompatible_options_preserve_record(setup, extra):
    audit, _, replacement, _, _ = setup
    before = audit.read_bytes()
    assert invoke(audit, replacement, *extra) == 2
    assert audit.read_bytes() == before


@pytest.mark.parametrize("kind", ["other_receiver", "other_project", "outside_symlink"])
def test_containment_uses_eligible_record(setup, tmp_path, kind):
    audit, _, replacement, _, _ = setup
    relative = {"other_receiver": "projects/demo/agents/other/inbox/replacement.yaml",
                "other_project": "projects/other/agents/reviewer/inbox/replacement.yaml",
                "outside_symlink": "outside/replacement.yaml"}[kind]
    outside = tmp_path / relative
    outside.parent.mkdir(parents=True)
    outside.write_bytes(replacement.read_bytes())
    candidate = outside
    if kind == "outside_symlink":
        candidate = replacement.parent / "alias.yaml"
        candidate.symlink_to(outside)
    before = audit.read_bytes()
    assert invoke(audit, candidate) == 2
    assert audit.read_bytes() == before


def test_verification_off_refused_without_mutating_audit(setup):
    audit, _, replacement, _, _ = setup
    (audit.parents[2] / "config.yaml").write_text("signing:\n  verify_mode: off\n")
    before = audit.read_bytes()
    assert invoke(audit, replacement) == 2
    assert audit.read_bytes() == before


def test_empty_shared_conversation_refused(setup):
    audit, record, replacement, message, signers = setup
    record["conversation_id"] = message["conversation_id"] = ""
    audit.write_text(yaml.safe_dump(record))
    replacement.write_bytes(sign_and_append(render_yaml(message).encode(), signers))
    before = audit.read_bytes()
    assert invoke(audit, replacement) == 2
    assert audit.read_bytes() == before


def test_terminal_original_is_archived_before_next_inbox_pass(setup, tmp_path):
    audit, record, replacement, message, signers = setup
    original = replacement.parent / "original.yaml"
    payload = dict(message, id=record["message_id"])
    raw = sign_and_append(render_yaml(payload).encode(), signers)
    original.write_bytes(raw)
    record["message_sha256"] = hashlib.sha256(raw).hexdigest()
    audit.write_text(yaml.safe_dump(record))
    before = original.stat()
    assert invoke(audit, replacement) == 0
    assert original.read_bytes() == raw  # Finalization alone never consumes input.
    target = original.parent / "archive" / original.name
    assert not target.exists()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == record["message_sha256"]
    original.rename(target)
    assert target.read_bytes() == raw and target.stat().st_mtime_ns == before.st_mtime_ns
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-m", "oacp.cli", "inbox", "demo", "--agent", "reviewer",
                             "--oacp-dir", str(tmp_path), "--json"],
                            cwd=root, capture_output=True, text=True, check=True)
    messages = json.loads(result.stdout)["agents"][0]["messages"]
    assert [Path(row["path"]).name for row in messages] == [replacement.name]
    assert finalizer.validate_audit_record(finalizer.load_audit_strict(audit)) == []
