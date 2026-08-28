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
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import claude_envelope_hook as hook  # noqa: E402
from envelope_compiler import (  # noqa: E402
    envelope_path,
    load_envelope,
    session_claim_path,
    write_envelope,
)


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


def test_quoted_pipe_pattern_stays_in_readonly_segment() -> None:
    command = (
        "oacp envelope show --project test-proj --receiver claude 2>&1 "
        '| head -25 && command grep -n "expires\\|ttl\\|estimated_minutes" '
        "config.yaml | head -8"
    )

    assert bash(command).action == "allow"


@pytest.mark.parametrize(
    "command",
    [
        'rg -n "foo|bar" README.md',
        "awk '{print $1 \"|\" $2}' data.txt",
        "git log --oneline \\\n  --max-count=5",
    ],
)
def test_quoted_separators_and_line_continuations_stay_in_segment(
    command: str,
) -> None:
    assert bash(command).action == "allow"


@pytest.mark.parametrize(
    "command",
    [
        'grep -n "safe|pattern" file | tee .env',
        'grep -n "safe|pattern" file && gh pr merge 12 --squash',
        'command grep -n "safe|pattern" file > pyproject.toml',
        'grep "$(gh pr merge 12 --squash)" file',
    ],
)
def test_quoted_readonly_head_does_not_hide_mutation(command: str) -> None:
    assert bash(command).action == "deny"


def test_nested_command_substitution_fails_closed() -> None:
    command = 'echo "$(printf \'(safe)\'; gh pr merge 12 --squash)"'

    assert bash(command).action == "ask"


def test_readonly_head_fallback_is_narrow_when_argument_parser_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_split = hook.shlex.split

    def degraded_split(command: str, *args: Any, **kwargs: Any):
        if command.startswith(("grep ", "command grep ", "rg ")):
            raise ValueError("simulated parser degradation")
        return original_split(command, *args, **kwargs)

    monkeypatch.setattr(hook.shlex, "split", degraded_split)

    assert bash('grep -n "safe pattern" file').action == "allow"
    assert bash('command grep -n "safe pattern" file').action == "allow"
    assert bash('grep -n "safe pattern" file > .env').action == "ask"
    assert bash("grep *.txt file").action == "ask"
    assert bash("rg --pre='rm -f .env' needle file").action == "ask"


def test_natural_readonly_fallback_allows_trailing_backslash() -> None:
    assert bash("cat safe-file\\").action == "allow"


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


def _pyproject_text(version: str = "0.4.1") -> str:
    return f'''[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "demo"
version = "{version}"
dependencies = [
  "PyYAML>=6.0",
]

[project.optional-dependencies]
crypto = ["cryptography>=3.4"]
'''


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


@pytest.mark.parametrize("tool_name", ["Edit", "Write"])
def test_version_only_pyproject_file_tool_edit_allowed_and_counted(
    tmp_path: Path,
    tool_name: str,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    before = _pyproject_text()
    pyproject.write_text(before, encoding="utf-8")
    tool_input: Dict[str, Any] = {"file_path": str(pyproject)}
    if tool_name == "Edit":
        tool_input.update(
            old_string='version = "0.4.1"',
            new_string='version = "0.4.2"',
        )
    else:
        tool_input["content"] = _pyproject_text("0.4.2")

    decision = hook.classify(
        tool_name,
        tool_input,
        str(tmp_path),
        make_envelope(),
    )

    assert decision.action == "allow"
    assert decision.new_files == [str(pyproject)]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            'dependencies = [\n  "PyYAML>=6.0",\n]',
            'dependencies = [\n  "PyYAML>=6.0",\n  "httpx>=0.27",\n]',
        ),
        (
            'crypto = ["cryptography>=3.4"]',
            'crypto = ["cryptography>=44"]',
        ),
        (
            'requires = ["hatchling>=1.27"]',
            'requires = ["hatchling>=1.28"]',
        ),
    ],
)
def test_real_pyproject_dependency_edit_stays_denied(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_pyproject_text(), encoding="utf-8")

    decision = hook.classify(
        "Edit",
        {"file_path": str(pyproject), "old_string": old, "new_string": new},
        str(tmp_path),
        make_envelope(),
    )

    assert decision.action == "deny"
    assert "touches_dependencies: false" in decision.reason


def test_mixed_version_and_dependency_pyproject_edit_stays_denied(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_pyproject_text(), encoding="utf-8")
    old = 'version = "0.4.1"\ndependencies = [\n  "PyYAML>=6.0",\n]'
    new = (
        'version = "0.4.2"\ndependencies = [\n'
        '  "PyYAML>=6.0",\n  "httpx>=0.27",\n]'
    )

    decision = hook.classify(
        "Edit",
        {"file_path": str(pyproject), "old_string": old, "new_string": new},
        str(tmp_path),
        make_envelope(),
    )

    assert decision.action == "deny"
    assert "touches_dependencies: false" in decision.reason


def test_pyproject_write_with_dependency_change_stays_denied(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_pyproject_text(), encoding="utf-8")
    content = _pyproject_text("0.4.2").replace(
        '  "PyYAML>=6.0",\n',
        '  "PyYAML>=6.0",\n  "httpx>=0.27",\n',
    )

    decision = hook.classify(
        "Write",
        {"file_path": str(pyproject), "content": content},
        str(tmp_path),
        make_envelope(),
    )

    assert decision.action == "deny"
    assert "touches_dependencies: false" in decision.reason


def test_bash_version_edit_of_pyproject_stays_fail_closed(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_pyproject_text(), encoding="utf-8")

    decision = hook.classify(
        "Bash",
        {"command": f"sed -i s/0.4.1/0.4.2/ {pyproject}"},
        str(tmp_path),
        make_envelope(),
    )

    assert decision.action == "deny"
    assert "touches_dependencies: false" in decision.reason


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


def test_process_foreign_session_noop_and_no_budget_pool(tmp_path: Path) -> None:
    """The two-process scenario: a concurrent session must get pre-envelope
    behavior under another session's envelope, and its writes must not
    consume the dispatched task's files_touched budget."""
    repo = _make_workspace(tmp_path)
    envelope = make_envelope()
    envelope["session_id"] = "sess-a"
    target = _install_envelope(tmp_path, envelope)

    foreign_merge = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 1"},
            "cwd": str(repo),
            "session_id": "sess-b",
        }
    )
    assert foreign_merge.action == "allow"

    foreign_write = hook.process(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "b.py"},
            "cwd": str(repo),
            "session_id": "sess-b",
        }
    )
    assert foreign_write.action == "allow"
    assert load_envelope(target)["counters"]["files_touched"] == []

    owner_merge = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 1"},
            "cwd": str(repo),
            "session_id": "sess-a",
        }
    )
    assert owner_merge.action == "deny"

    owner_write = hook.process(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "a.py"},
            "cwd": str(repo),
            "session_id": "sess-a",
        }
    )
    assert owner_write.action == "allow"
    assert load_envelope(target)["counters"]["files_touched"] == [
        str(Path(repo) / "a.py")
    ]


