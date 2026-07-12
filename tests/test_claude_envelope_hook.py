# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/claude_envelope_hook.py with recorded tool_input shapes."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import claude_envelope_hook as hook  # noqa: E402
from envelope_compiler import envelope_path, load_envelope, write_envelope  # noqa: E402


CONSTRAINTS: Dict[str, Any] = {
    "estimated_minutes": 30,
    "expected_files_touched": 2,
    "risk_tier": "P2",
    "target_repo": "example-org/private-repo",
    "destructive_ops": False,
    "external_side_effects": True,
    "creates_or_updates_pr": True,
    "comments_on_github": False,
    "commits_changes": True,
    "sends_oacp_reply_only": False,
    "touches_auth_config_or_secrets": False,
    "touches_dependencies": False,
    "public_visibility": False,
    "private_repo_allowlist": ["example-org/private-repo"],
}


def make_envelope(**overrides: Any) -> Dict[str, Any]:
    constraints = dict(CONSTRAINTS)
    constraints.update(overrides)
    return {
        "envelope_version": 1,
        "spec_version": "0.3.5",
        "compiler": "envelope_compiler.py",
        "compiled_at_utc": "2026-07-12T02:00:00Z",
        "project": "test-proj",
        "receiver": "claude",
        "message_id": "msg-1",
        "message_sha256": "0" * 64,
        "constraints": constraints,
        "counters": {"files_touched": []},
        "enforcement": "hooks",
    }


def bash(command: str, envelope: Optional[Dict[str, Any]] = None) -> hook.Decision:
    envelope = envelope or make_envelope()
    return hook.classify("Bash", {"command": command}, "/repo", envelope)


@pytest.fixture(autouse=True)
def _pin_repo_resolution(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        hook, "resolve_cwd_repo", lambda *a, **k: "example-org/private-repo"
    )
    monkeypatch.setattr(
        hook, "resolve_current_branch", lambda *a, **k: "feat/widget"
    )


# ── Bash: destructive tokens ─────────────────────────────────────────────────


def test_destructive_rm_rf_denied() -> None:
    decision = bash("rm -rf build/")
    assert decision.action == "deny"
    assert "rm -rf" in decision.reason


def test_destructive_force_flag_denied() -> None:
    assert bash("git push --force origin feat/x").action == "deny"


def test_no_verify_denied() -> None:
    assert bash("git commit --no-verify -m x").action == "deny"


def test_plain_commands_allowed() -> None:
    assert bash("ls -la").action == "allow"
    assert bash("pytest tests/ -x").action == "allow"
    assert bash("make preflight").action == "allow"


# ── Bash: git ────────────────────────────────────────────────────────────────


def test_git_commit_allowed_when_declared() -> None:
    assert bash("git commit -m 'add widget'").action == "allow"


def test_git_commit_denied_when_not_declared() -> None:
    decision = bash("git commit -m x", make_envelope(commits_changes=False))
    assert decision.action == "deny"
    assert "commits_changes" in decision.reason


def test_git_push_feature_branch_allowed() -> None:
    assert bash("git push -u origin feat/widget").action == "allow"


def test_git_push_to_main_denied() -> None:
    decision = bash("git push origin HEAD:main")
    assert decision.action == "deny"
    assert "protected branch" in decision.reason


def test_git_push_head_refspec_checks_current_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook, "resolve_current_branch", lambda *a, **k: "main")
    assert bash("git push origin HEAD").action == "deny"


def test_git_push_denied_without_pr_declaration() -> None:
    decision = bash(
        "git push origin feat/x", make_envelope(creates_or_updates_pr=False)
    )
    assert decision.action == "deny"
    assert "creates_or_updates_pr" in decision.reason


def test_git_push_denied_without_external_side_effects() -> None:
    decision = bash(
        "git push origin feat/x",
        make_envelope(external_side_effects=False, creates_or_updates_pr=False),
    )
    assert decision.action == "deny"


def test_git_push_repo_mismatch_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "resolve_cwd_repo", lambda *a, **k: "other/repo")
    decision = bash("git push origin feat/x")
    assert decision.action == "deny"
    assert "other/repo" in decision.reason


def test_git_push_unresolvable_repo_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "resolve_cwd_repo", lambda *a, **k: None)
    assert bash("git push origin feat/x").action == "ask"


def test_git_readonly_allowed() -> None:
    assert bash("git status && git diff --stat").action == "allow"
    assert bash("git log --oneline -5").action == "allow"


# ── Bash: gh ─────────────────────────────────────────────────────────────────


def test_gh_pr_create_allowed_on_allowlisted_repo() -> None:
    decision = bash(
        "gh pr create -R example-org/private-repo --title x --body y"
    )
    assert decision.action == "allow"


