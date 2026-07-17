# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/envelope_compiler.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from envelope_compiler import (  # noqa: E402
    ENVELOPE_COMPILE_ERROR,
    EnvelopeCompileError,
    build_envelope,
    envelope_path,
    load_envelope,
    main,
)


RECEIVER_CONFIG: Dict[str, Any] = {
    "autonomy": {
        "default_mode": "auto_review",
        "auto_review_thresholds": {
            "max_estimated_minutes": 45,
            "max_expected_files_touched": 5,
            "destructive_ops": "pause",
            "external_side_effects": "allow_pr_artifacts",
            "auth_config_or_secrets": "pause",
            "dependency_changes": "pause",
            "public_visibility": "pause",
            "git_push_or_deploy": "pause",
        },
        "allow_without_task_profile": ["brainstorm_request"],
        "private_repo_allowlist": ["example-org/private-repo"],
        "continuation_grants": {"enabled": False},
    }
}

PROFILE_BODY = """\
Implement the widget.

task_profile:
  estimated_minutes: 30
  expected_files_touched: 4
  risk_tier: P2
  target_repo: example-org/private-repo
  destructive_ops: false
  external_side_effects: true
  creates_or_updates_pr: true
  comments_on_github: false
  commits_changes: true
  sends_oacp_reply_only: false
  touches_auth_config_or_secrets: false
  touches_dependencies: false
  public_visibility: false
"""


def make_message(body: str = PROFILE_BODY, message_id: str = "msg-1") -> Dict[str, Any]:
    return {
        "id": message_id,
        "from": "iris",
        "to": "claude",
        "type": "task_request",
        "priority": "P2",
        "created_at_utc": "2026-07-12T01:00:00Z",
        "subject": "Implement the widget",
        "body": body,
    }


def test_build_envelope_happy_path() -> None:
    envelope = build_envelope(
        make_message(),
        RECEIVER_CONFIG,
        receiver="claude",
        project="test-proj",
        now_iso="2026-07-12T02:00:00Z",
    )
    assert envelope["envelope_version"] == 1
    assert envelope["message_id"] == "msg-1"
    assert envelope["project"] == "test-proj"
    assert envelope["receiver"] == "claude"
    assert envelope["enforcement"] == "hooks"
    assert envelope["compiled_at_utc"] == "2026-07-12T02:00:00Z"
    assert envelope["counters"] == {"files_touched": []}

    constraints = envelope["constraints"]
    assert constraints["estimated_minutes"] == 30
    assert constraints["expected_files_touched"] == 4
    assert constraints["risk_tier"] == "P2"
    assert constraints["target_repo"] == "example-org/private-repo"
    assert constraints["creates_or_updates_pr"] is True
    assert constraints["comments_on_github"] is False
    assert constraints["commits_changes"] is True
    assert constraints["touches_auth_config_or_secrets"] is False
    assert constraints["private_repo_allowlist"] == ["example-org/private-repo"]
    assert "continuation_grants" not in constraints


def test_build_envelope_missing_profile_fails_closed() -> None:
    with pytest.raises(EnvelopeCompileError):
        build_envelope(
            make_message(body="No profile here."),
            RECEIVER_CONFIG,
            receiver="claude",
            project="test-proj",
        )


def test_build_envelope_unparsable_profile_fails_closed() -> None:
    body = "task_profile:\n  estimated_minutes: [broken\n"
    with pytest.raises(EnvelopeCompileError):
        build_envelope(
            make_message(body=body),
            RECEIVER_CONFIG,
            receiver="claude",
            project="test-proj",
        )


def test_build_envelope_invalid_numeric_fails_closed() -> None:
    body = PROFILE_BODY.replace("estimated_minutes: 30", "estimated_minutes: soon")
    with pytest.raises(EnvelopeCompileError):
        build_envelope(
            make_message(body=body),
            RECEIVER_CONFIG,
            receiver="claude",
            project="test-proj",
        )


def test_build_envelope_malformed_config_fails_closed() -> None:
    with pytest.raises(EnvelopeCompileError):
        build_envelope(
            make_message(),
            {"autonomy": {"default_mode": "bogus"}},
            receiver="claude",
            project="test-proj",
        )


def test_build_envelope_missing_message_id_fails_closed() -> None:
    message = make_message()
    message["id"] = ""
    with pytest.raises(EnvelopeCompileError):
        build_envelope(
            message,
            RECEIVER_CONFIG,
            receiver="claude",
            project="test-proj",
        )


@pytest.mark.parametrize(
    "bad_id",
    ["*", "msg-*", "../msg-1", "msg 1", "msg?[1]", ".hidden", "m" * 129],
)
def test_build_envelope_unsafe_message_id_fails_closed(bad_id: str) -> None:
    # The id is compared against audit-record content downstream and must
    # never be able to act as a glob or path metacharacter.
    message = make_message()
    message["id"] = bad_id
    with pytest.raises(EnvelopeCompileError):
        build_envelope(
            message,
            RECEIVER_CONFIG,
            receiver="claude",
            project="test-proj",
        )