def test_foreign_session_cannot_touch_shared_envelope_state(tmp_path: Path) -> None:
    """The foreign-session bypass must not extend to the envelope state that
    protects the bound session: clear/compile and direct state-file writes
    keep full classification (and its self-modification denials)."""
    repo = _make_workspace(tmp_path)
    envelope = make_envelope()
    envelope["session_id"] = "sess-owner"
    target = _install_envelope(tmp_path, envelope)

    foreign_clear = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "oacp envelope clear --project test-proj --oacp-dir /home"
            },
            "cwd": str(repo),
            "session_id": "sess-foreign",
        }
    )
    assert foreign_clear.action != "allow"
    assert target.is_file()

    foreign_compile = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "oacp envelope compile /inbox/other.yaml --receiver claude"
            },
            "cwd": str(repo),
            "session_id": "sess-foreign",
        }
    )
    assert foreign_compile.action != "allow"

    foreign_write = hook.process(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "cwd": str(repo),
            "session_id": "sess-foreign",
        }
    )
    assert foreign_write.action != "allow"

    foreign_rm = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"rm {target}"},
            "cwd": str(repo),
            "session_id": "sess-foreign",
        }
    )
    assert foreign_rm.action != "allow"

    # Ordinary foreign work is still bypassed — the guard is state-scoped.
    ordinary = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 1"},
            "cwd": str(repo),
            "session_id": "sess-foreign",
        }
    )
    assert ordinary.action == "allow"


def test_foreign_session_readonly_state_inspection_keeps_bypass(
    tmp_path: Path,
) -> None:
    """State relation is not mutation capability: a foreign session reading
    shared state gets pre-envelope ALLOW with no counter updates, while
    mutation-capable spellings of the same surfaces keep the deny."""
    repo = _make_workspace(tmp_path)
    envelope = make_envelope()
    envelope["session_id"] = "sess-owner"
    target = _install_envelope(tmp_path, envelope)

    readonly_commands = [
        f"cat {target}",
        "oacp envelope show --project test-proj --oacp-dir /home",
        (
            "oacp envelope show --project=test-proj --oacp-dir=/home "
            "--receiver=claude"
        ),
        "python3 -m oacp.cli envelope show --project test-proj",
        "python3 -m oacp.cli envelope show --project=test-proj",
        "oacp envelope show --pro=test-proj --rec claude",
        "oacp envelope show --help",
        "oacp envelope show -h",
        f"cd {target.parent} && cat active_envelope.json | grep session_id",
        f"stat {target}; wc -l {target}",
    ]
    for command in readonly_commands:
        decision = hook.process(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(repo),
                "session_id": "sess-foreign",
            }
        )
        assert decision.action == "allow", command
    assert load_envelope(target)["counters"]["files_touched"] == []

    mutating_commands = [
        "oacp envelope clear --project test-proj --oacp-dir /home",
        # Redirection loses the exemption even under a read-only command.
        f"cat {target} > /tmp/copy.json",
        # A read segment cannot launder a mutating one in the compound.
        f"cat {target} && rm {target}",
        f"echo extra >> {target}",
        # Process substitution executes its body under a read-only head.
        f"cat <(rm {target})",
        # Allowlisted-by-name tools with execution flags stay excluded.
        f"rg --pre rm needle {target}",
        # Trailing show tokens must not bless a different command head.
        f"rm {target} oacp envelope show",
        # Interactive pagers (shell escapes) are not inspection tools.
        f"less {target}",
        # A look-alike module must not pass for the real CLI.
        "python3 -m evil_oacp envelope show --project test-proj",
        # Unknown option grammar on show fails safe.
        "oacp envelope show --project test-proj --unknown-flag",
    ]
    for command in mutating_commands:
        decision = hook.process(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(repo),
                "session_id": "sess-foreign",
            }
        )
        assert decision.action != "allow", command
    assert target.is_file()


def test_foreign_session_variable_state_mutation_stays_enforced(
    tmp_path: Path,
) -> None:
    """Unresolved shell expansion hides the target from the guard: an env
    assignment plus a $VAR operand must stay enforced, not bypassed."""
    repo = _make_workspace(tmp_path)
    envelope = make_envelope()
    envelope["session_id"] = "sess-owner"
    target = _install_envelope(tmp_path, envelope)

    decision = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    f"STATE={target.parent}; rm $STATE/active_envelope.json"
                )
            },
            "cwd": str(repo),
            "session_id": "sess-foreign",
        }
    )
    assert decision.action != "allow"
    assert target.is_file()

    # A bare protected filename after a cd also stays enforced.
    cd_form = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": f"cd {target.parent} && rm active_envelope.json"
            },
            "cwd": str(repo),
            "session_id": "sess-foreign",
        }
    )
    assert cd_form.action != "allow"


def test_foreign_session_guard_canonicalizes_symlinked_state_root(
    tmp_path: Path,
) -> None:
    """A state root reached through a symlink must still contain the real
    path: candidates are canonicalized, so the root must be too."""
    repo = _make_workspace(tmp_path)
    envelope = make_envelope()
    envelope["session_id"] = "sess-owner"
    target = _install_envelope(tmp_path, envelope)

    link_home = tmp_path / "home_link"
    link_home.symlink_to(tmp_path / "home")
    symlinked_state_root = str(
        link_home / "projects" / "test-proj" / "agents" / "claude" / "state"
    )
    assert hook._shared_state_affinity(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
        },
        symlinked_state_root,
        str(repo),
    ) == "affine"
    assert hook._shared_state_affinity(
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"mv {target} /tmp/stolen.json"},
        },
        symlinked_state_root,
        str(repo),
    ) == "affine"


def test_foreign_session_uncertain_command_enforced_without_pooling(
    tmp_path: Path,
) -> None:
    """Expansion-bearing foreign commands with no state affinity stay
    enforced through classification, and foreign work never charges the
    bound session's files_touched budget."""
    repo = _make_workspace(tmp_path)
    envelope = make_envelope()
    envelope["session_id"] = "sess-owner"
    target = _install_envelope(tmp_path, envelope)

    decision = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo $HOME"},
            "cwd": str(repo),
            "session_id": "sess-foreign",
        }
    )
    assert decision.action == "allow"
    assert load_envelope(target)["counters"]["files_touched"] == []


def test_process_unbound_envelope_enforces_every_session(tmp_path: Path) -> None:
    """An envelope without a session binding keeps the historical
    (project, agent) scope even for callers that identify themselves."""
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    decision = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 1"},
            "cwd": str(repo),
            "session_id": "sess-b",
        }
    )
    assert decision.action == "deny"


def test_process_bound_envelope_enforces_unidentified_caller(tmp_path: Path) -> None:
    """A caller the harness gave no session id cannot be proven foreign, so
    a bound envelope still enforces it — scope never silently narrows."""
    repo = _make_workspace(tmp_path)
    envelope = make_envelope()
    envelope["session_id"] = "sess-a"
    _install_envelope(tmp_path, envelope)
    decision = hook.process(
        {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}, "cwd": str(repo)}
    )
    assert decision.action == "deny"