def test_gh_pr_create_denied_on_unlisted_repo() -> None:
    decision = bash("gh pr create -R other/repo --title x --body y")
    assert decision.action == "deny"
    assert "other/repo" in decision.reason


def test_gh_pr_create_denied_when_not_declared() -> None:
    decision = bash(
        "gh pr create -R example-org/private-repo --title x",
        make_envelope(creates_or_updates_pr=False),
    )
    assert decision.action == "deny"


def test_gh_pr_comment_gated_by_comments_flag() -> None:
    denied = bash("gh pr comment 12 --body hi")
    assert denied.action == "deny"
    assert "comments_on_github" in denied.reason
    allowed = bash(
        "gh pr comment 12 --body hi", make_envelope(comments_on_github=True)
    )
    assert allowed.action == "allow"


def test_gh_pr_merge_always_denied() -> None:
    decision = bash("gh pr merge 12 --squash")
    assert decision.action == "deny"
    assert "allow class" in decision.reason


def test_gh_issue_create_denied() -> None:
    assert bash("gh issue create --title x").action == "deny"


def test_gh_release_denied() -> None:
    assert bash("gh release create v1.0.0").action == "deny"


def test_gh_readonly_allowed() -> None:
    assert bash("gh pr view 12 --json state").action == "allow"
    assert bash("gh pr checks 12").action == "allow"
    assert bash("gh issue list --state open").action == "allow"
    assert bash("gh auth status").action == "allow"


def test_gh_api_get_allowed_post_asks() -> None:
    assert bash("gh api repos/example-org/private-repo/pulls").action == "allow"
    assert bash("gh api -X POST repos/example-org/private-repo/pulls").action == "ask"


def test_gh_auth_login_denied() -> None:
    decision = bash("gh auth login")
    assert decision.action == "deny"
    assert "auth" in decision.reason


def test_public_visibility_true_denies_mutations() -> None:
    decision = bash(
        "gh pr create -R example-org/private-repo --title x",
        make_envelope(public_visibility=True),
    )
    assert decision.action == "deny"


# ── Bash: dependencies, secrets, escalation ──────────────────────────────────


def test_pip_install_denied() -> None:
    decision = bash("pip install requests")
    assert decision.action == "deny"
    assert "dependencies" in decision.reason


def test_uv_and_npm_variants_denied() -> None:
    assert bash("uv add httpx").action == "deny"
    assert bash("uv pip install httpx").action == "deny"
    assert bash("npm install left-pad").action == "deny"
    assert bash("python3 -m pip install requests").action == "deny"


def test_dependency_install_allowed_when_declared() -> None:
    decision = bash("pip install requests", make_envelope(touches_dependencies=True))
    assert decision.action == "allow"


def test_pip_list_allowed() -> None:
    assert bash("pip list").action == "allow"


def test_redirect_to_secret_path_denied() -> None:
    decision = bash("echo TOKEN=x > .env")
    assert decision.action == "deny"
    assert ".env" in decision.reason


def test_redirect_to_plain_path_allowed_and_counted() -> None:
    decision = bash("echo hello > notes.txt")
    assert decision.action == "allow"
    assert decision.new_files == ["/repo/notes.txt"]


def test_redirect_to_dev_null_not_counted() -> None:
    decision = bash("make test > /dev/null")
    assert decision.action == "allow"
    assert decision.new_files == []


def test_cp_to_ssh_dir_denied() -> None:
    assert bash("cp key ~/.ssh/id_rsa").action == "deny"


def test_oacp_send_always_allowed() -> None:
    decision = bash(
        "oacp send test-proj --from claude --to iris --type notification "
        "--subject done --body done",
        make_envelope(external_side_effects=False),
    )
    assert decision.action == "allow"


def test_compound_command_deny_wins() -> None:
    assert bash("ls && gh pr merge 12").action == "deny"


def test_unbalanced_quotes_ask() -> None:
    assert bash("echo 'unclosed").action == "ask"


def test_sudo_asks() -> None:
    assert bash("sudo systemctl restart nginx").action == "ask"


# ── Round-2 regressions (codex findings F-001..F-004) ────────────────────────


def test_oacp_envelope_clear_denied() -> None:
    decision = bash("oacp envelope clear --project test-proj")
    assert decision.action == "deny"
    assert "self" in decision.reason or "envelope" in decision.reason


def test_oacp_envelope_compile_denied() -> None:
    assert bash("oacp envelope compile msg.yaml --extend").action == "deny"


def test_oacp_envelope_show_allowed() -> None:
    assert bash("oacp envelope show --project test-proj").action == "allow"


def test_oacp_memory_push_asks() -> None:
    assert bash("oacp memory push").action == "ask"


def test_oacp_readonly_subcommands_allowed() -> None:
    assert bash("oacp inbox test-proj --agent claude").action == "allow"
    assert bash("oacp validate msg.yaml").action == "allow"
    assert bash("oacp doctor").action == "allow"


