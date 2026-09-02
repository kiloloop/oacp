# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Package-content test: build the wheel and assert the shipped file set.

Requires `build` and `hatchling` (both pinned in the dev dependency group);
skipped when either is unavailable. The build runs with --no-isolation so the
pinned dev toolchain is what builds the asserted wheel.
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Set

import pytest

pytest.importorskip("build")
pytest.importorskip("hatchling")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from preflight import _iter_script_files, parse_force_include  # noqa: E402

KERNEL_DOCS = [
    "oacp/_protocol/inbox_outbox.md",
    "oacp/_protocol/message_signing.md",
    "oacp/_protocol/autonomy.md",
    "oacp/_protocol/org_memory.md",
]
WIRE_TEMPLATE = "oacp/_templates/inbox_message.template.yaml"
REMOVED_SCRIPTS = [
    "oacp/_scripts/normalize_findings.py",
    "oacp/_scripts/create_handoff_packet.py",
    "oacp/_scripts/init_project_workspace.sh",
    "oacp/_scripts/session_lifecycle_hooks.py",
]


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory) -> Set[str]:
    outdir = tmp_path_factory.mktemp("wheel")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(outdir),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.fail(
            "wheel build failed:\n"
            + completed.stdout[-2000:]
            + completed.stderr[-2000:]
        )
    wheels = list(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    with zipfile.ZipFile(wheels[0]) as zf:
        return set(zf.namelist())


class TestKernelDocsShipped:
    def test_kernel_docs_in_wheel(self, wheel_names):
        missing = [doc for doc in KERNEL_DOCS if doc not in wheel_names]
        assert not missing, f"kernel docs missing from wheel: {missing}"

    def test_wire_template_in_wheel(self, wheel_names):
        assert WIRE_TEMPLATE in wheel_names


class TestForceIncludeParity:
    def test_every_force_include_destination_shipped(self, wheel_names):
        entries, errors = parse_force_include(REPO_ROOT / "pyproject.toml")
        assert not errors, errors
        missing = [dst for _, dst in entries if dst not in wheel_names]
        assert not missing, f"force-include destinations missing from wheel: {missing}"

    def test_every_script_on_disk_shipped(self, wheel_names):
        # End-to-end closure of the scripts/ == force-include rule: every
        # regular file under scripts/ must land in the wheel at the
        # oacp/_scripts/ destination, independent of the table contents.
        expected = {
            "oacp/_scripts/" + rel[len("scripts/") :]
            for rel in _iter_script_files(REPO_ROOT)
        }
        missing = sorted(expected - wheel_names)
        assert not missing, f"scripts missing from wheel: {missing}"


class TestRemovedScriptsAbsent:
    def test_removed_scripts_not_shipped(self, wheel_names):
        present = [name for name in REMOVED_SCRIPTS if name in wheel_names]
        assert not present, f"removed scripts still in wheel: {present}"


# Retained shipped tools and templates must not direct users to the retired
# entry points. Userland protocol docs are deliberately excluded — their
# content is handled by a separate documentation-packaging change.
RETIRED_REFERENCE_PATTERNS = [
    re.compile(r"init_project_workspace\.sh"),
    re.compile(r"create_handoff_packet"),
    re.compile(r"normalize_findings"),
    re.compile(r"\bmake init\b"),
    re.compile(r"\bmake handoff\b"),
    re.compile(r"\bmake normalize\b"),
    re.compile(r"\bsession_lifecycle_hooks\b"),
    re.compile(r"\boacp_coordinator\b"),
    re.compile(r"\bmcp_servers\b"),
]


class TestNoRetiredReferences:
    def test_shipped_tools_and_templates_free_of_retired_references(self):
        hits = []
        roots = [REPO_ROOT / "scripts", REPO_ROOT / "templates"]
        for root in roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(REPO_ROOT).as_posix()
                if "__pycache__" in rel:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(text.splitlines(), start=1):
                    for pattern in RETIRED_REFERENCE_PATTERNS:
                        if pattern.search(line):
                            hits.append(f"{rel}:{lineno}: {line.strip()[:80]}")
        assert not hits, "retired-script references in shipped files:\n" + "\n".join(
            hits
        )
