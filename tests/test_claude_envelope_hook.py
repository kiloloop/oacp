# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/claude_envelope_hook.py with recorded tool_input shapes."""

from __future__ import annotations

import io
import json
import os
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


@pytest.fixture(autouse=True)
def _pin_scratchpad_prefixes(monkeypatch: pytest.MonkeyPatch):
    """Pin the scratchpad roots to a neutral value: pytest's own tmp dirs can
    live under the runtime scratchpad (sandboxed $TMPDIR), which would exempt
    every fixture path from the counter and invert the counter tests."""
    monkeypatch.setattr(hook, "SCRATCHPAD_PREFIXES", ("/scratchpad/",))


@pytest.fixture(autouse=True)
def _clear_ambient_gh_env(monkeypatch: pytest.MonkeyPatch):
    """Ambient GH_REPO/GH_HOST now feed gh classification — clear them so
    the developer's or CI runner's environment cannot flip gh tests."""
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)


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


def test_gh_pr_merge_denied_when_not_declared() -> None:
    decision = bash("gh pr merge 12 --squash")
    assert decision.action == "deny"
    assert "merges_pr" in decision.reason


def test_gh_pr_merge_allowed_when_declared() -> None:
    envelope = make_envelope(merges_pr=True)
    assert bash("gh pr merge 12 --squash", envelope).action == "allow"


def test_gh_pr_merge_declared_still_repo_gated() -> None:
    envelope = make_envelope(merges_pr=True, target_repo="example-org/other")
    decision = bash("gh pr merge 12 --squash", envelope)
    assert decision.action == "deny"
    assert "target_repo" in decision.reason


def test_gh_issue_create_denied_when_not_declared() -> None:
    decision = bash("gh issue create --title x")
    assert decision.action == "deny"
    assert "files_issues" in decision.reason


def test_gh_issue_lifecycle_allowed_when_declared() -> None:
    envelope = make_envelope(files_issues=True)
    assert bash("gh issue create --title x", envelope).action == "allow"
    assert bash("gh issue edit 7 --add-label bug", envelope).action == "allow"
    assert bash("gh issue close 7", envelope).action == "allow"
    assert bash("gh label create triage", envelope).action == "allow"


def test_gh_issue_destructive_verbs_stay_denied_even_declared() -> None:
    envelope = make_envelope(files_issues=True)
    assert bash("gh issue delete 7", envelope).action == "deny"
    assert bash("gh issue transfer 7 example-org/other", envelope).action == "deny"
    assert bash("gh label delete triage", envelope).action == "deny"


def test_gh_merge_and_issue_capabilities_are_independent() -> None:
    merge_only = make_envelope(merges_pr=True)
    assert bash("gh issue create --title x", merge_only).action == "deny"
    issues_only = make_envelope(files_issues=True)
    assert bash("gh pr merge 12 --squash", issues_only).action == "deny"


# The repo gate must judge the repository gh will actually mutate: URL
# positionals and repeated -R/--repo flags can retarget the command.


def test_gh_pr_merge_cross_repo_url_positional_denied() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "gh pr merge https://github.com/other-org/public-repo/pull/7 --squash",
        envelope,
    )
    assert decision.action == "deny"
    assert "other-org/public-repo" in decision.reason


def test_gh_issue_edit_cross_repo_url_positional_denied() -> None:
    envelope = make_envelope(files_issues=True)
    decision = bash(
        "gh issue edit https://github.com/other-org/public-repo/issues/9 "
        "--title changed",
        envelope,
    )
    assert decision.action == "deny"
    assert "other-org/public-repo" in decision.reason


def test_gh_pr_merge_same_repo_url_positional_allowed() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "gh pr merge https://github.com/example-org/private-repo/pull/7 "
        "--squash",
        envelope,
    )
    assert decision.action == "allow"


def test_gh_pr_merge_duplicated_repo_flags_escalate() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "gh pr merge 7 --repo example-org/private-repo "
        "--repo other-org/public-repo --squash",
        envelope,
    )
    assert decision.action == "ask"
    assert "conflicting repository selectors" in decision.reason