def test_newline_separated_mutation_denied() -> None:
    assert bash("ls\ngh pr merge 162 --squash").action == "deny"


def test_background_separated_mutation_denied() -> None:
    assert bash("true & gh pr merge 162 --squash").action == "deny"


def test_shell_indirection_asks() -> None:
    assert bash("bash -c 'gh pr merge 162 --squash'").action == "ask"
    assert bash("xargs -I{} sh -c '{}'").action == "ask"


def test_wrapper_with_flags_asks() -> None:
    assert bash("env -i gh pr merge 162 --squash").action == "ask"


def test_command_substitution_content_classified() -> None:
    assert bash("echo $(gh pr merge 12)").action == "deny"
    assert bash("echo `oacp envelope clear`").action == "deny"


def test_gh_api_implicit_post_asks() -> None:
    decision = bash("gh api repos/example-org/private-repo/pulls/162 -f state=closed")
    assert decision.action == "ask"


def test_gh_global_repo_flag_before_group_denied() -> None:
    decision = bash("gh --repo example-org/private-repo pr merge 162 --squash")
    assert decision.action == "deny"


def test_gh_unknown_mutation_asks() -> None:
    assert bash("gh run delete 123").action == "ask"


def test_git_push_mirror_denied() -> None:
    decision = bash("git push --mirror origin")
    assert decision.action == "deny"
    assert "--mirror" in decision.reason


def test_git_push_force_prefixed_main_refspec_denied() -> None:
    assert bash("git push origin +main").action == "deny"


def test_bash_redirect_to_dependency_manifest_denied() -> None:
    decision = bash("echo hi > package.json")
    assert decision.action == "deny"
    assert "package.json" in decision.reason


def test_sed_in_place_on_secret_denied() -> None:
    decision = bash("sed -i s/x/y/ .env")
    assert decision.action == "deny"
    assert ".env" in decision.reason


def test_touch_counts_against_file_counter() -> None:
    decision = bash("touch a b c")  # expected_files_touched: 2
    assert decision.action == "deny"
    assert decision.reason.startswith(hook.BLOCKED_OPENER)
    assert "expected 2, now 3" in decision.reason


def test_bash_writes_accumulate_counter() -> None:
    decision = bash("touch a b")
    assert decision.action == "allow"
    assert decision.new_files == ["/repo/a", "/repo/b"]


# ── Round-3 regressions (codex findings F-005..F-007) ────────────────────────


def test_redirect_on_recognized_gh_command_gated() -> None:
    decision = bash("gh pr view 162 > .env")
    assert decision.action == "deny"
    assert ".env" in decision.reason


def test_redirect_on_recognized_git_command_gated() -> None:
    decision = bash("git status > package.json")
    assert decision.action == "deny"
    assert "package.json" in decision.reason


def test_redirects_on_oacp_commands_count_toward_ceiling() -> None:
    decision = bash("oacp doctor > a && oacp doctor > b && oacp doctor > c")
    assert decision.action == "deny"
    assert decision.reason.startswith(hook.BLOCKED_OPENER)
    assert "expected 2, now 3" in decision.reason


def test_gh_attached_short_repo_flag_feeds_repo_gate() -> None:
    decision = bash("gh pr create -Rother/repo --title x")
    assert decision.action == "deny"
    assert "other/repo" in decision.reason


def test_gh_auth_switch_denied() -> None:
    decision = bash("gh auth switch --hostname github.com")
    assert decision.action == "deny"
    assert "auth" in decision.reason


def test_git_push_wildcard_refspec_denied() -> None:
    decision = bash("git push origin 'refs/heads/*:refs/heads/*'")
    assert decision.action == "deny"
    assert "wildcard" in decision.reason


def test_uv_run_recurses_into_nested_command() -> None:
    decision = bash("uv run oacp envelope clear --project test-proj")
    assert decision.action == "deny"
    assert "envelope" in decision.reason


def test_uv_run_plain_nested_command_allowed() -> None:
    assert bash("uv run pytest tests/ -x").action == "allow"


def test_source_and_dot_ask() -> None:
    assert bash("source ./setup.sh").action == "ask"
    assert bash(". ./disable-envelope.sh").action == "ask"


def test_interpreter_inline_code_asks() -> None:
    py = 'python3 -c "print(1)"'
    assert bash(py).action == "ask"
    assert bash("node -e 'console.log(1)'").action == "ask"


def test_python_module_form_still_classified_not_asked() -> None:
    # `-m pip install` is dependency-classified, not inline-code escalated.
    assert bash("python3 -m pip install requests").action == "deny"
    assert bash("python3 -m pytest tests/").action == "allow"


# ── File tools ───────────────────────────────────────────────────────────────


