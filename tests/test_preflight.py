# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/preflight.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Sequence
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from preflight import (  # noqa: E402
    check_conflict_markers,
    check_yaml_syntax,
    run_preflight,
    validate_makefile_phony,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestValidateMakefilePhony(unittest.TestCase):
    def test_detects_missing_phony_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            makefile = Path(td) / "Makefile"
            _write(
                makefile,
                """\
.PHONY: help

help:
\t@echo help

build:
\t@echo build
""",
            )

            _, _, missing_phony, orphan_phony = validate_makefile_phony(makefile)
            self.assertEqual(missing_phony, ["build"])
            self.assertEqual(orphan_phony, [])

    def test_detects_orphan_phony_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            makefile = Path(td) / "Makefile"
            _write(
                makefile,
                """\
.PHONY: help ghost

help:
\t@echo help
""",
            )

            _, _, missing_phony, orphan_phony = validate_makefile_phony(makefile)
            self.assertEqual(missing_phony, [])
            self.assertEqual(orphan_phony, ["ghost"])


class TestConflictMarkerScan(unittest.TestCase):
    def test_conflict_markers_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            marker_head = "<" * 7 + " HEAD"
            marker_mid = "=" * 7
            marker_tail = ">" * 7 + " branch"
            _write(
                repo / "README.md",
                "\n".join(
                    [
                        "Start",
                        marker_head,
                        "mine",
                        marker_mid,
                        "theirs",
                        marker_tail,
                    ]
                )
                + "\n",
            )

            def runner(command: Sequence[str], _cwd: Path):
                if list(command) == ["git", "ls-files"]:
                    return 0, "README.md\n"
                return 0, ""

            result = check_conflict_markers(repo, runner=runner)
            self.assertFalse(result.passed)
            self.assertIn("README.md", result.details)


class TestYamlValidation(unittest.TestCase):
    def test_invalid_yaml_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(repo / "templates" / "ok.yaml", "key: value\n")
            _write(repo / "docs" / "protocol" / "bad.yaml", "invalid: [\n")

            def fake_loader(text: str):
                if text == "invalid: [\n":
                    raise ValueError("parse error")
                return {}

            result = check_yaml_syntax(repo, loader=fake_loader)
            self.assertFalse(result.passed)
            self.assertIn("bad.yaml", result.details)


class TestRunPreflight(unittest.TestCase):
    def test_full_mode_runs_make_test(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(
                repo / "Makefile",
                """\
.PHONY: test preflight

test:
\t@echo ok

preflight:
\t@echo ok
""",
            )
            _write(repo / "templates" / "sample.yaml", "id: 1\n")
            _write(repo / "docs" / "protocol" / "sample.yaml", "name: proto\n")
            _write(repo / "scripts" / "a.py", "print('ok')\n")
            _write(repo / "scripts" / "a.sh", "#!/usr/bin/env bash\necho ok\n")

            calls: List[List[str]] = []

            def runner(command: Sequence[str], _cwd: Path):
                calls.append(list(command))
                if list(command) == ["git", "ls-files"]:
                    return 0, "\n".join(
                        [
                            "Makefile",
                            "templates/sample.yaml",
                            "docs/protocol/sample.yaml",
                            "scripts/a.py",
                            "scripts/a.sh",
                        ]
                    )
                return 0, ""

            with mock.patch("preflight.shutil.which", return_value="/usr/bin/tool"):
                results = run_preflight(
                    repo,
                    full=True,
                    runner=runner,
                    yaml_loader=lambda _text: {},
                )

            self.assertTrue(all(item.passed for item in results))
            self.assertIn(["ruff", "check", "scripts/a.py"], calls)
            self.assertIn(["shellcheck", "scripts/a.sh"], calls)
            self.assertIn(["make", "test", "ARGS="], calls)

    def test_fast_mode_skips_make_test(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(
                repo / "Makefile",
                """\
.PHONY: preflight

preflight:
\t@echo ok
""",
            )
            _write(repo / "templates" / "sample.yaml", "id: 1\n")
            _write(repo / "docs" / "protocol" / "sample.yaml", "name: proto\n")
            _write(repo / "scripts" / "a.py", "print('ok')\n")
            _write(repo / "scripts" / "a.sh", "#!/usr/bin/env bash\necho ok\n")

            calls: List[List[str]] = []

            def runner(command: Sequence[str], _cwd: Path):
                calls.append(list(command))
                if list(command) == ["git", "ls-files"]:
                    return 0, "\n".join(
                        [
                            "Makefile",
                            "templates/sample.yaml",
                            "docs/protocol/sample.yaml",
                            "scripts/a.py",
                            "scripts/a.sh",
                        ]
                    )
                return 0, ""

            with mock.patch("preflight.shutil.which", return_value="/usr/bin/tool"):
                results = run_preflight(
                    repo,
                    full=False,
                    runner=runner,
                    yaml_loader=lambda _text: {},
                )

            self.assertTrue(all(item.passed for item in results))
            self.assertNotIn(["make", "test"], calls)


if __name__ == "__main__":
    unittest.main()