def test_gh_pr_merge_url_flag_conflict_escalates() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "gh pr merge https://github.com/other-org/public-repo/pull/7 "
        "-R example-org/private-repo --squash",
        envelope,
    )
    assert decision.action == "ask"


def test_gh_pr_merge_non_github_url_escalates() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "gh pr merge https://ghe.example.com/o/r/pull/7 --squash", envelope
    )
    assert decision.action == "ask"
    assert "cannot be resolved" in decision.reason


def test_gh_pr_merge_agreeing_selectors_still_repo_gated() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "gh pr merge https://github.com/other-org/public-repo/pull/7 "
        "-R other-org/public-repo --squash",
        envelope,
    )
    assert decision.action == "deny"
    assert "other-org/public-repo" in decision.reason


def test_gh_pr_comment_cross_repo_url_positional_denied() -> None:
    envelope = make_envelope(comments_on_github=True)
    decision = bash(
        "gh pr comment https://github.com/other-org/public-repo/pull/7 "
        "--body hi",
        envelope,
    )
    assert decision.action == "deny"


def test_gh_pr_merge_branch_positional_uses_cwd_repo() -> None:
    envelope = make_envelope(merges_pr=True)
    assert bash("gh pr merge feat/widget --squash", envelope).action == "allow"


# GH_REPO/GH_HOST assignments and earlier compound-segment shell state can
# retarget gh after the cwd-based gate approved it.


def test_gh_repo_assignment_cross_repo_denied() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "GH_REPO=other-org/public-repo gh pr merge 7 --squash", envelope
    )
    assert decision.action == "deny"
    assert "other-org/public-repo" in decision.reason


def test_gh_repo_assignment_env_wrapper_cross_repo_denied() -> None:
    envelope = make_envelope(files_issues=True)
    decision = bash(
        "env GH_REPO=other-org/public-repo gh issue edit 9 --title changed",
        envelope,
    )
    assert decision.action == "deny"
    assert "other-org/public-repo" in decision.reason


def test_gh_repo_assignment_matching_repo_allowed() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "GH_REPO=example-org/private-repo gh pr merge 7 --squash", envelope
    )
    assert decision.action == "allow"


def test_gh_repo_assignment_conflicting_flag_escalates() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "GH_REPO=other-org/public-repo gh pr merge 7 "
        "-R example-org/private-repo --squash",
        envelope,
    )
    assert decision.action == "ask"
    assert "conflicting repository selectors" in decision.reason


def test_gh_repo_assignment_host_prefixed_github_parsed() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "GH_REPO=github.com/example-org/private-repo gh pr merge 7 --squash",
        envelope,
    )
    assert decision.action == "allow"


def test_gh_repo_assignment_foreign_host_escalates() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "GH_REPO=ghe.example.com/o/r gh pr merge 7 --squash", envelope
    )
    assert decision.action == "ask"


def test_gh_host_assignment_escalates() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "GH_HOST=ghe.example.com gh pr merge 7 --squash", envelope
    )
    assert decision.action == "ask"
    assert "GH_HOST" in decision.reason


def test_gh_mutation_after_cd_segment_escalates() -> None:
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "cd /checkout/of/other-org/public-repo && gh pr merge 7 --squash",
        envelope,
    )
    assert decision.action == "ask"
    assert "compound" in decision.reason


def test_gh_mutation_after_export_segment_escalates() -> None:
    envelope = make_envelope(files_issues=True)
    decision = bash(
        "export GH_REPO=other-org/public-repo; gh issue edit 9 --title changed",
        envelope,
    )
    assert decision.action == "ask"


def test_gh_readonly_in_compound_still_allowed() -> None:
    assert bash("git status && gh pr view 12 --json state").action == "allow"


# Ambient GH_REPO/GH_HOST inherited by the Bash child retarget gh exactly
# like inline assignments — the hook must gate the effective environment.


def test_ambient_gh_repo_cross_repo_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_REPO", "other-org/public-repo")
    envelope = make_envelope(merges_pr=True)
    decision = bash("gh pr merge 7 --squash", envelope)
    assert decision.action == "deny"
    assert "other-org/public-repo" in decision.reason


def test_ambient_gh_repo_matching_repo_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_REPO", "example-org/private-repo")
    envelope = make_envelope(merges_pr=True)
    assert bash("gh pr merge 7 --squash", envelope).action == "allow"


