# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for write_event.py — related field parsing and event building."""

from __future__ import annotations

import datetime as dt
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from write_event import _normalize_related, build_event, main  # noqa: E402


class TestNormalizeRelated(unittest.TestCase):
    """Test _normalize_related handles both comma-sep and JSON array inputs."""

    def test_comma_separated(self) -> None:
        self.assertEqual(
            _normalize_related("PR #43, event/20260316-foo"),
            ["PR #43", "event/20260316-foo"],
        )

    def test_single_item(self) -> None:
        self.assertEqual(_normalize_related("PR #43"), ["PR #43"])

    def test_json_array(self) -> None:
        self.assertEqual(
            _normalize_related('["PR #13", "PR #14"]'),
            ["PR #13", "PR #14"],
        )

    def test_json_single_item_array(self) -> None:
        self.assertEqual(_normalize_related('["PR #13"]'), ["PR #13"])

    def test_json_empty_array(self) -> None:
        self.assertEqual(_normalize_related("[]"), [])

    def test_malformed_json_falls_back_to_comma_split(self) -> None:
        # Starts with '[' but isn't valid JSON — falls back to comma split
        self.assertEqual(
            _normalize_related("[broken, json"),
            ["[broken", "json"],
        )

    def test_strips_whitespace(self) -> None:
        self.assertEqual(
            _normalize_related("  PR #1 ,  PR #2  "),
            ["PR #1", "PR #2"],
        )

    def test_json_with_leading_whitespace(self) -> None:
        self.assertEqual(
            _normalize_related('  ["PR #1"]  '),
            ["PR #1"],
        )

    def test_skips_empty_items(self) -> None:
        self.assertEqual(
            _normalize_related("PR #1,,, PR #2,"),
            ["PR #1", "PR #2"],
        )


class TestBuildEventRelated(unittest.TestCase):
    """Test that build_event produces correct YAML for related field."""

    NOW = dt.datetime(2026, 3, 21, 12, 0, 0, tzinfo=dt.timezone.utc)

    def _build(self, related: list[str] | None = None) -> str:
        event = build_event(
            agent="claude",
            project="test",
            event_type="event",
            slug="test-slug",
            body="test body",
            related=related,
            now=self.NOW,
        )
        return event["content"]

    def test_no_related_field(self) -> None:
        content = self._build(related=None)
        self.assertNotIn("related:", content)

    def test_plain_items(self) -> None:
        content = self._build(related=["PR #43", "issue #10"])
        self.assertIn('related: ["PR #43", "issue #10"]', content)

    def test_no_double_quoting(self) -> None:
        """Pre-parsed JSON items should not get double-quoted."""
        # Simulate what _normalize_related returns for '["PR #13"]'
        content = self._build(related=["PR #13"])
        self.assertIn('related: ["PR #13"]', content)
        # Must NOT contain nested quotes like ["["PR #13"]"]
        self.assertNotIn('["[', content)


class TestBuildEventDryRun(unittest.TestCase):
    """Test main() dry-run to verify end-to-end related parsing."""

    def test_dry_run_json_related(self) -> None:
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            with redirect_stdout(buf):
                rc = main([
                    "--agent", "claude",
                    "--project", "test",
                    "--type", "event",
                    "--slug", "test-slug",
                    "--body", "hello",
                    "--related", '["PR #13", "PR #14"]',
                    "--oacp-dir", tmpdir,
                    "--dry-run",
                ])
            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn('related: ["PR #13", "PR #14"]', output)
            self.assertNotIn('["[', output)

    def test_dry_run_comma_related(self) -> None:
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            with redirect_stdout(buf):
                rc = main([
                    "--agent", "claude",
                    "--project", "test",
                    "--type", "event",
                    "--slug", "test-slug",
                    "--body", "hello",
                    "--related", "PR #13, PR #14",
                    "--oacp-dir", tmpdir,
                    "--dry-run",
                ])
            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn('related: ["PR #13", "PR #14"]', output)


if __name__ == "__main__":
    unittest.main()
