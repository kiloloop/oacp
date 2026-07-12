# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for validate_message.py — inbox protocol v2 features."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _oacp_constants import AGENT_RE  # noqa: E402
from validate_message import validate_message_dict  # noqa: E402


def _base_msg(**overrides):
    """Build a minimal valid message dict with optional overrides."""
    msg = {
        "id": "msg-20260213120000-claude-ab12",
        "from": "claude",
        "to": "codex",
        "type": "task_request",
        "priority": "P2",
        "created_at_utc": "2026-02-13T12:00:00Z",
        "subject": "Test subject",
        "body": "Test body",
    }
    msg.update(overrides)
    return msg


class TestToListAccepted(unittest.TestCase):
    def test_to_list_accepted(self):
        msg = _base_msg(to=["codex", "gemini"])
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_to_single_string_still_works(self):
        msg = _base_msg(to="codex")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


class TestToListMaxExceeded(unittest.TestCase):
    def test_to_list_max_exceeded(self):
        recipients = [f"agent{i}" for i in range(11)]
        msg = _base_msg(to=recipients)
        errors = validate_message_dict(msg)
        self.assertTrue(any("exceeds maximum of 10" in e for e in errors))


class TestToListSelfSend(unittest.TestCase):
    def test_sender_in_list_rejected(self):
        msg = _base_msg(**{"from": "claude", "to": ["claude", "codex"]})
        errors = validate_message_dict(msg)
        self.assertTrue(any("must not include the sender" in e for e in errors))

    def test_sender_with_leading_underscore_rejected(self):
        msg = _base_msg(**{"from": "_bot"})
        errors = validate_message_dict(msg)
        self.assertTrue(any(AGENT_RE.pattern in e for e in errors))


class TestHandoffBroadcastRejected(unittest.TestCase):
    def test_handoff_broadcast_rejected(self):
        msg = _base_msg(type="handoff", to=["codex", "gemini"])
        # handoff body validation will also fail, but we check for broadcast error
        errors = validate_message_dict(msg)
        self.assertTrue(any("does not support broadcast" in e for e in errors))


class TestExpiresAtValid(unittest.TestCase):
    def test_expires_at_valid(self):
        msg = _base_msg(expires_at="2026-02-14T12:00:00Z")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_expires_at_empty_ok(self):
        msg = _base_msg()  # no expires_at
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


class TestExpiresAtInvalidFormat(unittest.TestCase):
    def test_expires_at_invalid_format(self):
        msg = _base_msg(expires_at="not-a-date")
        errors = validate_message_dict(msg)
        self.assertTrue(any("expires_at" in e for e in errors))

    def test_expires_at_wrong_format(self):
        msg = _base_msg(expires_at="2026-02-14 12:00:00")
        errors = validate_message_dict(msg)
        self.assertTrue(any("expires_at" in e for e in errors))


class TestChannelAccepted(unittest.TestCase):
    def test_channel_accepted(self):
        msg = _base_msg(channel="review")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_channel_with_hyphens(self):
        msg = _base_msg(channel="my-channel_01")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


class TestChannelTooLong(unittest.TestCase):
    def test_channel_too_long(self):
        msg = _base_msg(channel="a" * 65)
        errors = validate_message_dict(msg)
        self.assertTrue(any("channel" in e for e in errors))

    def test_channel_invalid_chars(self):
        msg = _base_msg(channel="my channel!")
        errors = validate_message_dict(msg)
        self.assertTrue(any("channel" in e for e in errors))


class TestAutonomyHintAccepted(unittest.TestCase):
    def test_autonomy_hint_accepted(self):
        msg = _base_msg(autonomy_hint="auto_proceed")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])

    def test_autonomy_hint_invalid_rejected(self):
        msg = _base_msg(autonomy_hint="totally_invalid_value")
        errors = validate_message_dict(msg)
        self.assertTrue(any("autonomy_hint" in e and "auto_proceed" in e for e in errors))

    def test_autonomy_hint_must_be_scalar(self):
        msg = _base_msg(autonomy_hint={"mode": "auto_proceed"})
        errors = validate_message_dict(msg)
        self.assertTrue(any("autonomy_hint" in e for e in errors))


class TestReviewTelemetryFields(unittest.TestCase):
    def test_all_six_fields_are_accepted_for_review_messages(self):
        msg = _base_msg(
            type="review_lgtm",
            model="gpt-5",
            turns=3,
            input_tokens=1200,
            output_tokens=450,
            wall_time_s=12.5,
            est_cost_usd=0.42,
        )
        self.assertEqual(validate_message_dict(msg), [])

    def test_numeric_strings_from_yaml_base_loader_are_accepted(self):
        msg = _base_msg(
            type="review_feedback",
            model="claude-opus",
            turns="2",
            input_tokens="1000",
            output_tokens="250",
            wall_time_s="8.75",
            est_cost_usd="0.10",
        )
        self.assertEqual(validate_message_dict(msg), [])

    def test_telemetry_is_rejected_outside_review_loop(self):
        errors = validate_message_dict(_base_msg(model="gpt-5"))
        self.assertTrue(any("review telemetry fields" in error for error in errors))

    def test_invalid_telemetry_types_are_rejected(self):
        msg = _base_msg(
            type="review_addressed",
            model="",
            turns=-1,
            input_tokens="many",
            output_tokens=True,
            wall_time_s="NaN",
            est_cost_usd=-0.01,
        )
        errors = validate_message_dict(msg)
        for field in (
            "model",
            "turns",
            "input_tokens",
            "output_tokens",
            "wall_time_s",
            "est_cost_usd",
        ):
            self.assertTrue(any(field in error for error in errors), field)


class TestBackwardCompatStringTo(unittest.TestCase):
    def test_backward_compat_string_to(self):
        """v1 messages with string 'to' field must still validate."""
        msg = _base_msg(to="codex")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