def test_ambient_gh_host_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_HOST", "github.com")
    envelope = make_envelope(merges_pr=True)
    decision = bash("gh pr merge 7 --squash", envelope)
    assert decision.action == "ask"
    assert "GH_HOST" in decision.reason


def test_inline_gh_repo_overrides_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shell precedence: the inline assignment is what the child sees.
    monkeypatch.setenv("GH_REPO", "other-org/public-repo")
    envelope = make_envelope(merges_pr=True)
    decision = bash(
        "GH_REPO=example-org/private-repo gh pr merge 7 --squash", envelope
    )
    assert decision.action == "allow"


def test_ambient_gh_repo_unparseable_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_REPO", "ghe.example.com/o/r")
    envelope = make_envelope(files_issues=True)
    decision = bash("gh issue edit 9 --title changed", envelope)
    assert decision.action == "ask"
    assert "GH_REPO" in decision.reason


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


# ── Completion clear: audit-sanctioned envelope exit ─────────────────────────


CLEAR_CMD = "oacp envelope clear --project test-proj --receiver claude"


def _write_audit_record(
    tmp_path: Path,
    message_id: str = "msg-1",
    final_state: str = "done",
    stamp: str = "20260713T000000Z",
    receiver: str = "claude",
    filename_id: Optional[str] = None,
    body: Optional[str] = None,
) -> Path:
    audit_dir = (
        tmp_path
        / "home"
        / "projects"
        / "test-proj"
        / "agents"
        / "claude"
        / "audit"
        / "autonomy_decisions"
    )
    audit_dir.mkdir(parents=True, exist_ok=True)
    record = audit_dir / f"{stamp}_{filename_id or message_id}.yaml"
    if body is None:
        body = (
            f"message_id: {message_id}\n"
            f"receiver: {receiver}\n"
            f"result:\n  final_state: {final_state}\n"
        )
    record.write_text(body, encoding="utf-8")
    return record


def _process_bash(repo: Path, command: str) -> hook.Decision:
    return hook.process(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    )


def _process_write(repo: Path, file_path: str) -> hook.Decision:
    return hook.process(
        {"tool_name": "Write", "tool_input": {"file_path": file_path}, "cwd": str(repo)}
    )


def test_envelope_clear_denied_without_audit_record(tmp_path: Path) -> None:
    # Regression: the documented completion step used to be blanket-denied,
    # stranding the envelope; it must still deny while nothing sanctions it.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "deny"
    assert "audit record" in decision.reason


def test_envelope_clear_denied_while_audit_pending(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="pending")
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "deny"
    assert "pending" in decision.reason


def test_envelope_clear_denied_while_audit_paused(tmp_path: Path) -> None:
    # A checkpoint-paused task re-authorizes via compile --extend, not clear.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="paused")
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "deny"
    assert "paused" in decision.reason


@pytest.mark.parametrize("state", ["done", "error"])
def test_envelope_clear_allowed_after_terminal_audit(
    tmp_path: Path, state: str
) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state=state)
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "allow"