def test_process_records_session_claim_for_compile_command(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    target = envelope_path(tmp_path / "home", "test-proj", "claude")
    claim_file = session_claim_path(target, "sess-a")

    decision = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "oacp envelope compile /inbox/msg_x.yaml "
                    "--receiver claude --oacp-dir /home"
                )
            },
            "cwd": str(repo),
            "session_id": "sess-a",
        }
    )
    assert decision.action == "allow"
    claim = json.loads(claim_file.read_text(encoding="utf-8"))
    assert claim["session_id"] == "sess-a"
    assert claim["message_name"] == "msg_x.yaml"

    claim_file.unlink()

    script_spelling = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "python3 scripts/envelope_compiler.py compile /inbox/msg_y.yaml"
            },
            "cwd": str(repo),
            "session_id": "sess-a",
        }
    )
    assert script_spelling.action == "allow"
    assert json.loads(claim_file.read_text(encoding="utf-8"))["message_name"] == "msg_y.yaml"


def test_compile_claim_recorded_for_module_cli_spelling(tmp_path: Path) -> None:
    """`python3 -m oacp.cli envelope compile …` is a supported front end and
    must record the claim like the executable spelling; end-to-end, the
    compiled envelope binds to the hook session."""
    repo = _make_workspace(tmp_path)
    home = tmp_path / "home"
    target = envelope_path(home, "test-proj", "claude")

    agent_dir = home / "projects" / "test-proj" / "agents" / "claude"
    agent_dir.mkdir(parents=True, exist_ok=True)
    conformance = Path(__file__).resolve().parent / "conformance" / "autonomy"
    (agent_dir / "config.yaml").write_bytes(
        (conformance / "configs" / "auto_review_standard.yaml").read_bytes()
    )
    inbox = agent_dir / "inbox"
    inbox.mkdir(exist_ok=True)
    message_path = inbox / "msg_m.yaml"
    message_path.write_bytes(
        (conformance / "messages" / "clean_task.yaml").read_bytes()
    )

    decision = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    f"python3 -m oacp.cli envelope compile {message_path} "
                    f"--oacp-dir {home}"
                )
            },
            "cwd": str(repo),
            "session_id": "sess-a",
        }
    )
    assert decision.action == "allow"
    claim_file = session_claim_path(target, "sess-a")
    assert json.loads(claim_file.read_text(encoding="utf-8"))["message_name"] == "msg_m.yaml"

    from envelope_compiler import main as compiler_main

    assert compiler_main(
        ["compile", str(message_path), "--oacp-dir", str(home)]
    ) == 0
    assert load_envelope(target)["session_id"] == "sess-a"


def test_compile_claim_survives_option_before_positional(tmp_path: Path) -> None:
    """Value options must not be mistaken for the positional message."""
    repo = _make_workspace(tmp_path)
    target = envelope_path(tmp_path / "home", "test-proj", "claude")

    decision = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "oacp envelope compile --receiver claude "
                    "--oacp-dir /home /inbox/msg_z.yaml"
                )
            },
            "cwd": str(repo),
            "session_id": "sess-a",
        }
    )
    assert decision.action == "allow"
    claim_file = session_claim_path(target, "sess-a")
    assert json.loads(claim_file.read_text(encoding="utf-8"))["message_name"] == "msg_z.yaml"


def test_compile_claim_survives_audit_option_before_positional(
    tmp_path: Path,
) -> None:
    """--audit consumes a value: its argument is never the message.

    Covers the executable, module, and script spellings — a desynced
    option grammar would claim ``audit.yaml`` and leave the real compile
    unbound for every session.
    """
    repo = _make_workspace(tmp_path)
    target = envelope_path(tmp_path / "home", "test-proj", "claude")

    commands = (
        "oacp envelope compile --audit /audit/record.yaml "
        "--receiver claude /inbox/msg_z.yaml",
        "python3 -m oacp.cli envelope compile --audit /audit/record.yaml "
        "/inbox/msg_z.yaml",
        "python3 scripts/envelope_compiler.py compile "
        "--audit /audit/record.yaml /inbox/msg_z.yaml",
    )
    for index, command in enumerate(commands):
        session = f"sess-audit-{index}"
        decision = hook.process(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(repo),
                "session_id": session,
            }
        )
        assert decision.action == "allow"
        claim_file = session_claim_path(target, session)
        claim = json.loads(claim_file.read_text(encoding="utf-8"))
        assert claim["message_name"] == "msg_z.yaml", command


def test_process_writes_no_claim_without_session_or_compile(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    target = envelope_path(tmp_path / "home", "test-proj", "claude")
    claim_file = session_claim_path(target, "sess-a")

    no_session = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "oacp envelope compile /inbox/msg_x.yaml"},
            "cwd": str(repo),
        }
    )
    assert no_session.action == "allow"
    assert not claim_file.exists()

    not_compile = hook.process(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "oacp envelope show --project test-proj"},
            "cwd": str(repo),
            "session_id": "sess-a",
        }
    )
    assert not_compile.action == "allow"
    assert not claim_file.exists()


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
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("[oacp-envelope]")
    assert "msg-1" in reason


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
    assert output["hookSpecificOutput"]["permissionDecisionReason"].startswith(
        "[oacp-envelope]"
    )


def test_main_malformed_stdin_asks(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hook.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert output["hookSpecificOutput"]["permissionDecisionReason"].startswith(
        "[oacp-envelope]"
    )


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


def test_envelope_clear_skips_superseded_records(tmp_path: Path) -> None:
    # A superseded evaluation's authority transferred to its successor: a
    # newest-but-superseded record must neither sanction the clear (live
    # work continues under the older record) nor block it once the live
    # record is terminal.
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="pending", stamp="20260713T000000Z")
    _write_audit_record(tmp_path, final_state="superseded", stamp="20260714T000000Z")
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "deny"


