# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/envelope_compiler.py."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from envelope_compiler import (  # noqa: E402
    ADAPTER_EXPECTED_MISSING,
    ADAPTER_FAILED,
    ADAPTER_RESOLVED,
    ADAPTER_UNSUPPORTED,
    ENVELOPE_COMPILE_ERROR,
    AdapterDetection,
    AdapterDetectionError,
    EnvelopeCompileError,
    build_envelope,
    detect_adapter,
    detect_receiver_adapter,
    envelope_path,
    load_envelope,
    main,
    read_receiver_runtime,
    session_claim_path,
    workspace_hook_registration,
    write_session_claim,
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


# ── Adapter detection inputs ─────────────────────────────────────────────────


def _which_present(name: str) -> str:
    return f"/opt/oacp/bin/{name}"


def _which_absent(name: str) -> None:
    return None


def _registered(console: str) -> bool:
    return True


def _unregistered(console: str) -> bool:
    return False


def _probe_unreadable(console: str) -> bool:
    raise AdapterDetectionError("workspace marker not found")


RESOLVED_ADAPTER = detect_adapter(
    "claude", which_fn=_which_present, registration_fn=_registered
)


def _main(argv: Sequence[str], **overrides: Any) -> int:
    """Run the CLI against a resolved claude adapter unless overridden."""
    kwargs: Dict[str, Any] = {
        "which_fn": _which_present,
        "registration_fn": _registered,
    }
    kwargs.update(overrides)
    return main(list(argv), **kwargs)


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


def test_expected_pauses_do_not_change_envelope_constraints() -> None:
    original = make_message()
    annotated = make_message(
        PROFILE_BODY + "  expected_pauses: [merges_pr_pause, future_pause_code]\n"
    )
    args = {
        "receiver": "claude", "project": "test-proj",
        "now_iso": "2026-07-12T02:00:00Z", "adapter": RESOLVED_ADAPTER,
    }
    before = build_envelope(original, RECEIVER_CONFIG, **args)
    after = build_envelope(annotated, RECEIVER_CONFIG, **args)
    assert before["message_sha256"] != after["message_sha256"]
    assert {k: v for k, v in before.items() if k != "message_sha256"} == {
        k: v for k, v in after.items() if k != "message_sha256"
    }


def test_build_envelope_happy_path() -> None:
    envelope = build_envelope(
        make_message(),
        RECEIVER_CONFIG,
        receiver="claude",
        project="test-proj",
        now_iso="2026-07-12T02:00:00Z",
        adapter=RESOLVED_ADAPTER,
    )
    assert envelope["envelope_version"] == 1
    assert envelope["message_id"] == "msg-1"
    assert envelope["project"] == "test-proj"
    assert envelope["receiver"] == "claude"
    # The resolved state is the only one that earns hooks, and it carries
    # no reason: a reason is present exactly when enforcement is none.
    assert envelope["enforcement"] == "hooks"
    assert "enforcement_reason" not in envelope
    assert envelope["adapter"]["state"] == ADAPTER_RESOLVED
    assert envelope["adapter"]["runtime"] == "claude"
    assert envelope["adapter"]["console"] == "oacp-envelope-hook"
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


# ── Adapter detection ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("runtime", "which_fn", "registration_fn", "state", "reason"),
    [
        # claude: both inputs present is the only way to earn hooks.
        ("claude", _which_present, _registered, ADAPTER_RESOLVED, None),
        ("claude", _which_absent, _registered, ADAPTER_EXPECTED_MISSING, "adapter_expected_missing"),
        ("claude", _which_present, _unregistered, ADAPTER_EXPECTED_MISSING, "adapter_expected_missing"),
        ("claude", _which_absent, _unregistered, ADAPTER_EXPECTED_MISSING, "adapter_expected_missing"),
        ("claude", _which_present, _probe_unreadable, ADAPTER_FAILED, "adapter_detection_failed"),
        # Known runtimes with no adapter by design.
        ("codex", _which_present, _registered, ADAPTER_UNSUPPORTED, "adapter_unsupported"),
        ("cursor", _which_present, _registered, ADAPTER_UNSUPPORTED, "adapter_unsupported"),
        ("gemini", _which_present, _registered, ADAPTER_UNSUPPORTED, "adapter_unsupported"),
        ("human", _which_present, _registered, ADAPTER_UNSUPPORTED, "adapter_unsupported"),
        # No runtime identity to detect against.
        ("unknown", _which_present, _registered, ADAPTER_FAILED, "adapter_detection_failed"),
        (None, _which_present, _registered, ADAPTER_FAILED, "adapter_detection_failed"),
        ("", _which_present, _registered, ADAPTER_FAILED, "adapter_detection_failed"),
        ("clade", _which_present, _registered, ADAPTER_FAILED, "adapter_detection_failed"),
    ],
)
def test_detect_adapter_grammar(runtime, which_fn, registration_fn, state, reason) -> None:
    detection = detect_adapter(
        runtime, which_fn=which_fn, registration_fn=registration_fn
    )
    assert detection.state == state
    assert detection.enforcement_reason == reason
    assert detection.enforcement == ("hooks" if state == ADAPTER_RESOLVED else "none")
    assert detection.advisory == (state in (ADAPTER_EXPECTED_MISSING, ADAPTER_FAILED))
    assert detection.detail