def test_envelope_clear_uses_newest_audit_record(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done", stamp="20260713T000000Z")
    _write_audit_record(tmp_path, final_state="paused", stamp="20260714T000000Z")
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "deny"


def test_envelope_clear_other_project_asks(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    decision = _process_bash(repo, "oacp envelope clear --project other-proj")
    assert decision.action == "ask"


def test_envelope_clear_other_receiver_asks(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    decision = _process_bash(
        repo, "oacp envelope clear --project test-proj --receiver codex"
    )
    assert decision.action == "ask"


def test_envelope_clear_matching_oacp_dir_allowed(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    home = tmp_path / "home"
    decision = _process_bash(repo, f"{CLEAR_CMD} --oacp-dir {home}")
    assert decision.action == "allow"


def test_envelope_clear_foreign_oacp_dir_asks(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    decision = _process_bash(repo, f"{CLEAR_CMD} --oacp-dir /somewhere/else")
    assert decision.action == "ask"


def test_envelope_clear_corrupt_audit_asks(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, body="- not\n- a mapping\n")
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "ask"


def test_envelope_compile_still_denied_with_terminal_audit(tmp_path: Path) -> None:
    # Completion sanctions the exit only; recompilation stays checkpoint-only.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    decision = _process_bash(repo, "oacp envelope compile msg.yaml --extend")
    assert decision.action == "deny"


# ── Bookkeeping surfaces: exempt from the file counter ───────────────────────


def _exhausted_envelope() -> Dict[str, Any]:
    envelope = make_envelope()  # expected_files_touched: 2
    envelope["counters"]["files_touched"] = ["/repo/a.py", "/repo/b.py"]
    return envelope


def _agent_dir(tmp_path: Path) -> Path:
    return tmp_path / "home" / "projects" / "test-proj" / "agents" / "claude"


def test_audit_write_exempt_from_file_counter(tmp_path: Path) -> None:
    # Regression (soak shape): tight honest declare, budget already consumed,
    # then the mandatory completion audit write — must not deny or count.
    repo = _make_workspace(tmp_path)
    target = _install_envelope(tmp_path, _exhausted_envelope())
    audit_path = (
        _agent_dir(tmp_path) / "audit" / "autonomy_decisions" / "x_msg-1.yaml"
    )
    decision = _process_write(repo, str(audit_path))
    assert decision.action == "allow"
    stored = load_envelope(target)
    assert stored["counters"]["files_touched"] == ["/repo/a.py", "/repo/b.py"]


def test_scratchpad_write_exempt_from_file_counter(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    target = _install_envelope(tmp_path, _exhausted_envelope())
    decision = _process_write(repo, "/scratchpad/reply-body.md")
    assert decision.action == "allow"
    stored = load_envelope(target)
    assert stored["counters"]["files_touched"] == ["/repo/a.py", "/repo/b.py"]


def test_inbox_outbox_writes_exempt(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    target = _install_envelope(tmp_path, _exhausted_envelope())
    agent = _agent_dir(tmp_path)
    for path in (
        agent / "inbox" / "20260713_iris_task.yaml",
        agent / "outbox" / "20260713_claude_reply.yaml",
    ):
        decision = _process_write(repo, str(path))
        assert decision.action == "allow", path
    stored = load_envelope(target)
    assert stored["counters"]["files_touched"] == ["/repo/a.py", "/repo/b.py"]


def test_envelope_state_write_denied_even_with_budget(tmp_path: Path) -> None:
    # The active envelope is the policy object: direct writes are
    # self-modification, categorically — not an uncounted bookkeeping write.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())  # budget NOT exhausted
    decision = _process_write(
        repo, str(_agent_dir(tmp_path) / "state" / "active_envelope.json")
    )
    assert decision.action == "deny"
    assert "self-modification" in decision.reason


def test_envelope_state_rm_and_mv_denied(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    state_file = _agent_dir(tmp_path) / "state" / "active_envelope.json"
    assert _process_bash(repo, f"rm {state_file}").action == "deny"
    assert _process_bash(repo, f"mv {state_file} /tmp/x").action == "deny"


def test_trust_root_writes_denied_when_auth_false(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    for path in (
        _agent_dir(tmp_path) / "trust" / "allowed_signers.yaml",
        tmp_path / "home" / "projects" / "test-proj" / "trust" / "catalog.yaml",
    ):
        decision = _process_write(repo, str(path))
        assert decision.action == "deny", path
        assert "trust root" in decision.reason


def test_trust_root_write_counted_when_auth_declared(tmp_path: Path) -> None:
    # With auth declared, trust writes are ordinary task scope: allowed
    # while budget remains, and they consume the counter.
    repo = _make_workspace(tmp_path)
    envelope = make_envelope(touches_auth_config_or_secrets=True)
    target = _install_envelope(tmp_path, envelope)
    pins = _agent_dir(tmp_path) / "trust" / "allowed_signers.yaml"
    decision = _process_write(repo, str(pins))
    assert decision.action == "allow"
    stored = load_envelope(target)
    assert stored["counters"]["files_touched"] != []


def test_bash_redirect_to_audit_dir_exempt(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    target = _install_envelope(tmp_path, _exhausted_envelope())
    audit_path = (
        _agent_dir(tmp_path) / "audit" / "autonomy_decisions" / "y_msg-1.yaml"
    )
    decision = _process_bash(repo, f"echo done > {audit_path}")
    assert decision.action == "allow"
    stored = load_envelope(target)
    assert stored["counters"]["files_touched"] == ["/repo/a.py", "/repo/b.py"]


def test_peer_agent_inbox_not_exempt(tmp_path: Path) -> None:
    # The exemption is receiver-scoped: another agent's inbox is task scope.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, _exhausted_envelope())
    peer_inbox = (
        tmp_path / "home" / "projects" / "test-proj" / "agents" / "iris"
        / "inbox" / "msg.yaml"
    )
    decision = _process_write(repo, str(peer_inbox))
    assert decision.action == "deny"
    assert decision.reason.startswith(hook.BLOCKED_OPENER)


def test_bookkeeping_exemption_is_not_a_secret_bypass(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, _exhausted_envelope())
    decision = _process_write(repo, "/scratchpad/.env")
    assert decision.action == "deny"
    assert ".env" in decision.reason


def test_ordinary_write_still_denied_at_ceiling(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, _exhausted_envelope())
    decision = _process_write(repo, str(Path(repo) / "c.py"))
    assert decision.action == "deny"
    assert decision.reason.startswith(hook.BLOCKED_OPENER)
    assert "expected 2, now 3" in decision.reason


# ── Hardening regressions: exemption and clear-validation bypasses ───────────


def test_envelope_clear_wildcard_message_id_denied(tmp_path: Path) -> None:
    # Sender data must never act as a filesystem glob: a wildcard id must
    # not adopt an unrelated terminal record.
    repo = _make_workspace(tmp_path)
    envelope = make_envelope()
    envelope["message_id"] = "*"
    _install_envelope(tmp_path, envelope)
    _write_audit_record(tmp_path, message_id="msg-other", final_state="done")
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "deny"


def test_envelope_clear_filename_content_mismatch_denied(tmp_path: Path) -> None:
    # Content identity governs, never the filename.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(
        tmp_path, message_id="msg-other", filename_id="msg-1", final_state="done"
    )
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "deny"


def test_envelope_clear_wrong_receiver_record_denied(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, receiver="codex", final_state="done")
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "deny"


def test_envelope_clear_env_home_override_asks(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    decision = _process_bash(repo, f"OACP_HOME=/tmp/foreign-home {CLEAR_CMD}")
    assert decision.action == "ask"
    assert "OACP_HOME" in decision.reason


def test_envelope_clear_duplicate_flags_use_effective_value(tmp_path: Path) -> None:
    # argparse takes the last occurrence; validation must judge that value.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    retargeted = _process_bash(
        repo, f"{CLEAR_CMD} --project other-proj"
    )
    assert retargeted.action == "ask"
    home = tmp_path / "home"
    converging = _process_bash(
        repo, f"{CLEAR_CMD} --oacp-dir /tmp/foreign-home --oacp-dir {home}"
    )
    assert converging.action == "allow"


def test_envelope_clear_relative_oacp_dir_resolved(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    decision = _process_bash(repo, f"{CLEAR_CMD} --oacp-dir ../home")
    assert decision.action == "allow"


def test_scratchpad_symlink_into_task_scope_not_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A symlink planted under an exempt root must not smuggle ordinary task
    # scope past the counter: containment is judged on resolved paths.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, _exhausted_envelope())
    scratch = tmp_path / "scratch-claude"
    scratch.mkdir()
    (scratch / "alias").symlink_to(repo)
    monkeypatch.setattr(
        hook, "SCRATCHPAD_PREFIXES", (str(Path(os.path.realpath(scratch))) + "/",)
    )
    decision = _process_write(repo, str(scratch / "alias" / "ordinary.py"))
    assert decision.action == "deny"
    assert decision.reason.startswith(hook.BLOCKED_OPENER)


def test_audit_dir_symlink_into_task_scope_not_exempt(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, _exhausted_envelope())
    audit_dir = _agent_dir(tmp_path) / "audit" / "autonomy_decisions"
    audit_dir.mkdir(parents=True)
    (audit_dir / "alias").symlink_to(repo)
    decision = _process_write(repo, str(audit_dir / "alias" / "ordinary.py"))
    assert decision.action == "deny"
    assert decision.reason.startswith(hook.BLOCKED_OPENER)


def test_envelope_clear_in_compound_command_asks(tmp_path: Path) -> None:
    # Earlier segments can retarget the clear after validation: a prior
    # export or cd changes what the CLI resolves, so only a standalone
    # simple command is sanctioned.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done")
    for command in (
        f"export OACP_HOME=/tmp/foreign-home; {CLEAR_CMD}",
        f"cd /tmp; {CLEAR_CMD} --oacp-dir home",
        f"echo done && {CLEAR_CMD}",
        f"true; {CLEAR_CMD}",
    ):
        decision = _process_bash(repo, command)
        assert decision.action == "ask", command


def test_envelope_state_unlink_and_copy_out_denied(tmp_path: Path) -> None:
    # Every filesystem-mutator operand under state/ is self-modification —
    # deletion and exfiltration included, not just write destinations.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    state_file = _agent_dir(tmp_path) / "state" / "active_envelope.json"
    for command in (
        f"unlink {state_file}",
        f"cp {state_file} /tmp/exported-envelope.json",
        f"truncate -s 0 {state_file}",
    ):
        decision = _process_bash(repo, command)
        assert decision.action == "deny", command
        assert "self-modification" in decision.reason


def test_trust_root_mutations_denied_when_auth_false(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    pins = _agent_dir(tmp_path) / "trust" / "allowed_signers.yaml"
    for command in (
        f"rm {pins}",
        f"unlink {pins}",
        f"mv {pins} /tmp/exported-signers.yaml",
    ):
        decision = _process_bash(repo, command)
        assert decision.action == "deny", command
        assert "trust root" in decision.reason


def test_trust_root_mutation_allowed_when_auth_declared(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(
        tmp_path, make_envelope(touches_auth_config_or_secrets=True)
    )
    pins = _agent_dir(tmp_path) / "trust" / "allowed_signers.yaml"
    decision = _process_bash(repo, f"rm {pins}")
    assert decision.action == "allow"


def test_mutator_expansion_operands_ask(tmp_path: Path) -> None:
    # The shell expands globs/braces AFTER classification: a pattern operand
    # can reach a protected path while its literal spelling does not.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    agent = _agent_dir(tmp_path)
    for command in (
        f"unlink {agent}/st*/active_envelope.json",
        f"rm {agent}/{{state,audit}}/x",
        "rm build/*.pyc",
        "rm $STATE/active_envelope.json",
        "rm $TMPDIR/scratch.txt",
    ):
        decision = _process_bash(repo, command)
        assert decision.action == "ask", command
        assert "expansion" in decision.reason or "variable" in decision.reason


def test_mutator_target_directory_forms_gated(tmp_path: Path) -> None:
    # GNU target-directory spellings contribute their value as an operand.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    state = _agent_dir(tmp_path) / "state"
    trust = _agent_dir(tmp_path) / "trust"
    for command in (
        f"cp /tmp/payload --target-directory={state}",
        f"cp /tmp/payload -t{state}",
        f"cp /tmp/payload -t {state}",
    ):
        decision = _process_bash(repo, command)
        assert decision.action == "deny", command
        assert "self-modification" in decision.reason
    linked = _process_bash(repo, f"ln /tmp/payload --target-directory={trust}")
    assert linked.action == "deny"
    assert "trust root" in linked.reason


def test_mutator_dashdash_operand_gated(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    state_file = _agent_dir(tmp_path) / "state" / "active_envelope.json"
    decision = _process_bash(repo, f"rm -- {state_file}")
    assert decision.action == "deny"
    assert "self-modification" in decision.reason


def test_bash_write_target_expansion_asks(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    agent = _agent_dir(tmp_path)
    for command in (
        f"tee {agent}/st*/active_envelope.json",
        "echo x > out*.txt",
    ):
        decision = _process_bash(repo, command)
        assert decision.action == "ask", command
        assert "expansion" in decision.reason


def test_find_action_predicates_ask() -> None:
    assert bash("find . -name '*.tmp' -delete").action == "ask"
    assert bash("find . -name core -exec rm {} +").action == "ask"
    assert bash("find . -name '*.py' -type f").action == "allow"