def test_envelope_clear_allowed_when_only_live_record_terminal(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    _write_audit_record(tmp_path, final_state="done", stamp="20260713T000000Z")
    _write_audit_record(tmp_path, final_state="superseded", stamp="20260714T000000Z")
    decision = _process_bash(repo, CLEAR_CMD)
    assert decision.action == "allow"


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
    # then a bookkeeping audit write — must not deny or count. The completion
    # record itself moved out of this exemption: `autonomy_decisions/` became
    # authority once a recorded re-authorization could widen the live bound,
    # and its writes go through the canonical CLI writers (see
    # test_admission_audit_record_write_denied).
    repo = _make_workspace(tmp_path)
    target = _install_envelope(tmp_path, _exhausted_envelope())
    audit_path = _agent_dir(tmp_path) / "audit" / "x_msg-1.yaml"
    decision = _process_write(repo, str(audit_path))
    assert decision.action == "allow"
    stored = load_envelope(target)
    assert stored["counters"]["files_touched"] == ["/repo/a.py", "/repo/b.py"]


def test_admission_audit_record_write_denied(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, _exhausted_envelope())
    audit_path = (
        _agent_dir(tmp_path) / "audit" / "autonomy_decisions" / "x_msg-1.yaml"
    )
    decision = _process_write(repo, str(audit_path))
    assert decision.action == "deny"
    assert "authority self-modification" in decision.reason


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
    audit_path = _agent_dir(tmp_path) / "audit" / "notes.md"
    decision = _process_bash(repo, f"echo done > {audit_path}")
    assert decision.action == "allow"
    stored = load_envelope(target)
    assert stored["counters"]["files_touched"] == ["/repo/a.py", "/repo/b.py"]


def test_bash_redirect_to_admission_audit_record_denied(tmp_path: Path) -> None:
    """`audit/autonomy_decisions/` is authority, not bookkeeping: a recorded
    re-authorization widens the live bound, so a session able to write its own
    record would be able to write its own grant."""
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, _exhausted_envelope())
    audit_path = (
        _agent_dir(tmp_path) / "audit" / "autonomy_decisions" / "y_msg-1.yaml"
    )
    decision = _process_bash(repo, f"echo done > {audit_path}")
    assert decision.action == "deny"
    assert "authority self-modification" in decision.reason


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


# ── Heredoc bodies and variable redirect targets (phantom files_touched) ──────


PHANTOM_FILE_ROWS = [
    pytest.param(
        "python3 - <<'PYEOF'\nprint(f\"{a:>8} -> {c.value:13} ok\")\nPYEOF",
        id="heredoc_arrow_in_format_string",
    ),
    pytest.param(
        "python3 - <<'PYEOF'\nprint(v, \"-> INSERTED id\", rid)\nPYEOF",
        id="heredoc_arrow_in_string",
    ),
    pytest.param(
        'printf "x: nan\\n" > /scratchpad/probe/obs_nan.yaml',
        id="literal_scratchpad_redirect",
    ),
    pytest.param(
        "python3 - <<'PYEOF'\nst = Store(\":memory:\")\nPYEOF",
        id="heredoc_without_redirect",
    ),
]


@pytest.mark.parametrize("command", PHANTOM_FILE_ROWS)
def test_phantom_write_rows_are_not_counted(command: str) -> None:
    """Nothing outside the exempt scratchpad is written: zero budget suffices."""
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "allow", decision.reason
    assert decision.new_files == []


@pytest.mark.parametrize(
    "command, expected",
    [
        pytest.param(
            "cat > notes.md <<'EOF'\nline -> arrow\nEOF",
            ["/repo/notes.md"],
            id="redirect_before_heredoc_operator",
        ),
        pytest.param(
            "tee notes.md <<'EOF'\nline -> arrow\nEOF",
            ["/repo/notes.md"],
            id="tee_with_heredoc_body",
        ),
    ],
)
def test_real_write_on_heredoc_line_still_counted(command: str, expected: list) -> None:
    """The operator line is shell; only the body is data."""
    decision = bash(command)
    assert decision.action == "allow", decision.reason
    assert decision.new_files == expected


def test_live_incident_heredoc_shape_allowed_with_zero_budget() -> None:
    command = (
        "python3 - <<'PYEOF'\n"
        "import json\n"
        "for rid in ids:\n"
        '    print(rid, "-> INSERTED")\n'
        'print("a -> b")\n'
        "PYEOF"
    )
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "allow", decision.reason
    assert decision.new_files == []


def test_unterminated_heredoc_asks() -> None:
    decision = bash("python3 - <<'PYEOF'\nprint('x')\n")
    assert decision.action == "ask", decision.reason
    assert "heredoc" in decision.reason


# ── Fail-closed boundaries of the phantom-count fix ──────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "cat <<EOF\n$(touch escaped-by-hook.md)\nEOF",
            id="unquoted_delimiter_command_substitution",
        ),
        pytest.param(
            "cat <<EOF\n`touch escaped-by-hook.md`\nEOF",
            id="unquoted_delimiter_backtick",
        ),
        pytest.param(
            "cat <<-EOF\n\t$(touch escaped-by-hook.md)\n\tEOF",
            id="unquoted_dash_delimiter_substitution",
        ),
    ],
)
def test_expanding_heredoc_body_with_substitution_asks(command: str) -> None:
    """An unquoted delimiter leaves the body subject to expansion: a command
    or backtick substitution in it executes, so the body is not data."""
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "ask", decision.reason
    assert "heredoc" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat <<'EOF'\n$(touch not-run.md)\nEOF", id="single_quoted_delimiter"),
        pytest.param('cat <<"EOF"\n`touch not-run.md`\nEOF', id="double_quoted_delimiter"),
        pytest.param("cat <<\\EOF\n$(touch not-run.md)\nEOF", id="backslashed_delimiter"),
        pytest.param("cat <<EOF\nhello $USER -> ${HOME}\nEOF", id="expanding_body_no_substitution"),
    ],
)
def test_literal_or_substitution_free_heredoc_bodies_allowed(command: str) -> None:
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "allow", decision.reason
    assert decision.new_files == []


@pytest.mark.parametrize(
    "command, expected",
    [
        pytest.param("touch -- '<notes.md'", ["/repo/<notes.md"], id="quoted_operand_like_input_redirect"),
        pytest.param("touch -- '<<NOT_A_HEREDOC'", ["/repo/<<NOT_A_HEREDOC"], id="quoted_operand_like_heredoc"),
        pytest.param('tee "<notes.md" <<\'EOF\'\nline -> arrow\nEOF', ["/repo/<notes.md"], id="quoted_operand_with_heredoc"),
    ],
)
def test_quoted_writer_operands_keep_lexical_provenance(command: str, expected: list) -> None:
    """shlex dequotes, so a literal operand spelled like redirection syntax is
    still an operand; only the real `<<WORD` operator is removed."""
    decision = bash(command)
    assert decision.action == "allow", decision.reason
    assert decision.new_files == expected
    zero = bash(command, make_envelope(expected_files_touched=0))
    assert zero.action == "deny"
    assert "expected 0, now 1" in zero.reason