def test_detect_adapter_names_every_missing_input() -> None:
    detection = detect_adapter(
        "claude", which_fn=_which_absent, registration_fn=_unregistered
    )
    assert "not found on PATH" in detection.detail
    assert "not registered" in detection.detail


def test_detect_adapter_never_raises_on_a_broken_probe() -> None:
    def exploding_which(name: str) -> str:
        raise OSError("PATH is on fire")

    def exploding_registration(console: str) -> bool:
        raise ValueError("settings is on fire")

    by_which = detect_adapter(
        "claude", which_fn=exploding_which, registration_fn=_registered
    )
    assert by_which.state == ADAPTER_FAILED
    assert "PATH is on fire" in by_which.detail
    by_registration = detect_adapter(
        "claude", which_fn=_which_present, registration_fn=exploding_registration
    )
    assert by_registration.state == ADAPTER_FAILED
    assert "settings is on fire" in by_registration.detail


def test_detect_adapter_carries_the_runtime_error() -> None:
    detection = detect_adapter(
        None,
        which_fn=_which_present,
        registration_fn=_registered,
        runtime_error="agent card not found: /x/agent_card.yaml",
    )
    assert detection.state == ADAPTER_FAILED
    assert detection.detail == "agent card not found: /x/agent_card.yaml"


def test_build_envelope_without_detection_is_loud_none() -> None:
    """A caller that never detected an adapter never gets hooks."""
    envelope = build_envelope(
        make_message(), RECEIVER_CONFIG, receiver="claude", project="test-proj"
    )
    assert envelope["enforcement"] == "none"
    assert envelope["enforcement_reason"] == "adapter_detection_failed"
    assert envelope["adapter"]["state"] == ADAPTER_FAILED


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (ADAPTER_UNSUPPORTED, "adapter_unsupported"),
        (ADAPTER_EXPECTED_MISSING, "adapter_expected_missing"),
        (ADAPTER_FAILED, "adapter_detection_failed"),
    ],
)
def test_build_envelope_none_states_name_their_reason(state: str, reason: str) -> None:
    envelope = build_envelope(
        make_message(),
        RECEIVER_CONFIG,
        receiver="claude",
        project="test-proj",
        adapter=AdapterDetection(state, "claude", "oacp-envelope-hook", "why"),
    )
    assert envelope["enforcement"] == "none"
    assert envelope["enforcement_reason"] == reason
    assert envelope["adapter"] == {
        "runtime": "claude",
        "state": state,
        "console": "oacp-envelope-hook",
        "detail": "why",
    }


# ── Workspace hook registration probe ────────────────────────────────────────


def _repo_with_settings(tmp_path: Path, settings: Optional[Any]) -> Path:
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    if settings is not None:
        (repo / ".claude" / "settings.json").write_text(
            settings if isinstance(settings, str) else json.dumps(settings),
            encoding="utf-8",
        )
    return repo


def _write_workspace_marker(home: Path, project: str, repo_path: Any) -> None:
    project_dir = home / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "workspace.json").write_text(
        json.dumps({"project_name": project, "repo_path": repo_path}),
        encoding="utf-8",
    )


HOOK_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash|Edit|Write|NotebookEdit",
                "hooks": [{"type": "command", "command": "oacp-envelope-hook"}],
            }
        ]
    }
}


