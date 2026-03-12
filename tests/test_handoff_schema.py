# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for handoff schema validation helpers."""

from __future__ import annotations

import unittest

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from handoff_schema import (  # noqa: E402
    validate_handoff_complete_text,
    validate_handoff_packet_text,
)


VALID_HANDOFF_BODY = """\
source_agent: "codex"
target_agent: "claude"
intent: "Transfer issue #77 context"

artifacts_to_review:
  - "PR #83"

definition_of_done:
  - "Open review-ready PR"

context_bundle:
  files_touched:
    - path: "scripts/send_inbox_message.py"
      rationale: "Add handoff schema enforcement"
  decisions_made:
    - decision: "Use YAML body schema for handoff packets"
      alternatives_considered:
        - "Top-level message fields"
  blockers_hit:
    - blocker: "none"
      workarounds_attempted:
        - "n/a"
  suggested_next_steps:
    - "Review and continue implementation"
"""


class TestHandoffPacketSchema(unittest.TestCase):
    def test_valid_packet(self) -> None:
        self.assertEqual(validate_handoff_packet_text(VALID_HANDOFF_BODY), [])

    def test_missing_required_context_field(self) -> None:
        broken = VALID_HANDOFF_BODY.replace("  blockers_hit:\n", "")
        errors = validate_handoff_packet_text(broken)
        self.assertTrue(any("blockers_hit" in err for err in errors))

    def test_source_and_target_must_differ(self) -> None:
        broken = VALID_HANDOFF_BODY.replace('target_agent: "claude"', 'target_agent: "codex"')
        errors = validate_handoff_packet_text(broken)
        self.assertTrue(any("must differ" in err for err in errors))


class TestHandoffCompleteSchema(unittest.TestCase):
    def test_valid_handoff_complete(self) -> None:
        body = """\
issue: "#77"
pr: "84"
branch: "codex/issue-77-handoff-protocol"
tests_run: "make test"
next_owner: "claude"
"""
        self.assertEqual(validate_handoff_complete_text(body), [])

    def test_requires_numeric_pr(self) -> None:
        body = """\
issue: "#77"
pr: "not-a-pr"
branch: "codex/issue-77-handoff-protocol"
tests_run: "make test"
next_owner: "claude"
"""
        errors = validate_handoff_complete_text(body)
        self.assertTrue(any("field 'pr'" in err for err in errors))

    def test_requires_owner(self) -> None:
        body = """\
issue: "#77"
pr: "84"
branch: "codex/issue-77-handoff-protocol"
tests_run: "make test"
"""
        errors = validate_handoff_complete_text(body)
        self.assertTrue(any("next_owner" in err for err in errors))


if __name__ == "__main__":
    unittest.main()