@pytest.mark.parametrize(
    "command",
    [
        # The issue rows that spell a scratchpad target through a variable.
        pytest.param(
            'OUT=/scratchpad/pytest.log; { python3 -m pytest -q; } > "$OUT"',
            id="var_redirect_assigned_in_same_command",
        ),
        pytest.param(
            'S=/scratchpad/probe; printf "x: nan\\n" > $S/obs_nan.yaml',
            id="var_redirect_path_segment",
        ),
        pytest.param('echo x > "$OUT"', id="never_assigned"),
        pytest.param('OUT=/scratchpad/x.log python3 -m pytest > "$OUT"', id="prefix_assignment"),
        pytest.param('OUT=$TMPDIR/x.log; echo x > "$OUT"', id="assigned_from_a_variable"),
        pytest.param('D=/scratchpad; F=probe.log; echo x > "${D}/$F"', id="braced_and_bare"),
        pytest.param('OUT=/scratchpad/x; touch "$OUT"', id="writer_program_operand"),
        pytest.param('cp notes.md "$DEST"', id="cp_destination"),
        pytest.param('echo x > "$(mktemp)"', id="command_substitution_target"),
        pytest.param("echo x > `mktemp`", id="backtick_target"),
        pytest.param('OUT=/repo/notes.md; echo x > "$OUT"', id="task_scope_value_is_not_counted_but_asked"),
    ],
)
def test_variable_write_target_asks(command: str) -> None:
    """A target the shell would expand from state the classifier cannot see
    escalates: neither counted as a cwd-relative phantom nor trusted."""
    decision = bash(command)
    assert decision.action == "ask", decision.reason
    assert "variable" in decision.reason
    zero = bash(command, make_envelope(expected_files_touched=0))
    assert zero.action == "ask", zero.reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('false && OUT=/scratchpad/skipped; touch "$OUT/actual.md"', id="and_conditional"),
        pytest.param('true || OUT=/scratchpad/skipped; touch "$OUT/actual.md"', id="or_conditional"),
        pytest.param('OUT=/scratchpad/x | cat; echo x > "$OUT"', id="pipeline_subshell"),
        pytest.param('OUT=/scratchpad/x & echo x > "$OUT"', id="background_subshell"),
        pytest.param('OUT=/scratchpad/a; true && OUT=/repo/b; echo x > "$OUT"', id="conditional_reassignment"),
        pytest.param('OUT=/scratchpad/a; read OUT; echo x > "$OUT"', id="read_builtin"),
        pytest.param('OUT=/scratchpad/a; export OUT=/repo/b; echo x > "$OUT"', id="export_builtin"),
        pytest.param('OUT=/scratchpad/a; readonly OUT=/repo/actual.md; touch "$OUT"', id="readonly_builtin"),
        pytest.param('OUT=/scratchpad/a; printf -v OUT /repo/b; echo x > "$OUT"', id="printf_v"),
        pytest.param('OUT=/scratchpad/a; eval "OUT=/repo/b"; echo x > "$OUT"', id="eval"),
        pytest.param('OUT=/scratchpad/a; for OUT in /repo/b; do echo x > "$OUT"; done', id="for_loop"),
        pytest.param('OUT=/scratchpad; OUT+=../repo; echo x > "$OUT/x.md"', id="append_assignment"),
        pytest.param(
            'OUT=/repo; echo "$(OUT=/scratchpad/safe)" "$(touch "$OUT/actual.md")"',
            id="assignment_in_a_separate_substitution",
        ),
    ],
)
def test_shell_state_is_not_modeled(command: str) -> None:
    """No assignment shape — conditional, subshell, builtin-mediated, or a
    separate command substitution — resolves a variable target: the
    classifier holds no shell-state model, so every such use escalates."""
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "ask", decision.reason
    # `eval` already escalates as shell indirection before any target is read.
    assert "variable" in decision.reason or "indirection" in decision.reason


# ── Extraction coverage: every redirect / writer destination is collected ────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('OUT=/repo/actual.md; echo x >| "$OUT"', id="noclobber_override_spaced"),
        pytest.param('OUT=/repo/actual.md; echo x >|"$OUT"', id="noclobber_override_unspaced"),
        pytest.param("echo x >| $OUT", id="noclobber_override_bare"),
        pytest.param('cmd 2>| "$ERR"', id="noclobber_override_stderr"),
        pytest.param('DEST=/repo; cp --target-directory="$DEST" /scratchpad/source.md', id="cp_target_directory_equals"),
        pytest.param('DEST=/repo; cp --target-directory "$DEST" /scratchpad/source.md', id="cp_target_directory_separate"),
        pytest.param('DEST=/repo; install -t "$DEST" /scratchpad/source.md', id="install_t"),
        pytest.param('DEST=/repo; mv -t"$DEST" /scratchpad/source.md', id="mv_t_attached"),
        pytest.param('DEST=/repo; cp -rt "$DEST" /scratchpad/source.md', id="cp_short_cluster_ending_in_t"),
        pytest.param('install -m 644 -t "$DEST" /scratchpad/source.md', id="install_option_argument_before_t"),
        pytest.param("echo x > >(tee $OUT)", id="process_substitution_writer_variable"),
    ],
)
def test_uncollected_destination_syntaxes_now_ask(command: str) -> None:
    """A destination the extractor never collected could not reach the
    variable check: the noclobber override, GNU target-directory options,
    and a writer inside a process substitution are collected now."""
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "ask", decision.reason
    assert "variable" in decision.reason


@pytest.mark.parametrize(
    "command, expected",
    [
        pytest.param("echo x >| notes.md", ["/repo/notes.md"], id="noclobber_literal"),
        pytest.param("echo x >|notes.md", ["/repo/notes.md"], id="noclobber_literal_unspaced"),
        pytest.param("cp -t docs notes.md", ["/repo/docs"], id="cp_t_literal"),
        pytest.param("install --target-directory=docs notes.md", ["/repo/docs"], id="install_target_directory_literal"),
        pytest.param("cat <(touch /repo/x.md)", ["/repo/x.md"], id="process_substitution_input_writer"),
        pytest.param("echo x > >(tee /repo/x.md)", ["/repo/x.md"], id="process_substitution_output_writer"),
        pytest.param("cp -- src dst", ["/repo/dst"], id="double_dash_ends_options"),
    ],
)
def test_collected_literal_destinations_count(command: str, expected: list) -> None:
    decision = bash(command)
    assert decision.action == "allow", decision.reason
    assert decision.new_files == expected
    zero = bash(command, make_envelope(expected_files_touched=0))
    assert zero.action == "deny"
    assert "expected 0, now 1" in zero.reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("echo x >| /scratchpad/x", id="noclobber_scratchpad"),
        pytest.param("cp --target-directory=/scratchpad/out notes.md", id="target_directory_scratchpad"),
        pytest.param("diff <(git ls-files | sort) <(cat list)", id="read_only_process_substitutions"),
        pytest.param("comm -23 <(git ls-files|sort) <(cat x|sort)", id="read_only_process_substitutions_with_pipes"),
        pytest.param("echo x > >(cat)", id="process_substitution_without_writer"),
        pytest.param("cmd 2>&1 | grep x", id="pipe_after_dup_redirect"),
        pytest.param("cp -S.txt a /scratchpad/b", id="short_option_value_without_t"),
    ],
)
def test_extraction_coverage_controls_allow(command: str) -> None:
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "allow", decision.reason
    assert decision.new_files == []


def test_nested_process_substitution_asks() -> None:
    decision = bash("diff <(sort a) <($(cat list))")
    assert decision.action == "ask", decision.reason
    assert "process substitution" in decision.reason