def test_registration_probe_finds_the_setup_written_hook(tmp_path: Path) -> None:
    repo = _repo_with_settings(tmp_path, HOOK_SETTINGS)
    _write_workspace_marker(tmp_path, "test-proj", str(repo))
    assert workspace_hook_registration(tmp_path, "test-proj", "oacp-envelope-hook") is True
    # Detection is by console name: a different console is not this adapter.
    assert workspace_hook_registration(tmp_path, "test-proj", "other-hook") is False


@pytest.mark.parametrize(
    "settings",
    [
        None,
        {},
        {"hooks": {}},
        {"hooks": {"PreToolUse": []}},
        {"hooks": {"PreToolUse": "not-a-list"}},
        {"hooks": {"SessionStart": HOOK_SETTINGS["hooks"]["PreToolUse"]}},
    ],
    ids=["no-file", "empty", "no-events", "empty-event", "bad-event", "wrong-event"],
)
def test_registration_probe_reports_absent_registration(tmp_path: Path, settings) -> None:
    repo = _repo_with_settings(tmp_path, settings)
    _write_workspace_marker(tmp_path, "test-proj", str(repo))
    assert workspace_hook_registration(tmp_path, "test-proj", "oacp-envelope-hook") is False


def test_registration_probe_raises_when_indeterminate(tmp_path: Path) -> None:
    # No workspace marker at all.
    with pytest.raises(AdapterDetectionError, match="workspace marker not found"):
        workspace_hook_registration(tmp_path, "test-proj", "oacp-envelope-hook")
    # A marker with no repo_path.
    _write_workspace_marker(tmp_path, "test-proj", None)
    with pytest.raises(AdapterDetectionError, match="names no repo_path"):
        workspace_hook_registration(tmp_path, "test-proj", "oacp-envelope-hook")
    # A repo_path that is not a directory.
    _write_workspace_marker(tmp_path, "test-proj", str(tmp_path / "gone"))
    with pytest.raises(AdapterDetectionError, match="not a directory"):
        workspace_hook_registration(tmp_path, "test-proj", "oacp-envelope-hook")
    # Unreadable settings.
    repo = _repo_with_settings(tmp_path, "{not json")
    _write_workspace_marker(tmp_path, "test-proj", str(repo))
    with pytest.raises(AdapterDetectionError, match="hook settings unreadable"):
        workspace_hook_registration(tmp_path, "test-proj", "oacp-envelope-hook")


# ── CLI ──────────────────────────────────────────────────────────────────────


