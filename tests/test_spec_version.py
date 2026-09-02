# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The protocol contract stamp and the spec's own literals move together."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _oacp_constants import SPEC_VERSION  # noqa: E402

SPEC_DOC = REPO_ROOT / "docs" / "protocol" / "autonomy.md"
# The YAML form (`spec_version: "X"`) and the JSON form (`"spec_version": "X"`)
# wherever the spec shows a stamped artifact.
STAMP_LITERAL = re.compile(r'"?spec_version"?:\s*"(\d+\.\d+\.\d+)"')


class TestSpecVersionStamp(unittest.TestCase):
    def test_stamp_is_a_release_version(self) -> None:
        self.assertRegex(SPEC_VERSION, r"^\d+\.\d+\.\d+$")

    def test_spec_literals_match_the_stamp(self) -> None:
        literals = STAMP_LITERAL.findall(SPEC_DOC.read_text(encoding="utf-8"))
        self.assertTrue(literals, f"{SPEC_DOC.name} shows no stamped artifact")
        self.assertEqual(
            set(literals),
            {SPEC_VERSION},
            "the spec_version literals in docs/protocol/autonomy.md must move "
            "with SPEC_VERSION (see its 'Contract version' section)",
        )


if __name__ == "__main__":
    unittest.main()