# ── Option spellings the supported GNU writers accept ────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('DEST=/repo; cp --target-dir="$DEST" /scratchpad/source.md', id="cp_abbreviated_attached"),
        pytest.param('cp --target-dir "$DEST" /scratchpad/source.md', id="cp_abbreviated_separate"),
        pytest.param('install --t="$DEST" /scratchpad/source.md', id="install_shortest_prefix"),
        pytest.param('mv --targ "$DEST" /scratchpad/source.md', id="mv_prefix"),
        pytest.param('sed --in-place s/a/b/ "$F"', id="sed_long_in_place"),
        pytest.param('sed --in-p=.bak s/a/b/ "$F"', id="sed_abbreviated_in_place_suffix"),
        pytest.param('sed --i s/a/b/ "$F"', id="sed_shortest_in_place_prefix"),
        pytest.param('sed -ni s/a/b/ "$F"', id="sed_cluster_with_i"),
        pytest.param('sed -Ei.bak s/a/b/ "$F"', id="sed_cluster_with_i_and_suffix"),
        pytest.param('sed -i -e s/a/b/ "$F"', id="sed_separate_expression"),
        pytest.param('sed -i -es/a/b/ "$F"', id="sed_attached_expression"),
        pytest.param('sed -ie s/a/b/ "$F"', id="sed_cluster_i_then_e"),
        pytest.param('sed -i --expression=s/a/b/ "$F"', id="sed_long_expression"),
        pytest.param('sed -i --expr s/a/b/ "$F"', id="sed_abbreviated_expression"),
        pytest.param('sed -i -f prog.sed "$F"', id="sed_script_file"),
        pytest.param('sed -i -l 80 s/a/b/ "$F"', id="sed_line_length_value_skipped"),
        pytest.param('sed -i s/a/b/ -- "$F"', id="sed_double_dash"),
        pytest.param('tee -- "$F"', id="tee_double_dash"),
    ],
)
def test_accepted_option_spellings_reach_the_variable_check(command: str) -> None:
    """GNU getopt_long accepts unambiguous long-option abbreviations and
    short-option clusters; every spelling of a destination-bearing option
    must expose its destination to the fail-closed check."""
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "ask", decision.reason
    assert "variable" in decision.reason


@pytest.mark.parametrize(
    "command, expected",
    [
        pytest.param("cp --target-dir=/repo/out /scratchpad/source.md", ["/repo/out"], id="cp_abbreviated_literal"),
        pytest.param("install --t=docs notes.md", ["/repo/docs"], id="install_shortest_prefix_literal"),
        pytest.param("cp --recursive src dst", ["/repo/dst"], id="unrelated_long_option_not_a_destination"),
        pytest.param("cp --no-target-directory src dst", ["/repo/dst"], id="negated_option_not_a_destination"),
        pytest.param("mv --suffix=.bak src dst", ["/repo/dst"], id="valued_long_option_not_a_destination"),
        pytest.param("sed --in-place s/a/b/ notes.md", ["/repo/notes.md"], id="sed_long_in_place_literal"),
        pytest.param("sed -i -e s/a/b/ -e s/c/d/ notes.md", ["/repo/notes.md"], id="sed_expressions_are_not_files"),
        pytest.param("sed -i.bak s/a/b/ notes.md", ["/repo/notes.md"], id="sed_suffix_literal"),
        pytest.param("sed -ni s/a/b/p notes.md", ["/repo/notes.md"], id="sed_cluster_literal"),
        pytest.param("sed -i -l 80 s/a/b/ notes.md", ["/repo/notes.md"], id="sed_line_length_literal"),
        pytest.param("sed -i -f prog.sed notes.md", ["/repo/notes.md"], id="sed_script_file_literal"),
        pytest.param("touch -- -weird", ["/repo/-weird"], id="double_dash_operand_with_dash"),
    ],
)
def test_accepted_option_spellings_count_literal_destinations(command: str, expected: list) -> None:
    decision = bash(command)
    assert decision.action == "allow", decision.reason
    assert decision.new_files == expected
    zero = bash(command, make_envelope(expected_files_touched=0))
    assert zero.action == "deny"
    assert "expected 0, now 1" in zero.reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("sed -ne s/a/b/p notes.md", id="sed_not_in_place"),
        pytest.param("sed --expression=s/a/b/ notes.md", id="sed_long_expression_not_in_place"),
        pytest.param("sed -i s/a/b/ /scratchpad/x", id="sed_in_place_scratchpad"),
        pytest.param("install --t=/scratchpad/out notes.md", id="abbreviated_target_scratchpad"),
    ],
)
def test_accepted_option_spellings_controls_allow(command: str) -> None:
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "allow", decision.reason
    assert decision.new_files == []


# ── The Bash output-redirect grammar, every spelling ─────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('OUT=/repo/actual.md; echo x >& "$OUT"', id="combined_output_spaced"),
        pytest.param('echo x >&"$OUT"', id="combined_output_unspaced"),
        pytest.param("echo x >&$OUT", id="combined_output_bare"),
        pytest.param('echo x 2>& "$ERR"', id="combined_output_numbered"),
        pytest.param('echo x > "$O"', id="plain"),
        pytest.param('echo x >> "$O"', id="append"),
        pytest.param('echo x >| "$O"', id="noclobber_override"),
        pytest.param('echo x &> "$O"', id="ampersand_combined"),
        pytest.param('echo x &>> "$O"', id="ampersand_combined_append"),
        pytest.param('echo x 2> "$O"', id="numbered"),
        pytest.param('echo x 1>> "$O"', id="numbered_append"),
        pytest.param('cmd 3<> "$O"', id="read_write"),
        pytest.param('cmd {fd}> "$O"', id="named_descriptor"),
        pytest.param('cmd {fd}>> "$O"', id="named_descriptor_append"),
        pytest.param('cmd >"$O" 2>&1', id="file_then_dup"),
        pytest.param('cmd 2>&1 >"$O"', id="dup_then_file"),
        pytest.param('cmd &>"$O" </dev/null', id="combined_with_input"),
    ],
)
def test_every_output_redirect_spelling_reaches_the_variable_check(command: str) -> None:
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "ask", decision.reason
    assert "variable" in decision.reason