def edit(path: str, envelope: Optional[Dict[str, Any]] = None) -> hook.Decision:
    envelope = envelope or make_envelope()
    return hook.classify("Edit", {"file_path": path}, "/repo", envelope)


def test_secret_paths_denied() -> None:
    for path in (
        "/repo/.env",
        "/repo/.env.production",
        "/home/u/.ssh/id_rsa",
        "/repo/server.pem",
        "/repo/signing.key",
        "/oacp/projects/p/agents/claude/config.yaml",
        "/repo/aws_credentials.json",
    ):
        assert edit(path).action == "deny", path


def test_dependency_manifests_denied() -> None:
    for path in (
        "/repo/pyproject.toml",
        "/repo/package.json",
        "/repo/requirements-dev.txt",
        "/repo/uv.lock",
    ):
        assert edit(path).action == "deny", path


def test_dependency_manifest_allowed_when_declared() -> None:
    decision = edit(
        "/repo/pyproject.toml", make_envelope(touches_dependencies=True)
    )
    assert decision.action == "allow"


def test_file_counter_drift_denied_with_canonical_opener() -> None:
    envelope = make_envelope()  # expected_files_touched: 2
    envelope["counters"]["files_touched"] = ["/repo/a.py", "/repo/b.py"]
    decision = edit("/repo/c.py", envelope)
    assert decision.action == "deny"
    assert decision.reason.startswith(hook.BLOCKED_OPENER)
    assert "expected 2, now 3" in decision.reason


def test_file_counter_records_new_files() -> None:
    decision = edit("/repo/a.py")
    assert decision.action == "allow"
    assert decision.new_files == ["/repo/a.py"]


def test_recounted_file_not_recorded_twice() -> None:
    envelope = make_envelope()
    envelope["counters"]["files_touched"] = ["/repo/a.py"]
    decision = edit("/repo/a.py", envelope)
    assert decision.action == "allow"
    assert decision.new_files == []


def test_relative_paths_normalized_against_cwd() -> None:
    envelope = make_envelope()
    decision = hook.classify("Edit", {"file_path": "a.py"}, "/repo", envelope)
    assert decision.new_files == ["/repo/a.py"]


def test_write_and_notebook_share_classification() -> None:
    assert (
        hook.classify("Write", {"file_path": "/repo/.env"}, "/repo", make_envelope())
        .action
        == "deny"
    )
    assert (
        hook.classify(
            "NotebookEdit", {"notebook_path": "/repo/nb.ipynb"}, "/repo", make_envelope()
        ).action
        == "allow"
    )


def test_unknown_tool_allowed() -> None:
    assert hook.classify("Grep", {}, "/repo", make_envelope()).action == "allow"


# ── End-to-end: process() + main() ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _hermetic_oacp_home(monkeypatch: pytest.MonkeyPatch):
    """Keep a developer's real $OACP_HOME out of marker discovery."""
    monkeypatch.delenv("OACP_HOME", raising=False)


def _make_workspace(tmp_path: Path) -> Path:
    """Create an OACP home + repo dir with a .oacp marker symlink."""
    project_dir = tmp_path / "home" / "projects" / "test-proj"
    project_dir.mkdir(parents=True)
    marker = project_dir / "workspace.json"
    marker.write_text(
        json.dumps({"project_name": "test-proj"}), encoding="utf-8"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".oacp").symlink_to(marker)
    return repo


def _install_envelope(tmp_path: Path, envelope: Dict[str, Any]) -> Path:
    target = envelope_path(tmp_path / "home", "test-proj", "claude")
    write_envelope(target, envelope)
    return target


def test_process_no_marker_allows(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    decision = hook.process(
        {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}, "cwd": str(plain)}
    )
    assert decision.action == "allow"


def test_process_no_envelope_allows(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    decision = hook.process(
        {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}, "cwd": str(repo)}
    )
    assert decision.action == "allow"


def test_process_enforces_and_persists_counters(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    target = _install_envelope(tmp_path, make_envelope())

    denied = hook.process(
        {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}, "cwd": str(repo)}
    )
    assert denied.action == "deny"

    allowed = hook.process(
        {"tool_name": "Write", "tool_input": {"file_path": "a.py"}, "cwd": str(repo)}
    )
    assert allowed.action == "allow"
    stored = load_envelope(target)
    assert stored["counters"]["files_touched"] == [str(Path(repo) / "a.py")]


def test_main_emits_deny_json(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr merge 1"},
        "cwd": str(repo),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_allow_is_silent(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(repo),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main([]) == 0
    assert capsys.readouterr().out == ""


def test_main_corrupt_envelope_asks(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_workspace(tmp_path)
    target = envelope_path(tmp_path / "home", "test-proj", "claude")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(repo),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_main_malformed_stdin_asks(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hook.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
