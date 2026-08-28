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
    check_packaging_boundary,
    check_yaml_syntax,
    parse_force_include,
    run_preflight,
    validate_makefile_phony,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_force_include(repo: Path, entries: Sequence[str]) -> None:
    lines = ["[tool.hatch.build.targets.wheel.force-include]"]
    lines.extend(entries)
    _write(repo / "pyproject.toml", "\n".join(lines) + "\n")


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


class TestPackagingBoundary(unittest.TestCase):
    def test_matching_boundary_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(repo / "scripts" / "a.py", "print('ok')\n")
            _write(repo / "docs" / "protocol" / "spec.md", "# spec\n")
            _write_force_include(
                repo,
                [
                    '"scripts/a.py" = "oacp/_scripts/a.py"',
                    '"docs/protocol/spec.md" = "oacp/_protocol/spec.md"',
                ],
            )

            result = check_packaging_boundary(repo)
            self.assertTrue(result.passed, result.details)

    def test_unpackaged_script_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(repo / "scripts" / "a.py", "print('ok')\n")
            _write(repo / "scripts" / "orphan.py", "print('ok')\n")
            _write_force_include(repo, ['"scripts/a.py" = "oacp/_scripts/a.py"'])

            result = check_packaging_boundary(repo)
            self.assertFalse(result.passed)
            self.assertIn("scripts/orphan.py", result.details)
            self.assertIn("missing from force-include", result.details)

    def test_force_include_without_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(repo / "scripts" / "a.py", "print('ok')\n")
            _write_force_include(
                repo,
                [
                    '"scripts/a.py" = "oacp/_scripts/a.py"',
                    '"scripts/ghost.py" = "oacp/_scripts/ghost.py"',
                ],
            )

            result = check_packaging_boundary(repo)
            self.assertFalse(result.passed)
            self.assertIn("scripts/ghost.py", result.details)
            self.assertIn("no file on disk", result.details)

    def test_missing_table_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(repo / "scripts" / "a.py", "print('ok')\n")
            _write(repo / "pyproject.toml", "[project]\nname = 'x'\n")

            result = check_packaging_boundary(repo)
            self.assertFalse(result.passed)
            self.assertIn("missing", result.details)

    def test_missing_pyproject_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = check_packaging_boundary(Path(td))
            self.assertFalse(result.passed)
            self.assertIn("pyproject.toml not found", result.details)

    def test_duplicate_source_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(repo / "scripts" / "a.py", "print('ok')\n")
            _write_force_include(
                repo,
                [
                    '"scripts/a.py" = "oacp/_scripts/a.py"',
                    '"scripts/a.py" = "oacp/_scripts/duplicate.py"',
                ],
            )

            result = check_packaging_boundary(repo)
            self.assertFalse(result.passed)
            self.assertIn("duplicate force-include source", result.details)

    def test_unparseable_line_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(repo / "scripts" / "a.py", "print('ok')\n")
            _write_force_include(
                repo,
                [
                    '"scripts/a.py" = "oacp/_scripts/a.py"',
                    "not-a-valid-entry",
                ],
            )

            result = check_packaging_boundary(repo)
            self.assertFalse(result.passed)
            self.assertIn("unparseable force-include line", result.details)

    def test_parse_stops_at_next_table(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write(
                repo / "pyproject.toml",
                "\n".join(
                    [
                        "[tool.hatch.build.targets.wheel.force-include]",
                        '"scripts/a.py" = "oacp/_scripts/a.py"',
                        "[tool.other]",
                        '"scripts/ignored.py" = "oacp/_scripts/ignored.py"',
                    ]
                )
                + "\n",
            )

            entries, errors = parse_force_include(repo / "pyproject.toml")
            self.assertEqual(errors, [])
            self.assertEqual(entries, [("scripts/a.py", "oacp/_scripts/a.py")])


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
            _write_force_include(
                repo,
                [
                    '"scripts/a.py" = "oacp/_scripts/a.py"',
                    '"scripts/a.sh" = "oacp/_scripts/a.sh"',
                ],
            )

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
            _write_force_include(
                repo,
                [
                    '"scripts/a.py" = "oacp/_scripts/a.py"',
                    '"scripts/a.sh" = "oacp/_scripts/a.sh"',
                ],
            )

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