@pytest.mark.parametrize(
    "command, expected",
    [
        pytest.param("echo x >&/repo/actual.md", ["/repo/actual.md"], id="combined_output_literal_unspaced"),
        pytest.param("echo x >& notes.md", ["/repo/notes.md"], id="combined_output_literal_spaced"),
        pytest.param("echo x &> notes.md", ["/repo/notes.md"], id="ampersand_combined_literal"),
        pytest.param("echo x &>>notes.md", ["/repo/notes.md"], id="ampersand_combined_append_literal"),
        pytest.param("cmd 3<>notes.md", ["/repo/notes.md"], id="read_write_literal"),
        pytest.param("cmd {fd}>notes.md", ["/repo/notes.md"], id="named_descriptor_literal"),
        pytest.param("echo x > 1", ["/repo/1"], id="plain_redirect_to_a_file_named_1"),
        pytest.param("echo x >> -", ["/repo/-"], id="append_to_a_file_named_dash"),
        pytest.param("cmd >notes.md 2>&1", ["/repo/notes.md"], id="file_then_dup_literal"),
    ],
)
def test_every_output_redirect_spelling_counts_literal_files(command: str, expected: list) -> None:
    decision = bash(command)
    assert decision.action == "allow", decision.reason
    assert decision.new_files == expected
    zero = bash(command, make_envelope(expected_files_touched=0))
    assert zero.action == "deny"
    assert "expected 0, now 1" in zero.reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("echo x >&2", id="dup_stdout_to_stderr"),
        pytest.param("cmd 2>&1", id="dup_stderr_to_stdout"),
        pytest.param("cmd >&-", id="close_stdout"),
        pytest.param("cmd 2>&-", id="close_stderr"),
        pytest.param("cmd 1>&3", id="dup_to_descriptor_3"),
        pytest.param("cmd {fd}>&-", id="close_named_descriptor"),
        pytest.param("cmd <&0", id="dup_input"),
        pytest.param("cmd <&-", id="close_input"),
        pytest.param("cmd 2>&1 | grep x", id="dup_then_pipe"),
        pytest.param("cmd >&2 | tee /scratchpad/log", id="dup_then_pipe_to_scratchpad"),
        pytest.param("cat < notes.md", id="input_redirect"),
        pytest.param("echo x >& /scratchpad/x", id="combined_output_scratchpad"),
    ],
)
def test_descriptor_duplication_and_closure_are_not_files(command: str) -> None:
    decision = bash(command, make_envelope(expected_files_touched=0))
    assert decision.action == "allow", decision.reason
    assert decision.new_files == []


# ── Granted re-authorization: audit-record overlay at enforcement time ───────


REAUTH_SHA = "0" * 64


def _write_reauth_record(
    tmp_path: Path,
    *,
    disposition: str = "resumed",
    files: Optional[int] = 4,
    minutes: Optional[int] = 90,
    scope_less: bool = False,
    message_sha256: str = REAUTH_SHA,
    message_id: str = "msg-1",
    stamp: str = "20260828T000000Z",
    requested_files: Optional[int] = None,
    final_state: str = "pending",
) -> Path:
    """Write an audit record carrying a §E re-authorization block, shaped
    exactly as the gate writes it (see ``_default_reauthorization_block``)."""
    scope: Optional[Dict[str, Any]] = None
    if not scope_less:
        scope = {}
        if files is not None:
            scope["max_actual_files_touched"] = files
        if minutes is not None:
            scope["max_actual_minutes"] = minutes
    requested = (
        {"max_actual_files_touched": requested_files}
        if requested_files is not None
        else None
    )
    record = {
        "message_id": message_id,
        "receiver": "claude",
        "message_sha256": message_sha256,
        "result": {
            "final_state": final_state,
            "threshold_checkpoint": {
                "evaluated": True,
                "reauthorization": {
                    "presented": True,
                    "channel": "receiver_human",
                    "decision": "approved",
                    "disposition": disposition,
                    "requested_scope": requested,
                    "scope": scope,
                },
            },
        },
    }
    return _write_audit_record(
        tmp_path,
        message_id=message_id,
        stamp=stamp,
        body=yaml.safe_dump(record, sort_keys=False),
    )


def _at_ceiling(tmp_path: Path) -> Path:
    """Envelope compiled for 2 files, both already spent."""
    envelope = make_envelope()  # expected_files_touched: 2
    envelope["counters"]["files_touched"] = ["/repo/a.py", "/repo/b.py"]
    return _install_envelope(tmp_path, envelope)


def test_granted_reauthorization_widens_the_live_file_bound(
    tmp_path: Path,
) -> None:
    """Pause at N, grant N+k, then a real (non-exempt) write of file N+1
    proceeds while N+k+1 still blocks. The envelope is never recompiled —
    the grant is read from the audit record at enforcement time."""
    repo = _make_workspace(tmp_path)
    target = _at_ceiling(tmp_path)
    _write_reauth_record(tmp_path)  # N=2 → granted 4

    third = _process_write(repo, "c.py")
    assert third.action == "allow", third.reason
    fourth = _process_write(repo, "d.py")
    assert fourth.action == "allow", fourth.reason

    fifth = _process_write(repo, "e.py")
    assert fifth.action == "deny"
    assert fifth.reason.startswith(hook.BLOCKED_OPENER)
    assert "expected 4, now 5" in fifth.reason

    stored = load_envelope(target)
    assert stored["constraints"]["expected_files_touched"] == 2
    assert len(stored["counters"]["files_touched"]) == 4


def test_ceiling_blocks_without_a_granted_reauthorization(tmp_path: Path) -> None:
    """The pre-fix behavior on the unchanged path: no grant, no widening."""
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    decision = _process_write(repo, "c.py")
    assert decision.action == "deny"
    assert "expected 2, now 3" in decision.reason


