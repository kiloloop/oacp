# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for validate_agent_card.py — agent card schema validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_agent_card import validate_agent_card  # noqa: E402


def _base_card(**overrides):
    """Build a minimal valid agent card dict with optional overrides."""
    card = {
        "version": "0.2.0",
        "name": "claude",
        "runtime": "claude",
        "description": "Primary implementer agent",
    }
    card.update(overrides)
    return card


class TestValidCard(unittest.TestCase):
    def test_minimal_valid(self):
        errors = validate_agent_card(_base_card())
        self.assertEqual(errors, [])

    def test_full_card_valid(self):
        card = _base_card(
            model="claude-opus-4-6",
            skills=[
                {
                    "id": "code_review",
                    "name": "Code Review",
                    "description": "Reviews PRs for correctness",
                    "tags": ["review", "qa"],
                },
                {
                    "id": "implementation",
                    "name": "Implementation",
                    "description": "Implements features and fixes",
                    "tags": ["coding"],
                    "examples": ["Fix auth bug", "Add API endpoint"],
                },
            ],
            capabilities={
                "tools": ["Bash", "Read", "Edit"],
                "languages": ["python", "bash"],
                "domains": ["backend", "infra"],
            },
            permissions={
                "allowed_dirs": ["/home/user/project"],
                "denied_dirs": [],
                "allowed_commands": [],
                "denied_commands": [],
                "github_operations": ["pr_comment", "pr_merge"],
                "max_cost_usd_per_run": None,
            },
            availability={
                "schedule": "always",
                "max_concurrent_tasks": 3,
                "timezone": "UTC",
            },
            protocol={
                "inbox_path": "agents/claude/inbox/",
                "outbox_path": "agents/claude/outbox/",
                "supported_message_types": [
                    "task_request",
                    "question",
                    "notification",
                    "review_request",
                    "review_feedback",
                    "review_lgtm",
                    "follow_up",
                ],
            },
        )
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])


class TestRequiredFields(unittest.TestCase):
    def test_missing_version(self):
        card = _base_card()
        del card["version"]
        errors = validate_agent_card(card)
        self.assertTrue(any("version" in e for e in errors))

    def test_missing_name(self):
        card = _base_card()
        del card["name"]
        errors = validate_agent_card(card)
        self.assertTrue(any("name" in e for e in errors))

    def test_missing_runtime(self):
        card = _base_card()
        del card["runtime"]
        errors = validate_agent_card(card)
        self.assertTrue(any("runtime" in e for e in errors))

    def test_missing_description(self):
        card = _base_card()
        del card["description"]
        errors = validate_agent_card(card)
        self.assertTrue(any("description" in e for e in errors))

    def test_empty_name(self):
        errors = validate_agent_card(_base_card(name=""))
        self.assertTrue(any("name" in e for e in errors))


class TestVersion(unittest.TestCase):
    def test_valid_versions(self):
        for v in ("0.1.0", "0.2.0", "1.0.0", "10.20.30"):
            errors = validate_agent_card(_base_card(version=v))
            self.assertEqual(errors, [], f"version {v} should be valid")

    def test_invalid_version(self):
        errors = validate_agent_card(_base_card(version="v1"))
        self.assertTrue(any("version" in e for e in errors))


class TestName(unittest.TestCase):
    def test_valid_names(self):
        for n in ("claude", "codex", "gemini-3", "iris.v2", "agent_1"):
            errors = validate_agent_card(_base_card(name=n))
            self.assertEqual(errors, [], f"name {n} should be valid")

    def test_invalid_name_spaces(self):
        errors = validate_agent_card(_base_card(name="my agent"))
        self.assertTrue(any("name" in e for e in errors))

    def test_invalid_name_slash(self):
        errors = validate_agent_card(_base_card(name="path/traversal"))
        self.assertTrue(any("name" in e for e in errors))


class TestRuntime(unittest.TestCase):
    def test_valid_runtimes(self):
        for rt in ("claude", "codex", "gemini", "human", "unknown"):
            errors = validate_agent_card(_base_card(runtime=rt))
            self.assertEqual(errors, [], f"runtime {rt} should be valid")

    def test_invalid_runtime(self):
        errors = validate_agent_card(_base_card(runtime="gpt"))
        self.assertTrue(any("runtime" in e for e in errors))


