#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Generate the README command table from the CLI help text.

The ``## Commands`` table in ``README.md`` is generated from the ``Commands:``
block of ``oacp.cli.HELP_TEXT`` (what ``oacp --help`` prints) and lives
between two HTML-comment markers.  ``tests/test_readme_commands.py`` fails
when the two drift.

    make docs                                  # regenerate in place
    python3 scripts/gen_readme_commands.py     # print the generated block
    python3 scripts/gen_readme_commands.py --check   # exit 1 on drift
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED: oacp commands — do not edit by hand; run `make docs` -->"
)
END_MARKER = "<!-- END GENERATED: oacp commands -->"

Command = Tuple[str, str]


def load_help_text() -> str:
    """Return ``HELP_TEXT`` from this checkout, never from an installed wheel."""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from oacp.cli import HELP_TEXT

    return HELP_TEXT


def parse_commands(help_text: str) -> List[Command]:
    """Return ``(name, description)`` pairs from the ``Commands:`` block.

    The block starts at the ``Commands:`` line and ends at the first blank
    line or non-indented line after it (the next section header).  Each
    entry is an indented name, two or more spaces, then its description.
    """
    commands: List[Command] = []
    in_block = False
    for line in help_text.splitlines():
        if not in_block:
            in_block = line.strip() == "Commands:"
            continue
        if not line.strip() or not line.startswith("  "):
            break
        parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"cannot parse command line: {line!r}")
        commands.append((parts[0], parts[1]))
    if not commands:
        raise ValueError("no Commands: block found in help text")
    return commands


def render_table(commands: Sequence[Command]) -> str:
    rows = ["| Command | Description |", "|---------|-------------|"]
    for name, description in commands:
        cell = description.replace("|", "\\|")
        rows.append(f"| `oacp {name}` | {cell} |")
    return "\n".join(rows)


def render_block(commands: Sequence[Command]) -> str:
    return "\n".join([BEGIN_MARKER, render_table(commands), END_MARKER])


def extract_block(readme_text: str) -> str:
    """Return the marker-delimited block (markers included) from README text."""
    start = readme_text.find(BEGIN_MARKER)
    end = readme_text.find(END_MARKER, start if start >= 0 else 0)
    if start < 0 or end < 0:
        raise ValueError(
            "README is missing the generated-commands markers "
            f"({BEGIN_MARKER!r} ... {END_MARKER!r})"
        )
    return readme_text[start : end + len(END_MARKER)]


def replace_block(readme_text: str, block: str) -> str:
    return readme_text.replace(extract_block(readme_text), block, 1)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the README command table from oacp --help."
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=README_PATH,
        help="README to read/update (default: the repo README.md)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write", action="store_true", help="rewrite the README block in place"
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 with a diff when the README block is behind the help text",
    )
    args = parser.parse_args(argv)

    commands = parse_commands(load_help_text())
    expected = render_block(commands)

    if not (args.write or args.check):
        print(expected)
        return 0

    readme_text = args.readme.read_text(encoding="utf-8")
    current = extract_block(readme_text)
    if current == expected:
        print(
            f"OK: README command table matches oacp --help ({len(commands)} commands)"
        )
        return 0

    if args.write:
        args.readme.write_text(replace_block(readme_text, expected), encoding="utf-8")
        print(f"Regenerated README command table ({len(commands)} commands)")
        return 0

    sys.stdout.writelines(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=f"{args.readme.name} (current)",
            tofile="oacp --help (expected)",
        )
    )
    print(
        "DRIFT: README command table is behind oacp --help; run `make docs`",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
