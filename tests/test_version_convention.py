# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The package version is a release or the next patch's ``.dev0`` build.

Between cuts the development branch carries ``X.Y.Z.dev0`` (docs/guides/
versioning.md, "Dev Builds Between Cuts"); at a cut the release-bump PR
replaces the suffix with the release number and adds the matching CHANGELOG
section. A version that is neither shape, or a dev build that does not sit
above the last recorded release, is a missed step in that cycle.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

VERSION_LINE = re.compile(r'^version\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)
VERSION_SHAPE = re.compile(r"^(?P<release>\d+\.\d+\.\d+)(?P<dev>\.dev0)?$")
RELEASE_HEADING = re.compile(r"^## \[(?P<version>\d+\.\d+\.\d+)\]", re.MULTILINE)


def _release_tuple(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


class TestVersionConvention(unittest.TestCase):
    def setUp(self) -> None:
        match = VERSION_LINE.search(PYPROJECT.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "pyproject.toml carries no version line")
        self.version = match.group("version")
        headings = RELEASE_HEADING.findall(CHANGELOG.read_text(encoding="utf-8"))
        self.assertTrue(headings, "CHANGELOG.md records no release section")
        self.last_release = headings[0]

    def test_version_is_a_release_or_a_dev0_build(self) -> None:
        self.assertRegex(
            self.version,
            VERSION_SHAPE,
            "pyproject.toml version must be X.Y.Z (a cut) or X.Y.Z.dev0 "
            "(the post-cut dev bump)",
        )

    def test_version_agrees_with_the_last_changelog_release(self) -> None:
        shape = VERSION_SHAPE.match(self.version)
        self.assertIsNotNone(shape)
        release = _release_tuple(shape.group("release"))
        last = _release_tuple(self.last_release)
        if shape.group("dev"):
            self.assertGreater(
                release,
                last,
                f"dev build {self.version} must sit above the last recorded "
                f"release {self.last_release}",
            )
        else:
            self.assertEqual(
                release,
                last,
                f"released version {self.version} must match the newest "
                f"CHANGELOG section [{self.last_release}] — a cut adds the "
                "section, a post-cut bump adds the .dev0 suffix",
            )


if __name__ == "__main__":
    unittest.main()