def _workspace(
    tmp_path: Path, project: str = "test-proj", runtime: Optional[str] = "claude"
) -> Path:
    agent_dir = tmp_path / "projects" / project / "agents" / "claude"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        yaml.safe_dump(RECEIVER_CONFIG), encoding="utf-8"
    )
    if runtime is not None:
        (agent_dir / "agent_card.yaml").write_text(
            yaml.safe_dump({"agent": "claude", "runtime": runtime}), encoding="utf-8"
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

    assert _main([
        "compile", str(message_path), "--oacp-dir", str(tmp_path), "--json",
    ]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["message_id"] == "msg-1"
    assert envelope["project"] == "test-proj"  # inferred from the message path

    target = envelope_path(tmp_path, "test-proj", "claude")
    assert target.is_file()

    assert _main([
        "show", "--project", "test-proj", "--oacp-dir", str(tmp_path),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["message_id"] == "msg-1"

    assert _main([
        "clear", "--project", "test-proj", "--oacp-dir", str(tmp_path),
    ]) == 0
    capsys.readouterr()
    assert not target.is_file()

    assert _main([
        "show", "--project", "test-proj", "--oacp-dir", str(tmp_path),
    ]) == 1


def test_cli_compile_refuses_second_message_without_extend(
    tmp_path: Path, capsys
) -> None:
    inbox = _workspace(tmp_path)
    first = _write_message(inbox, "msg-1")
    second = _write_message(inbox, "msg-2")

    assert _main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    assert _main(["compile", str(second), "--oacp-dir", str(tmp_path)]) == 3
    err = capsys.readouterr().err
    assert ENVELOPE_COMPILE_ERROR in err
    assert "msg-1" in err


def test_cli_recompile_same_message_preserves_counters(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0

    target = envelope_path(tmp_path, "test-proj", "claude")
    envelope = load_envelope(target)
    envelope["counters"]["files_touched"] = ["/tmp/a.py", "/tmp/b.py"]
    target.write_text(json.dumps(envelope), encoding="utf-8")

    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["counters"]["files_touched"] == [
        "/tmp/a.py",
        "/tmp/b.py",
    ]


def test_cli_extend_preserves_counters_across_messages(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    first = _write_message(inbox, "msg-1")
    second = _write_message(inbox, "msg-2")
    assert _main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0

    target = envelope_path(tmp_path, "test-proj", "claude")
    envelope = load_envelope(target)
    envelope["counters"]["files_touched"] = ["/tmp/a.py"]
    target.write_text(json.dumps(envelope), encoding="utf-8")

    assert _main([
        "compile", str(second), "--oacp-dir", str(tmp_path), "--extend",
    ]) == 0
    updated = load_envelope(target)
    assert updated["message_id"] == "msg-2"
    assert updated["counters"]["files_touched"] == ["/tmp/a.py"]


def test_cli_force_resets_counters_for_new_message(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    first = _write_message(inbox, "msg-1")
    second = _write_message(inbox, "msg-2")
    assert _main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0

    target = envelope_path(tmp_path, "test-proj", "claude")
    envelope = load_envelope(target)
    envelope["counters"]["files_touched"] = ["/tmp/a.py"]
    target.write_text(json.dumps(envelope), encoding="utf-8")

    assert _main([
        "compile", str(second), "--oacp-dir", str(tmp_path), "--force",
    ]) == 0
    updated = load_envelope(target)
    assert updated["message_id"] == "msg-2"
    assert updated["counters"]["files_touched"] == []


def test_cli_compile_consumes_fresh_session_claim(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    target = envelope_path(tmp_path, "test-proj", "claude")
    write_session_claim(target, "sess-a", message_path.name)

    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    envelope = load_envelope(target)
    assert envelope["session_id"] == "sess-a"
    assert not session_claim_path(target, "sess-a").exists()


def test_cli_compile_binds_with_options_before_positional(
    tmp_path: Path, capsys
) -> None:
    """End-to-end for the option-order form: claim written, options-first
    compile invocation, binding preserved."""
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    target = envelope_path(tmp_path, "test-proj", "claude")
    write_session_claim(target, "sess-a", message_path.name)

    assert _main([
        "compile", "--receiver", "claude", "--oacp-dir", str(tmp_path),
        str(message_path),
    ]) == 0
    assert load_envelope(target)["session_id"] == "sess-a"


def test_cli_compile_racing_claims_from_two_sessions_compile_unbound(
    tmp_path: Path, capsys
) -> None:
    """The reviewed interleaving: session A claims, session B claims the same
    message before A's compiler runs. The compile must not bind to either —
    ambiguity degrades to unbound, never to a wrong binding."""
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    target = envelope_path(tmp_path, "test-proj", "claude")
    write_session_claim(target, "sess-a", message_path.name)
    write_session_claim(target, "sess-b", message_path.name)

    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["session_id"] is None
    assert not session_claim_path(target, "sess-a").exists()
    assert not session_claim_path(target, "sess-b").exists()


def test_cli_compile_same_session_double_claim_still_binds(
    tmp_path: Path, capsys
) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    target = envelope_path(tmp_path, "test-proj", "claude")
    write_session_claim(target, "sess-a", message_path.name)
    write_session_claim(target, "sess-a", message_path.name)

    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["session_id"] == "sess-a"


def test_cli_compile_without_claim_is_unbound(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)

    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert envelope["session_id"] is None


def test_cli_compile_ignores_mismatched_claim(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    target = envelope_path(tmp_path, "test-proj", "claude")
    write_session_claim(target, "sess-a", "some-other-message.yaml")

    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["session_id"] is None
    # A fresh claim for a different message belongs to the compile in
    # flight for that message — it must survive this consumption.
    assert session_claim_path(target, "sess-a").is_file()


def test_cli_compile_other_messages_claim_survives_and_binds_its_own(
    tmp_path: Path, capsys
) -> None:
    """Two sessions claim two different messages: each compile binds its own
    claimant, and neither consumption destroys the other's pending claim."""
    inbox = _workspace(tmp_path)
    first = _write_message(inbox, "msg-1")
    second = _write_message(inbox, "msg-2")
    target = envelope_path(tmp_path, "test-proj", "claude")
    write_session_claim(target, "sess-a", first.name)
    write_session_claim(target, "sess-b", second.name)

    assert _main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["session_id"] == "sess-a"
    assert session_claim_path(target, "sess-b").is_file()

    assert _main([
        "compile", str(second), "--oacp-dir", str(tmp_path), "--force",
    ]) == 0
    assert load_envelope(target)["session_id"] == "sess-b"
    assert not session_claim_path(target, "sess-b").exists()


def test_cli_compile_ignores_stale_claim(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    target = envelope_path(tmp_path, "test-proj", "claude")
    claim_file = session_claim_path(target, "sess-a")
    claim_file.parent.mkdir(parents=True, exist_ok=True)
    claim_file.write_text(
        json.dumps(
            {
                "session_id": "sess-a",
                "message_name": message_path.name,
                "claimed_at_utc": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["session_id"] is None


def test_cli_recompile_and_extend_preserve_session_binding(
    tmp_path: Path, capsys
) -> None:
    inbox = _workspace(tmp_path)
    first = _write_message(inbox, "msg-1")
    second = _write_message(inbox, "msg-2")
    target = envelope_path(tmp_path, "test-proj", "claude")
    write_session_claim(target, "sess-a", first.name)
    assert _main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["session_id"] == "sess-a"

    # Same-message recompile without a fresh claim (e.g. a human-run
    # --extend after re-authorization) keeps the binding.
    assert _main(["compile", str(first), "--oacp-dir", str(tmp_path)]) == 0
    assert load_envelope(target)["session_id"] == "sess-a"

    assert _main([
        "compile", str(second), "--oacp-dir", str(tmp_path), "--extend",
    ]) == 0
    assert load_envelope(target)["session_id"] == "sess-a"


def test_cli_compile_missing_profile_exits_3(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    path = inbox / "msg-np.yaml"
    path.write_text(
        yaml.safe_dump(make_message(body="No profile.", message_id="msg-np")),
        encoding="utf-8",
    )
    assert _main(["compile", str(path), "--oacp-dir", str(tmp_path)]) == 3
    assert ENVELOPE_COMPILE_ERROR in capsys.readouterr().err


def test_cli_compile_requires_project_when_not_inferable(
    tmp_path: Path, capsys
) -> None:
    _workspace(tmp_path)
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(yaml.safe_dump(make_message()), encoding="utf-8")
    assert _main(["compile", str(outside), "--oacp-dir", str(tmp_path)]) == 3
    assert "cannot infer project" in capsys.readouterr().err

    assert _main([
        "compile", str(outside), "--project", "test-proj", "--oacp-dir", str(tmp_path),
    ]) == 0


# ── CLI: enforcement stamp per adapter state ─────────────────────────────────


def _write_admission_record(
    tmp_path: Path,
    message_path: Path,
    *,
    message_id: str = "msg-1",
    receiver: str = "claude",
    message_sha256: Optional[str] = None,
    project: str = "test-proj",
) -> Path:
    """An auto-accepted admission record shaped like the gate's own output."""
    audit_dir = tmp_path / "projects" / project / "agents" / receiver / "audit" / "autonomy_decisions"
    audit_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 2,
        "message_id": message_id,
        "receiver": receiver,
        "decision": "auto_accepted",
        "message_sha256": message_sha256 or hashlib.sha256(message_path.read_bytes()).hexdigest(),
        "result": {
            "final_state": "pending",
            "completion_kind": "auto_accepted",
            "envelope_enforcement": "none",
        },
    }
    path = audit_dir / f"20260712T010000Z_{message_id}.yaml"
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return path


def test_cli_resolved_adapter_stamps_hooks_quietly(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert "enforcement: hooks" in out
    assert "ADVISORY" not in err
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert envelope["enforcement"] == "hooks"
    assert "enforcement_reason" not in envelope
    assert envelope["adapter"]["state"] == ADAPTER_RESOLVED


def test_cli_unsupported_runtime_stamps_none_without_advisory(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path, runtime="codex")
    message_path = _write_message(inbox)
    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert "enforcement: none (adapter_unsupported)" in out
    assert "ADVISORY" not in err
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert envelope["enforcement"] == "none"
    assert envelope["enforcement_reason"] == "adapter_unsupported"
    assert envelope["adapter"] == {
        "runtime": "codex",
        "state": ADAPTER_UNSUPPORTED,
        "console": None,
        "detail": "runtime 'codex' has no envelope adapter",
    }


@pytest.mark.parametrize(
    ("which_fn", "registration_fn"),
    [(_which_absent, _registered), (_which_present, _unregistered)],
    ids=["console-off-path", "unregistered"],
)
def test_cli_expected_missing_is_loud_but_still_compiles(
    tmp_path: Path, capsys, which_fn, registration_fn
) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    assert _main(
        ["compile", str(message_path), "--oacp-dir", str(tmp_path)],
        which_fn=which_fn,
        registration_fn=registration_fn,
    ) == 0
    out, err = capsys.readouterr()
    assert "ADVISORY (adapter_expected_missing):" in err
    assert "enforcement: none (adapter_expected_missing)" in out
    target = envelope_path(tmp_path, "test-proj", "claude")
    assert target.is_file()
    envelope = load_envelope(target)
    assert envelope["enforcement"] == "none"
    assert envelope["enforcement_reason"] == "adapter_expected_missing"
    assert envelope["adapter"]["state"] == ADAPTER_EXPECTED_MISSING
    assert envelope["adapter"]["console"] == "oacp-envelope-hook"


def test_cli_missing_agent_card_is_detection_failed(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path, runtime=None)
    message_path = _write_message(inbox)
    assert _main(["compile", str(message_path), "--oacp-dir", str(tmp_path)]) == 0
    _out, err = capsys.readouterr()
    assert "ADVISORY (adapter_detection_failed): agent card not found" in err
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert envelope["enforcement"] == "none"
    assert envelope["enforcement_reason"] == "adapter_detection_failed"
    assert envelope["adapter"]["runtime"] is None
    assert envelope["adapter"]["state"] == ADAPTER_FAILED


def test_cli_default_probe_reads_the_workspace_registration(tmp_path: Path, capsys) -> None:
    """No injected registration probe: the marker's repo_path is consulted."""
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    repo = _repo_with_settings(tmp_path, HOOK_SETTINGS)
    _write_workspace_marker(tmp_path, "test-proj", str(repo))
    assert _main(
        ["compile", str(message_path), "--oacp-dir", str(tmp_path)],
        registration_fn=None,
    ) == 0
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert envelope["enforcement"] == "hooks"

    # Strip the registration: the same compile is now expected_missing.
    (repo / ".claude" / "settings.json").unlink()
    assert _main(
        ["compile", str(message_path), "--oacp-dir", str(tmp_path)],
        registration_fn=None,
    ) == 0
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert envelope["enforcement"] == "none"
    assert envelope["enforcement_reason"] == "adapter_expected_missing"
    assert "not registered" in envelope["adapter"]["detail"]


def test_cli_default_probe_without_marker_is_detection_failed(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    assert _main(
        ["compile", str(message_path), "--oacp-dir", str(tmp_path)],
        registration_fn=None,
    ) == 0
    _out, err = capsys.readouterr()
    assert "ADVISORY (adapter_detection_failed): workspace marker not found" in err
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert envelope["enforcement_reason"] == "adapter_detection_failed"


def test_cli_json_output_carries_the_adapter_block(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path, runtime="codex")
    message_path = _write_message(inbox)
    assert _main([
        "compile", str(message_path), "--oacp-dir", str(tmp_path), "--json",
    ]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["enforcement"] == "none"
    assert envelope["enforcement_reason"] == "adapter_unsupported"
    assert envelope["adapter"]["state"] == ADAPTER_UNSUPPORTED


# ── CLI: --audit stamps the admission record with the same state ─────────────


def test_cli_audit_stamps_hooks_on_a_bound_record(tmp_path: Path, capsys) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    record = _write_admission_record(tmp_path, message_path)
    assert _main([
        "compile", str(message_path), "--oacp-dir", str(tmp_path), "--audit", str(record),
    ]) == 0
    audit = yaml.safe_load(record.read_text(encoding="utf-8"))
    assert audit["result"]["envelope_enforcement"] == "hooks"
    assert "envelope_enforcement_reason" not in audit["result"]
    # Untouched fields survive the locked rewrite.
    assert audit["decision"] == "auto_accepted"
    assert audit["result"]["completion_kind"] == "auto_accepted"


@pytest.mark.parametrize(
    ("runtime", "which_fn", "reason"),
    [
        ("codex", _which_present, "adapter_unsupported"),
        ("claude", _which_absent, "adapter_expected_missing"),
        (None, _which_present, "adapter_detection_failed"),
    ],
    ids=["unsupported", "expected-missing", "failed"],
)
def test_cli_audit_stamps_none_with_the_reason(
    tmp_path: Path, capsys, runtime, which_fn, reason
) -> None:
    inbox = _workspace(tmp_path, runtime=runtime)
    message_path = _write_message(inbox)
    record = _write_admission_record(tmp_path, message_path)
    assert _main(
        ["compile", str(message_path), "--oacp-dir", str(tmp_path), "--audit", str(record)],
        which_fn=which_fn,
    ) == 0
    audit = yaml.safe_load(record.read_text(encoding="utf-8"))
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert audit["result"]["envelope_enforcement"] == "none"
    assert audit["result"]["envelope_enforcement_reason"] == reason
    # The record and the envelope tell one story.
    assert audit["result"]["envelope_enforcement"] == envelope["enforcement"]
    assert audit["result"]["envelope_enforcement_reason"] == envelope["enforcement_reason"]


def test_cli_audit_hooks_clears_a_stale_none_reason(tmp_path: Path, capsys) -> None:
    """A recompile that resolves the adapter drops the earlier none reason."""
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    record = _write_admission_record(tmp_path, message_path)
    assert _main(
        ["compile", str(message_path), "--oacp-dir", str(tmp_path), "--audit", str(record)],
        which_fn=_which_absent,
    ) == 0
    audit = yaml.safe_load(record.read_text(encoding="utf-8"))
    assert audit["result"]["envelope_enforcement_reason"] == "adapter_expected_missing"

    assert _main([
        "compile", str(message_path), "--oacp-dir", str(tmp_path), "--audit", str(record),
    ]) == 0
    audit = yaml.safe_load(record.read_text(encoding="utf-8"))
    assert audit["result"]["envelope_enforcement"] == "hooks"
    assert "envelope_enforcement_reason" not in audit["result"]


@pytest.mark.parametrize(
    "mismatch",
    [
        {"message_id": "msg-other"},
        {"message_sha256": "0" * 64},
        {"receiver": "codex"},
    ],
    ids=["message-id", "snapshot-hash", "receiver"],
)
def test_cli_audit_leaves_an_unbound_record_alone(tmp_path: Path, capsys, mismatch) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    record = _write_admission_record(tmp_path, message_path, **mismatch)
    if mismatch.get("receiver"):
        # The path must still resolve inside claude's canonical audit dir.
        canonical = tmp_path / "projects" / "test-proj" / "agents" / "claude" / "audit" / "autonomy_decisions"
        canonical.mkdir(parents=True, exist_ok=True)
        moved = canonical / record.name
        moved.write_bytes(record.read_bytes())
        record = moved
    before = record.read_bytes()
    assert _main([
        "compile", str(message_path), "--oacp-dir", str(tmp_path), "--audit", str(record),
    ]) == 0
    _out, err = capsys.readouterr()
    assert "does not bind" in err
    assert record.read_bytes() == before
    # The envelope still compiled: an unbound record is a bookkeeping miss,
    # never a compile failure.
    assert load_envelope(envelope_path(tmp_path, "test-proj", "claude"))["enforcement"] == "hooks"


def test_cli_audit_outside_canonical_dir_fails_closed_for_private_tasks(
    tmp_path: Path, capsys
) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    stray = tmp_path / "elsewhere" / "record.yaml"
    stray.parent.mkdir()
    stray.write_text("schema_version: 2\n", encoding="utf-8")
    assert _main([
        "compile", str(message_path), "--oacp-dir", str(tmp_path), "--audit", str(stray),
    ]) == 3
    assert "canonical admission audit directory" in capsys.readouterr().err
    assert not envelope_path(tmp_path, "test-proj", "claude").exists()


# ── Round-1 review regressions ───────────────────────────────────────────────

UNDECODABLE_CARD = b"runtime: claude\n# comment \xff\n"


def test_read_receiver_runtime_undecodable_card_is_unreadable(tmp_path: Path) -> None:
    card = tmp_path / "projects" / "test-proj" / "agents" / "claude" / "agent_card.yaml"
    card.parent.mkdir(parents=True)
    card.write_bytes(UNDECODABLE_CARD)
    runtime, error = read_receiver_runtime(tmp_path, "test-proj", "claude")
    assert runtime is None
    assert error is not None and "agent card unreadable" in error


@pytest.mark.parametrize("with_audit", [False, True], ids=["no-audit", "audit"])
def test_cli_undecodable_card_is_detection_failed(
    tmp_path: Path, capsys, with_audit: bool
) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    (inbox.parent / "agent_card.yaml").write_bytes(UNDECODABLE_CARD)
    argv = ["compile", str(message_path), "--oacp-dir", str(tmp_path)]
    record = None
    if with_audit:
        record = _write_admission_record(tmp_path, message_path)
        argv += ["--audit", str(record)]
    assert _main(argv) == 0
    _out, err = capsys.readouterr()
    assert "ADVISORY (adapter_detection_failed): agent card unreadable" in err
    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    assert envelope["enforcement"] == "none"
    assert envelope["enforcement_reason"] == "adapter_detection_failed"
    assert envelope["adapter"]["state"] == ADAPTER_FAILED
    if record is not None:
        audit = yaml.safe_load(record.read_text(encoding="utf-8"))
        assert audit["result"]["envelope_enforcement"] == "none"
        assert audit["result"]["envelope_enforcement_reason"] == "adapter_detection_failed"


def test_cli_undecodable_card_recompile_replaces_the_resolved_envelope(
    tmp_path: Path, capsys
) -> None:
    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    argv = ["compile", str(message_path), "--oacp-dir", str(tmp_path)]
    assert _main(argv) == 0
    target = envelope_path(tmp_path, "test-proj", "claude")
    assert load_envelope(target)["enforcement"] == "hooks"

    (inbox.parent / "agent_card.yaml").write_bytes(UNDECODABLE_CARD)
    assert _main(argv) == 0
    envelope = load_envelope(target)
    assert envelope["enforcement"] == "none"
    assert envelope["adapter"]["state"] == ADAPTER_FAILED


def test_detect_receiver_adapter_never_raises(tmp_path: Path, monkeypatch) -> None:
    import envelope_compiler as module

    def exploding(*args: Any, **kwargs: Any):
        raise RuntimeError("card reader is on fire")

    monkeypatch.setattr(module, "read_receiver_runtime", exploding)
    detection = detect_receiver_adapter(
        tmp_path, "test-proj", "claude",
        which_fn=_which_present, registration_fn=_registered,
    )
    assert detection.state == ADAPTER_FAILED
    assert "card reader is on fire" in detection.detail


@pytest.mark.parametrize(
    ("first_which", "second_which"),
    [(_which_present, _which_absent), (_which_absent, _which_present)],
    ids=["resolved-then-missing", "missing-then-resolved"],
)
def test_cli_concurrent_compiles_keep_envelope_and_record_in_step(
    tmp_path: Path, monkeypatch, first_which, second_which
) -> None:
    """Two successful compiles of one message. The first is paused between
    its envelope write and its audit stamp while the second runs as far as
    it can. Whatever the interleaving, the envelope and the record must end
    up naming the same enforcement state and reason."""
    import threading

    import envelope_compiler as module

    inbox = _workspace(tmp_path)
    message_path = _write_message(inbox)
    record = _write_admission_record(tmp_path, message_path)
    argv = [
        "compile", str(message_path), "--oacp-dir", str(tmp_path),
        "--audit", str(record),
    ]

    first_paused = threading.Event()
    second_done = threading.Event()
    original_stamp = module._stamp_adapter_enforcement

    def paused_stamp(path: Path, **kwargs: Any) -> bool:
        if threading.current_thread().name == "first-compile":
            first_paused.set()
            # Under the required lock order the second compile cannot finish
            # while the first still holds the envelope lock, so this wait
            # times out by design; with the stamp outside the lock it returns
            # at once and the two stamps cross.
            second_done.wait(1.0)
        return original_stamp(path, **kwargs)

    monkeypatch.setattr(module, "_stamp_adapter_enforcement", paused_stamp)
    results: Dict[str, int] = {}

    def first() -> None:
        results["first"] = _main(argv, which_fn=first_which)

    thread = threading.Thread(target=first, name="first-compile")
    thread.start()
    assert first_paused.wait(10)
    results["second"] = _main(argv, which_fn=second_which)
    second_done.set()
    thread.join(10)
    assert not thread.is_alive()
    assert results == {"first": 0, "second": 0}

    envelope = load_envelope(envelope_path(tmp_path, "test-proj", "claude"))
    audit = yaml.safe_load(record.read_text(encoding="utf-8"))
    assert audit["result"]["envelope_enforcement"] == envelope["enforcement"]
    assert audit["result"].get("envelope_enforcement_reason") == envelope.get(
        "enforcement_reason"
    )
    # The second compile is the last writer of both artifacts.
    expected = "hooks" if second_which is _which_present else "none"
    assert envelope["enforcement"] == expected
