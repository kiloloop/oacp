# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0

"""Package-content test: build the wheel and assert the shipped file set.

Requires `build` and `hatchling` (both pinned in the dev dependency group);
skipped when either is unavailable. The build runs with --no-isolation so the
pinned dev toolchain is what builds the asserted wheel.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Iterable, List, Set

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
    "oacp/_scripts/init_org_memory.py",
    "oacp/_scripts/promote_to_archive.py",
    "oacp/_scripts/restore_from_archive.py",
]
# The memory engine ships as `agent-memory-cli`; the kernel wheel carries no
# memory module and no org-memory templates. Matched by shape, not by name, so
# a re-added module fails here before it is ever listed above. The script
# shape covers a flat module and a package with everything under it, in
# either spelling the preflight memory-boundary guard rejects.
MEMORY_ENGINE_SHAPES = [
    re.compile(r"^oacp/_scripts/(?:memory_|agent_memory)[^/]*(?:/|$)"),
    re.compile(r"^oacp/_templates/org-memory/"),
]
# Force-included into a copy of this tree to prove the shapes against a real
# build: (source path, wheel destination).
PLANTED_ENGINE_FILES = [
    ("scripts/memory_probe/__init__.py", "oacp/_scripts/memory_probe/__init__.py"),
    ("scripts/memory_probe_flat.py", "oacp/_scripts/memory_probe_flat.py"),
    ("templates/org-memory/probe.md", "oacp/_templates/org-memory/probe.md"),
]


def memory_engine_files(names: Iterable[str]) -> List[str]:
    return sorted(
        name for name in names if any(shape.match(name) for shape in MEMORY_ENGINE_SHAPES)
    )


def _build_wheel_names(tree: Path, outdir: Path) -> Set[str]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(outdir),
            str(tree),
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


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory) -> Set[str]:
    return _build_wheel_names(REPO_ROOT, tmp_path_factory.mktemp("wheel"))


@pytest.fixture(scope="module")
def planted_wheel_names(tmp_path_factory) -> Set[str]:
    """Wheel built from a copy of this tree with engine-shaped files force-included."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        pytest.skip("planted build needs a git checkout to copy")
    tree = tmp_path_factory.mktemp("planted-tree")
    for raw in tracked.stdout.split(b"\0"):
        rel = raw.decode()
        source = REPO_ROOT / rel
        if not rel or not source.is_file():
            continue
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    rows = []
    for source_rel, destination in PLANTED_ENGINE_FILES:
        planted = tree / source_rel
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("# planted by test_package_content\n")
        rows.append(f'"{source_rel}" = "{destination}"\n')
    pyproject = tree / "pyproject.toml"
    header = "[tool.hatch.build.targets.wheel.force-include]\n"
    text = pyproject.read_text()
    assert text.count(header) == 1
    pyproject.write_text(text.replace(header, header + "".join(rows), 1))
    return _build_wheel_names(tree, tmp_path_factory.mktemp("planted-wheel"))


class TestKernelDocsShipped:
    def test_kernel_docs_in_wheel(self, wheel_names):
        missing = [doc for doc in KERNEL_DOCS if doc not in wheel_names]
        assert not missing, f"kernel docs missing from wheel: {missing}"

    def test_wire_template_in_wheel(self, wheel_names):
        assert WIRE_TEMPLATE in wheel_names

    def test_protocol_guide_links_resolve_in_source_and_wheel(self, wheel_names):
        entries, errors = parse_force_include(REPO_ROOT / "pyproject.toml")
        assert not errors, errors
        links_checked = 0
        for source, destination in entries:
            if not source.startswith("docs/protocol/"):
                continue
            source_path = REPO_ROOT / source
            links = re.findall(r"\]\((\.\./guides/[^)\s]+)\)", source_path.read_text())
            for link in links:
                link = link.split("#", 1)[0]
                assert (source_path.parent / link).is_file()
                installed_link = posixpath.normpath(
                    posixpath.join(posixpath.dirname(destination), link)
                )
                assert installed_link in wheel_names, f"missing guide: {installed_link}"
                links_checked += 1
        assert links_checked, "no packaged protocol guide links checked"


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

    def test_memory_engine_not_shipped(self, wheel_names):
        present = memory_engine_files(wheel_names)
        assert not present, f"memory engine files in wheel: {present}"

    def test_memory_engine_shapes_trip_on_planted_build(self, planted_wheel_names):
        # The same tree with a memory_* package, a flat memory_* module and an
        # org-memory template force-included: every planted file is caught and
        # nothing the real build ships is.
        planted = {destination for _, destination in PLANTED_ENGINE_FILES}
        assert planted <= planted_wheel_names
        assert set(memory_engine_files(planted_wheel_names)) == planted


class TestMemoryEngineShapes:
    def test_shapes_cover_packages_modules_and_templates(self):
        caught = [
            "oacp/_scripts/memory_sync.py",
            "oacp/_scripts/memory_probe/__init__.py",
            "oacp/_scripts/memory_probe/nested/deep.py",
            "oacp/_scripts/agent_memory.py",
            "oacp/_scripts/agent_memory/__init__.py",
            "oacp/_templates/org-memory/README.md",
            "oacp/_templates/org-memory/events/.gitkeep",
        ]
        kept = [
            "oacp/_scripts/oacp_doctor.py",
            "oacp/_scripts/codex_session_init.py",
            "oacp/_protocol/org_memory.md",
            "oacp/guides/memory-context.md",
            "oacp/_templates/inbox_message.template.yaml",
        ]
        assert memory_engine_files(caught + kept) == sorted(caught)


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