# ── CLI ──────────────────────────────────────────────────────────────────────


def _workspace(tmp_path: Path, project: str = "test-proj") -> Path:
    agent_dir = tmp_path / "projects" / project / "agents" / "claude"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        yaml.safe_dump(RECEIVER_CONFIG), encoding="utf-8"
    )
    inbox = agent_dir / "inbox"
    inbox.mkdir()
    return inbox


def _write_message(inbox: Path, message_id: str = "msg-1") -> Path:
    path = inbox / f"{message_id}.yaml"
    path.write_text(yaml.safe_dump(make_message(message_id=message_id)), encoding="utf-8")
    return path


def test_cli_compile_show_clear_roundtrip(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)

    assert main([
        "compile", str(message_path), "--oacp-dir", str(tmp_path), "--json",
    ]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["message_id"] == "msg-1"
    assert envelope["project"] == "test-proj"  # inferred from the message path

    target = envelope_path(tmp_path, "test-proj", "claude")
    assert target.is_file()

    assert main([
        "show", "--project", "test-proj", "--oacp-dir", str(tmp_path),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["message_id"] == "msg-1"

    assert main([
        "clear", "--project", "test-proj", "--oacp-dir", str(tmp_path),
    ]) == 0
    capsys.readouterr()
    assert not target.is_file()

    assert main([
        "show", "--project", "test-proj", "--oacp-dir", str(tmp_path),
    ]) == 1


def test_cli_compile_refuses_second_message_without_extend(
    tmp_path: Path, capsys
) -> None:
    inbox = _workspace(tmp_path)
    first = _write_message(inbox, "msg-1")
    second = _write_message(inbox, "msg-2")

    assert main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["compile", str(second), "--oacp-dir", str(tmp_path)]) == 3
    err = capsys.readouterr().err
    assert ENVELOPE_COMPILE_ERROR in err
    assert "msg-1" in err


def test_cli_recompile_same_message_preserves_counters(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    assert main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0

    target = envelope_path(tmp_path, "test-proj", "claude")
    envelope = load_envelope(target)
    envelope["counters"]["files_touched"] = ["/tmp/a.py", "/tmp/b.py"]
    target.write_text(json.dumps(envelope), encoding="utf-8")

    assert main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["counters"]["files_touched"] == [
        "/tmp/a.py",
        "/tmp/b.py",
    ]


def test_cli_extend_preserves_counters_across_messages(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    first = _write_message(inbox, "msg-1")
    second = _write_message(inbox, "msg-2")
    assert main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0

    target = envelope_path(tmp_path, "test-proj", "claude")
    envelope = load_envelope(target)
    envelope["counters"]["files_touched"] = ["/tmp/a.py"]
    target.write_text(json.dumps(envelope), encoding="utf-8")

    assert main([
        "compile", str(second), "--oacp-dir", str(tmp_path), "--extend",
    ]) == 0
    updated = load_envelope(target)
    assert updated["message_id"] == "msg-2"
    assert updated["counters"]["files_touched"] == ["/tmp/a.py"]


def test_cli_force_resets_counters_for_new_message(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    first = _write_message(inbox, "msg-1")
    second = _write_message(inbox, "msg-2")
    assert main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0

    target = envelope_path(tmp_path, "test-proj", "claude")
    envelope = load_envelope(target)
    envelope["counters"]["files_touched"] = ["/tmp/a.py"]
    target.write_text(json.dumps(envelope), encoding="utf-8")

    assert main([
        "compile", str(second), "--oacp-dir", str(tmp_path), "--force",
    ]) == 0
    updated = load_envelope(target)
    assert updated["message_id"] == "msg-2"
    assert updated["counters"]["files_touched"] == []


def test_cli_compile_missing_profile_exits_3(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    path = inbox / "msg-np.yaml"
    path.write_text(
        yaml.safe_dump(make_message(body="No profile.", message_id="msg-np")),
        encoding="utf-8",
    )
    assert main(["compile", str(path), "--oacp-dir", str(tmp_path)]) == 3
    assert ENVELOPE_COMPILE_ERROR in capsys.readouterr().err


def test_cli_compile_requires_project_when_not_inferable(
    tmp_path: Path, capsys
) -> None:
    _workspace(tmp_path)
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(yaml.safe_dump(make_message()), encoding="utf-8")
    assert main(["compile", str(outside), "--oacp-dir", str(tmp_path)]) == 3
    assert "cannot infer project" in capsys.readouterr().err

    assert main([
        "compile", str(outside), "--project", "test-proj", "--oacp-dir", str(tmp_path),
    ]) == 0