class TestSkills(unittest.TestCase):
    def test_valid_skill(self):
        card = _base_card(
            skills=[{"id": "review", "name": "Review", "description": "Reviews code"}]
        )
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])

    def test_skill_missing_id(self):
        card = _base_card(
            skills=[{"name": "Review", "description": "Reviews code"}]
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("skills[0]" in e and "id" in e for e in errors))

    def test_skill_missing_name(self):
        card = _base_card(
            skills=[{"id": "review", "description": "Reviews code"}]
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("skills[0]" in e and "name" in e for e in errors))

    def test_skill_missing_description(self):
        card = _base_card(
            skills=[{"id": "review", "name": "Review"}]
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("skills[0]" in e and "description" in e for e in errors))

    def test_skill_unknown_field(self):
        card = _base_card(
            skills=[{
                "id": "review",
                "name": "Review",
                "description": "Reviews code",
                "priority": "high",
            }]
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("unknown" in e for e in errors))

    def test_duplicate_skill_ids(self):
        card = _base_card(
            skills=[
                {"id": "review", "name": "Review", "description": "Reviews code"},
                {"id": "review", "name": "Review 2", "description": "Also reviews"},
            ]
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_skill_tags_must_be_list(self):
        card = _base_card(
            skills=[{
                "id": "review",
                "name": "Review",
                "description": "Reviews code",
                "tags": "qa",
            }]
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("tags" in e for e in errors))

    def test_skills_not_list(self):
        card = _base_card(skills="code_review")
        errors = validate_agent_card(card)
        self.assertTrue(any("skills" in e and "list" in e for e in errors))


class TestCapabilities(unittest.TestCase):
    def test_valid_capabilities(self):
        card = _base_card(
            capabilities={"tools": ["Bash"], "languages": ["python"], "domains": ["backend"]}
        )
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])

    def test_tools_not_list(self):
        card = _base_card(capabilities={"tools": "Bash"})
        errors = validate_agent_card(card)
        self.assertTrue(any("tools" in e for e in errors))

    def test_capabilities_not_dict(self):
        card = _base_card(capabilities=["headless"])
        errors = validate_agent_card(card)
        self.assertTrue(any("capabilities" in e and "mapping" in e for e in errors))


class TestProtocol(unittest.TestCase):
    def test_valid_message_types(self):
        card = _base_card(
            protocol={"supported_message_types": ["task_request", "notification", "follow_up"]}
        )
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])

    def test_unknown_message_type(self):
        card = _base_card(
            protocol={"supported_message_types": ["task_request", "magic_spell"]}
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("magic_spell" in e for e in errors))

    def test_message_types_not_list(self):
        card = _base_card(
            protocol={"supported_message_types": "task_request"}
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("supported_message_types" in e for e in errors))


class TestAvailability(unittest.TestCase):
    def test_valid_concurrent_tasks(self):
        card = _base_card(availability={"max_concurrent_tasks": 3})
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])

    def test_zero_concurrent_tasks(self):
        card = _base_card(availability={"max_concurrent_tasks": 0})
        errors = validate_agent_card(card)
        self.assertTrue(any("max_concurrent_tasks" in e for e in errors))

    def test_negative_concurrent_tasks(self):
        card = _base_card(availability={"max_concurrent_tasks": -1})
        errors = validate_agent_card(card)
        self.assertTrue(any("max_concurrent_tasks" in e for e in errors))


class TestPermissions(unittest.TestCase):
    def test_valid_permissions(self):
        card = _base_card(
            permissions={
                "allowed_dirs": ["/tmp"],
                "denied_dirs": [],
                "github_operations": ["pr_comment"],
            }
        )
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])

    def test_github_operations_not_list(self):
        card = _base_card(permissions={"github_operations": "pr_comment"})
        errors = validate_agent_card(card)
        self.assertTrue(any("github_operations" in e for e in errors))

    def test_unknown_github_operation(self):
        card = _base_card(
            permissions={"github_operations": ["pr_comment", "delete_all_repos"]}
        )
        errors = validate_agent_card(card)
        self.assertTrue(any("delete_all_repos" in e for e in errors))

    def test_valid_github_operations(self):
        card = _base_card(
            permissions={
                "github_operations": [
                    "pr_comment", "pr_approve", "pr_merge", "pr_create",
                    "issue_create", "issue_comment", "issue_close",
                ]
            }
        )
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])


