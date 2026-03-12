# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for validate_message.py — inbox protocol v2 features."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

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


class TestBackwardCompatStringTo(unittest.TestCase):
    def test_backward_compat_string_to(self):
        """v1 messages with string 'to' field must still validate."""
        msg = _base_msg(to="codex")
        errors = validate_message_dict(msg)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
