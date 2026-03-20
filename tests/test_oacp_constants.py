# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for shared OACP constants/helpers."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _oacp_constants import AGENT_RE, _template_path, _write_if_missing, utc_now_iso  # noqa: E402


class TestAgentRegex(unittest.TestCase):
    def test_rejects_leading_punctuation(self) -> None:
        self.assertIsNone(AGENT_RE.fullmatch("_bot"))
        self.assertIsNone(AGENT_RE.fullmatch(".bot"))
        self.assertIsNone(AGENT_RE.fullmatch("-bot"))

    def test_accepts_alphanumeric_lead(self) -> None:
        self.assertIsNotNone(AGENT_RE.fullmatch("iris"))
        self.assertIsNotNone(AGENT_RE.fullmatch("agent_1"))


class TestUtcNowIso(unittest.TestCase):
    def test_formats_aware_timestamp(self) -> None:
        now = dt.datetime(2026, 3, 20, 12, 34, 56, tzinfo=dt.timezone.utc)
        self.assertEqual(utc_now_iso(now), "2026-03-20T12:34:56Z")

    def test_assumes_naive_timestamp_is_utc(self) -> None:
        now = dt.datetime(2026, 3, 20, 12, 34, 56)
        self.assertEqual(utc_now_iso(now), "2026-03-20T12:34:56Z")


class TestWriteIfMissing(unittest.TestCase):
    def test_writes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "file.txt"
            self.assertTrue(_write_if_missing(path, "hello"))
            self.assertFalse(_write_if_missing(path, "goodbye"))
            self.assertEqual(path.read_text(encoding="utf-8"), "hello")


class TestTemplatePath(unittest.TestCase):
    def test_resolves_repo_template(self) -> None:
        with _template_path("agent_card.template.yaml") as path:
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