class TestUnknownFields(unittest.TestCase):
    def test_unknown_top_level_field(self):
        errors = validate_agent_card(_base_card(mood="happy"))
        self.assertTrue(any("unknown" in e for e in errors))


class TestFileValidation(unittest.TestCase):
    """Test validate_agent_card_file for file-based and directory discovery."""

    def test_valid_card_file(self):
        import tempfile
        from validate_agent_card import validate_agent_card_file  # noqa: E402

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                'version: "0.2.0"\n'
                "name: test-agent\n"
                "runtime: claude\n"
                'description: "Test agent"\n'
            )
            f.flush()
            errors = validate_agent_card_file(Path(f.name))
            self.assertEqual(errors, [])
            Path(f.name).unlink()

    def test_nonexistent_file(self):
        from validate_agent_card import validate_agent_card_file  # noqa: E402

        errors = validate_agent_card_file(Path("/nonexistent/agent_card.yaml"))
        self.assertTrue(any("does not exist" in e for e in errors))

    def test_card_with_inline_comments(self):
        """Ensure inline comments are stripped by the fallback parser."""
        import tempfile
        from validate_agent_card import validate_agent_card_file  # noqa: E402

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                'version: "0.2.0"  # card version\n'
                "name: claude  # primary agent\n"
                "runtime: claude  # runtime type\n"
                'description: "Primary implementer"  # role\n'
            )
            f.flush()
            errors = validate_agent_card_file(Path(f.name))
            self.assertEqual(errors, [])
            Path(f.name).unlink()


class TestRoutingRules(unittest.TestCase):
    def test_valid_routing_rules(self):
        card = _base_card(routing_rules={"primary": ["codex"], "avoid": ["gemini"]})
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])

    def test_routing_rules_not_dict(self):
        card = _base_card(routing_rules=["codex"])
        errors = validate_agent_card(card)
        self.assertTrue(any("routing_rules" in e and "mapping" in e for e in errors))

    def test_routing_rules_primary_not_list(self):
        card = _base_card(routing_rules={"primary": "codex"})
        errors = validate_agent_card(card)
        self.assertTrue(any("primary" in e for e in errors))

    def test_routing_rules_avoid_not_list(self):
        card = _base_card(routing_rules={"avoid": "gemini"})
        errors = validate_agent_card(card)
        self.assertTrue(any("avoid" in e for e in errors))


class TestTrustLevel(unittest.TestCase):
    def test_valid_trust_levels(self):
        for tl in ("untrusted", "standard", "elevated", "admin"):
            errors = validate_agent_card(_base_card(trust_level=tl))
            self.assertEqual(errors, [], f"trust_level {tl} should be valid")

    def test_invalid_trust_level(self):
        errors = validate_agent_card(_base_card(trust_level="superuser"))
        self.assertTrue(any("trust_level" in e for e in errors))


class TestQuota(unittest.TestCase):
    def test_valid_quota(self):
        card = _base_card(quota={"reset_day": 15, "warn_threshold": 0.8})
        errors = validate_agent_card(card)
        self.assertEqual(errors, [])

    def test_quota_not_dict(self):
        card = _base_card(quota=100)
        errors = validate_agent_card(card)
        self.assertTrue(any("quota" in e and "mapping" in e for e in errors))

    def test_reset_day_too_low(self):
        card = _base_card(quota={"reset_day": 0})
        errors = validate_agent_card(card)
        self.assertTrue(any("reset_day" in e for e in errors))

    def test_reset_day_too_high(self):
        card = _base_card(quota={"reset_day": 29})
        errors = validate_agent_card(card)
        self.assertTrue(any("reset_day" in e for e in errors))

    def test_warn_threshold_too_low(self):
        card = _base_card(quota={"warn_threshold": -0.1})
        errors = validate_agent_card(card)
        self.assertTrue(any("warn_threshold" in e for e in errors))

    def test_warn_threshold_too_high(self):
        card = _base_card(quota={"warn_threshold": 1.5})
        errors = validate_agent_card(card)
        self.assertTrue(any("warn_threshold" in e for e in errors))

    def test_reset_day_not_int(self):
        card = _base_card(quota={"reset_day": "first"})
        errors = validate_agent_card(card)
        self.assertTrue(any("reset_day" in e for e in errors))

    def test_warn_threshold_not_float(self):
        card = _base_card(quota={"warn_threshold": "high"})
        errors = validate_agent_card(card)
        self.assertTrue(any("warn_threshold" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