def test_unresumed_reauthorization_does_not_widen(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    _write_reauth_record(tmp_path, disposition="insufficient")
    decision = _process_write(repo, "c.py")
    assert decision.action == "deny"
    assert "expected 2, now 3" in decision.reason


def test_scopeless_reauthorization_does_not_widen(tmp_path: Path) -> None:
    """A fresh scope-less approval clears exactly its own pause and records
    nothing durable — it must not raise the bound for later writes."""
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    _write_reauth_record(tmp_path, scope_less=True)
    decision = _process_write(repo, "c.py")
    assert decision.action == "deny"
    assert "expected 2, now 3" in decision.reason


def test_requested_scope_never_widens_past_the_effective_grant(
    tmp_path: Path,
) -> None:
    """Only the gate's policy-capped ``scope`` governs; the sender's
    ``requested_scope`` is provenance and must not reach enforcement."""
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    _write_reauth_record(
        tmp_path,
        files=3,
        requested_files=99,
    )
    third = _process_write(repo, "c.py")
    assert third.action == "allow", third.reason
    fourth = _process_write(repo, "d.py")
    assert fourth.action == "deny"
    assert "expected 3, now 4" in fourth.reason


def test_grant_below_the_compiled_bound_never_narrows_it(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())  # 2 files, none spent
    _write_reauth_record(tmp_path, files=1)
    first = _process_write(repo, "a.py")
    assert first.action == "allow", first.reason
    second = _process_write(repo, "b.py")
    assert second.action == "allow", second.reason
    assert _process_write(repo, "c.py").action == "deny"


def test_reauthorization_for_other_message_bytes_does_not_widen(
    tmp_path: Path,
) -> None:
    """Content binding: a record whose ``message_sha256`` is not this
    envelope's cannot widen it, even under a matching message id."""
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    _write_reauth_record(tmp_path, message_sha256="1" * 64)
    decision = _process_write(repo, "c.py")
    assert decision.action == "deny"
    assert "expected 2, now 3" in decision.reason


def test_superseded_record_does_not_widen(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    _write_reauth_record(tmp_path, final_state="superseded")
    decision = _process_write(repo, "c.py")
    assert decision.action == "deny"
    assert "expected 2, now 3" in decision.reason


def test_unreadable_audit_record_leaves_the_bound_standing(
    tmp_path: Path,
) -> None:
    """Fail-closed direction for a widening overlay: an unparseable record
    is skipped, so the compiled bound blocks rather than opening."""
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    _write_audit_record(tmp_path, body="{ not: [valid, yaml\n")
    decision = _process_write(repo, "c.py")
    assert decision.action == "deny"
    assert "expected 2, now 3" in decision.reason


def test_newest_matching_record_governs_the_overlay(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    _write_reauth_record(
        tmp_path, stamp="20260828T000000Z", files=9
    )
    _write_reauth_record(
        tmp_path, stamp="20260828T010000Z", files=3
    )
    third = _process_write(repo, "c.py")
    assert third.action == "allow", third.reason
    assert _process_write(repo, "d.py").action == "deny"


def test_overlay_is_not_read_before_the_ceiling_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit directory is consulted lazily — an ordinary in-budget write
    must not pay for it on the hot path."""
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())  # 2 files, none spent
    calls: list = []
    monkeypatch.setattr(
        hook,
        "_reauthorized_scope",
        lambda context: calls.append(context) or None,
    )
    assert _process_write(repo, "a.py").action == "allow"
    assert calls == []
    assert _process_write(repo, "b.py").action == "allow"
    assert calls == []
    assert _process_write(repo, "c.py").action == "deny"
    assert len(calls) == 1


def test_forged_audit_record_cannot_widen_the_live_bound(tmp_path: Path) -> None:
    """Round-1 blocking finding (F-001): the write path that would let a
    bounded session author its own grant must be closed at both ends —
    the record write is denied, and a record planted out-of-band (another
    process, a pre-existing file) still cannot widen without the canonical
    identity fields."""
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    forged = (
        _agent_dir(tmp_path)
        / "audit"
        / "autonomy_decisions"
        / "zzzz_forged.yaml"
    )

    # End 1: the session cannot write the authority record at all.
    blocked = _process_bash(repo, f"echo x > {forged}")
    assert blocked.action == "deny"
    assert "authority self-modification" in blocked.reason
    for verb in ("tee", "cp /etc/hosts", "mv /etc/hosts"):
        assert _process_bash(repo, f"{verb} {forged}").action == "deny"
    assert _process_write(repo, str(forged)).action == "deny"

    # End 2: even planted out-of-band with this envelope's exact identity
    # (message_id + receiver + message_sha256 are all readable from the
    # envelope), the re-authorization state must be complete and coherent.
    # A mapping that merely spells `disposition: resumed` does not widen.
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text(
        yaml.safe_dump(
            {
                "message_id": "msg-1",
                "receiver": "claude",
                "message_sha256": REAUTH_SHA,
                "result": {
                    "final_state": "pending",
                    "human_outcome": {"recorded": False},
                    "threshold_checkpoint": {
                        "reauthorization": {
                            "disposition": "resumed",
                            "scope": {"max_actual_files_touched": 999},
                        }
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    decision = _process_write(repo, "c.py")
    assert decision.action == "deny", (
        "an unvalidated audit mapping widened the live bound without a "
        "valid grant"
    )
    assert "expected 2, now 3" in decision.reason


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"presented": False}, id="not_presented"),
        pytest.param({"channel": "gh_comment"}, id="non_governing_channel"),
        pytest.param({"channel": None}, id="no_channel"),
        pytest.param({"decision": "declined"}, id="declined"),
        pytest.param({"decision": "sure"}, id="unknown_decision"),
        pytest.param({"decision": None}, id="no_decision"),
    ],
)
def test_incomplete_reauthorization_state_does_not_widen(
    tmp_path: Path, mutation: Dict[str, Any]
) -> None:
    """The whole re-authorization block is validated, not `disposition`
    alone — a partial or incoherent block widens nothing."""
    repo = _make_workspace(tmp_path)
    _at_ceiling(tmp_path)
    record_path = _write_reauth_record(tmp_path)
    record = yaml.safe_load(record_path.read_text(encoding="utf-8"))
    record["result"]["threshold_checkpoint"]["reauthorization"].update(mutation)
    record_path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")

    decision = _process_write(repo, "c.py")
    assert decision.action == "deny"
    assert "expected 2, now 3" in decision.reason


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("rm {rec}", id="rm"),
        pytest.param("rm -f {rec}", id="rm_force"),
        pytest.param("rm -- {rec}", id="rm_end_of_options"),
        pytest.param("unlink {rec}", id="unlink"),
        pytest.param("mv {rec} /tmp/stashed.yaml", id="mv_source"),
        pytest.param("mv -t /tmp {rec}", id="mv_target_dir_source"),
        pytest.param("cp {rec} /tmp/copied.yaml", id="cp_source"),
        pytest.param("truncate -s 0 {rec}", id="truncate"),
        pytest.param("shred {rec}", id="shred"),
        pytest.param("ln -s /dev/null {rec}", id="ln_alias"),
        pytest.param("install /etc/hosts {rec}", id="install_dest"),
        pytest.param("tee {rec}", id="tee_dest"),
        pytest.param("sed -i s/a/b/ {rec}", id="sed_in_place"),
        pytest.param("echo x > {rec}", id="redirect"),
        pytest.param("echo x >> {rec}", id="append_redirect"),
    ],
)
def test_authority_record_mutations_denied_regardless_of_role(
    tmp_path: Path, template: str
) -> None:
    """Round-2 blocking finding: the write-target gate sees only
    destinations, so `rm`/`unlink`/`mv`-as-source escaped it. Both gates now
    consume the same authority-roots list — deleting or relocating a record
    changes which record governs, so role cannot matter."""
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    record = (
        _agent_dir(tmp_path)
        / "audit"
        / "autonomy_decisions"
        / "20260828T000000Z_msg-1.yaml"
    )
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("message_id: msg-1\n", encoding="utf-8")
    command = template.format(rec=record)
    decision = _process_bash(repo, command)
    assert decision.action == "deny", f"{command} -> {decision.action}"
    assert "self-modification" in decision.reason


def test_authority_roots_cover_state_and_audit_together(tmp_path: Path) -> None:
    """The two gates must never protect one surface and miss the other:
    both read WorkspaceContext.authority_roots()."""
    context = hook.WorkspaceContext(
        oacp_root=tmp_path / "home",
        project="test-proj",
        receiver="claude",
        message_id="msg-1",
        message_sha256="0" * 64,
    )
    roots = {label: path for label, path in context.authority_roots()}
    assert set(roots) == {"envelope state", "autonomy audit record"}
    assert roots["envelope state"] == context.state_dir()
    assert roots["autonomy audit record"] == context.audit_dir()


def test_authority_mutation_with_expansion_escalates(tmp_path: Path) -> None:
    repo = _make_workspace(tmp_path)
    _install_envelope(tmp_path, make_envelope())
    audit_dir = _agent_dir(tmp_path) / "audit" / "autonomy_decisions"
    decision = _process_bash(repo, f"rm {audit_dir}/*.yaml")
    assert decision.action == "ask"
    assert "expansion" in decision.reason
