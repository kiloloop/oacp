# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for review loop message type validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_message import validate_message_dict  # noqa: E402


def _make_message(**overrides):
    """Build a valid baseline message dict, applying any overrides."""
    base = {
        "id": "msg-20260213T1000Z-claude-001",
        "from": "claude",
        "to": "codex",
        "type": "review_request",
        "priority": "P1",
        "created_at_utc": "2026-02-13T10:00:00Z",
        "subject": "Review: test PR",
        "body": "pr: 86\nbranch: claude/test\ndiff_summary: test changes",
    }
    base.update(overrides)
    return base


class TestReviewRequestType(unittest.TestCase):
    """Tests for review_request message type."""

    def test_review_request_accepted(self):
        msg = _make_message(type="review_request")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_review_request_with_related_pr(self):
        msg = _make_message(type="review_request", related_pr="86")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_review_request_with_all_body_fields(self):
        msg = _make_message(
            type="review_request",
            body="pr: 86\nbranch: claude/review-loop\ndiff_summary: adds protocol\nhandoff_ref: packets/handoff/20260213.yaml",
        )
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


class TestReviewFeedbackType(unittest.TestCase):
    """Tests for review_feedback message type."""

    def test_review_feedback_accepted(self):
        msg = _make_message(
            type="review_feedback",
            body="findings_packet: packets/findings/20260213_test_r1.yaml\nround: 1\nblocking_count: 2",
        )
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_review_feedback_with_findings_packet_path(self):
        msg = _make_message(
            type="review_feedback",
            body="findings_packet: packets/findings/20260213_review_loop_codex_r1.yaml\nround: 1\nblocking_count: 0",
        )
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_review_feedback_with_escalation(self):
        msg = _make_message(
            type="review_feedback",
            body="findings_packet: packets/findings/test_r3.yaml\nround: 3\nblocking_count: 1\nescalation: max_rounds_exceeded",
        )
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


class TestReviewAddressedType(unittest.TestCase):
    """Tests for review_addressed message type."""

    def test_review_addressed_accepted(self):
        msg = _make_message(
            type="review_addressed",
            body="commit_sha: abc1234\nchanges_summary: Fixed blocking issue\nround: 1",
        )
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


class TestReviewLgtmType(unittest.TestCase):
    """Tests for review_lgtm message type."""

    def test_review_lgtm_accepted(self):
        msg = _make_message(
            type="review_lgtm",
            body="quality_gate_result: pass\nmerge_ready: true",
        )
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_review_lgtm_with_quality_gate_result(self):
        msg = _make_message(
            type="review_lgtm",
            body="quality_gate_result: pass\nmerge_ready: true",
        )
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])
        # Verify body is preserved (not stripped)
        self.assertIn("quality_gate_result", msg["body"])


class TestExistingTypesStillWork(unittest.TestCase):
    """Backward compatibility: existing message types remain valid."""

    def test_task_request(self):
        msg = _make_message(type="task_request")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_question(self):
        msg = _make_message(type="question")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_notification(self):
        msg = _make_message(type="notification")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_handoff(self):
        # handoff has body schema requirements, so use a minimal valid body
        msg = _make_message(type="handoff", body=(
            'source_agent: "claude"\n'
            'target_agent: "codex"\n'
            'intent: "Transfer work"\n'
            "artifacts_to_review:\n"
            '  - "PR #1"\n'
            "definition_of_done:\n"
            '  - "Tests pass"\n'
            "context_bundle:\n"
            "  files_touched:\n"
            '    - path: "file.py"\n'
            '      rationale: "Changed"\n'
            "  decisions_made:\n"
            '    - decision: "Use X"\n'
            "      alternatives_considered:\n"
            '        - "Y"\n'
            "  blockers_hit:\n"
            '    - blocker: "none"\n'
            "      workarounds_attempted:\n"
            '        - "n/a"\n'
            "  suggested_next_steps:\n"
            '    - "Continue"\n'
        ))
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_handoff_complete(self):
        msg = _make_message(type="handoff_complete", body=(
            'issue: "#77"\n'
            'pr: "84"\n'
            'branch: "codex/issue-77"\n'
            'tests_run: "make test"\n'
            'next_owner: "claude"\n'
        ))
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


class TestInvalidTypesRejected(unittest.TestCase):
    """Invalid message types must be rejected."""

    def test_invalid_type_rejected(self):
        msg = _make_message(type="invalid_type")
        errors = validate_message_dict(msg)
        self.assertTrue(any("type" in err for err in errors))

    def test_empty_type_rejected(self):
        msg = _make_message(type="")
        errors = validate_message_dict(msg)
        self.assertTrue(len(errors) > 0)

    def test_review_typo_rejected(self):
        msg = _make_message(type="review_requst")  # typo
        errors = validate_message_dict(msg)
        self.assertTrue(any("type" in err for err in errors))


class TestMessageFieldValidation(unittest.TestCase):
    """Field-level validation still applies to review loop types."""

    def test_missing_required_field(self):
        msg = _make_message(type="review_request")
        del msg["subject"]
        errors = validate_message_dict(msg)
        self.assertTrue(any("subject" in err for err in errors))

    def test_invalid_priority(self):
        msg = _make_message(type="review_feedback", priority="P9")
        errors = validate_message_dict(msg)
        self.assertTrue(any("priority" in err for err in errors))

    def test_invalid_agent_name(self):
        msg = _make_message(type="review_lgtm")
        msg["from"] = "invalid agent name with spaces"
        errors = validate_message_dict(msg)
        self.assertTrue(any("from" in err for err in errors))

    def test_invalid_timestamp(self):
        msg = _make_message(type="review_addressed", created_at_utc="not-a-date")
        errors = validate_message_dict(msg)
        self.assertTrue(any("created_at_utc" in err for err in errors))


if __name__ == "__main__":
    unittest.main()
