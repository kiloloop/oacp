# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Pin the memory layout spec to the vendored conformance fixture.

The spec (docs/protocol/org_memory.md) enumerates the layout in a fenced
``oacp-memory-layout`` block and the sync allowlist in a fenced ``gitignore``
block. The fixture (tests/conformance/memory_layout/) carries the same set as
data. This module holds them to each other in both directions, holds the
fixture to its own closure and to the golden's rule lines, and holds the
kernel's project-tier scaffolder to the fixture. The org tier and the sync
allowlist are scaffolded by the memory tool, which vendors this fixture and
pins itself to it the same way.
"""

from __future__ import annotations

import copy
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Set

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from init_project_workspace import initialize_workspace  # noqa: E402

SPEC = REPO_ROOT / "docs" / "protocol" / "org_memory.md"
FIXTURE_DIR = REPO_ROOT / "tests" / "conformance" / "memory_layout"
FIXTURE = FIXTURE_DIR / "layout.yaml"


@pytest.fixture(scope="module")
def fixture() -> Dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden_gitignore(fixture: Dict) -> bytes:
    return (FIXTURE_DIR / fixture["gitignore_golden"]).read_bytes()


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def _fenced_block(text: str, language: str) -> str:
    blocks = re.findall(rf"^```{re.escape(language)}\n(.*?)^```$", text, re.S | re.M)
    assert len(blocks) == 1, f"expected exactly one ```{language} block in the spec, found {len(blocks)}"
    return blocks[0]


def _tier(fixture: Dict, name: str) -> Dict:
    return next(tier for tier in fixture["tiers"] if tier["name"] == name)


def _closure(fixture: Dict) -> Set[str]:
    entries = {fixture["gitignore_file"], fixture["marker_file"], fixture["projects_dir"] + "/"}
    for tier in fixture["tiers"]:
        root = tier["pattern"]
        entries.add(root + "/")
        entries.update(f"{root}/{name}" for name in tier["files"])
        entries.update(f"{root}/{name}/" for name in tier["dirs"] + tier["unsynced"])
    return entries


# --- fixture ↔ itself -------------------------------------------------------


def test_fixture_entries_are_the_closure_of_its_tiers(fixture: Dict) -> None:
    assert set(fixture["entries"]) == _closure(fixture)
    assert len(fixture["entries"]) == len(set(fixture["entries"]))


def test_fixture_wildcard_appears_only_in_the_project_pattern(fixture: Dict) -> None:
    wildcard = fixture["project_wildcard"]
    for tier in fixture["tiers"]:
        parts = tier["pattern"].split("/")
        if tier["name"] == "project":
            assert parts == [fixture["projects_dir"], wildcard, "memory"]
        else:
            assert wildcard not in parts


# --- fixture ↔ golden -------------------------------------------------------


def _rules(fixture: Dict) -> List[str]:
    """The ignore file's rule lines, derived from the fixture fields in allowlist order.

    Allow rules first (deny everything, re-allow directories, the ignore file,
    the marker, each tier's subtree), then the denies that must win over them
    (each tier's unsynced dirs, then the never-synced names last). Comment
    lines are prose and stay out of the derivation.
    """
    allow = ["*", "!*/", "!" + fixture["gitignore_file"], "!" + fixture["marker_file"]]
    allow += ["!" + tier["pattern"] + "/**" for tier in fixture["tiers"]]
    deny = [f"{tier['pattern']}/{name}/" for tier in fixture["tiers"] for name in tier["unsynced"]]
    deny += [name + "/" for name in fixture["never_synced_dirs"]]
    return allow + deny


def _golden_rules(golden: bytes) -> List[str]:
    return [line for line in golden.decode("utf-8").splitlines() if line and not line.startswith("#")]


def test_golden_rules_are_derived_from_the_fixture(fixture: Dict, golden_gitignore: bytes) -> None:
    assert _golden_rules(golden_gitignore) == _rules(fixture), (
        "canonical .gitignore rules and the fixture's tiers / unsynced / never_synced_dirs differ "
        "(order is part of the contract: allow rules, per-tier denies, never-synced names last)"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda f: f["never_synced_dirs"].append("credentials"), id="never-synced-name-added"),
        pytest.param(lambda f: f["never_synced_dirs"].clear(), id="never-synced-name-removed"),
        pytest.param(lambda f: _tier(f, "project")["unsynced"].append("tmp"), id="project-unsynced-dir-added"),
        pytest.param(lambda f: _tier(f, "org")["unsynced"].append(".cache"), id="org-unsynced-dir-added"),
        pytest.param(lambda f: f["tiers"].reverse(), id="tier-order-changed"),
        pytest.param(lambda f: f.update(marker_file=".memory-repo"), id="marker-renamed"),
    ],
)
def test_a_fixture_that_contradicts_the_golden_is_caught(fixture: Dict, golden_gitignore: bytes, mutate) -> None:
    mutated = copy.deepcopy(fixture)
    mutate(mutated)
    assert _rules(mutated) != _golden_rules(golden_gitignore)


# --- spec ↔ fixture ---------------------------------------------------------


def test_spec_layout_block_enumerates_exactly_the_fixture(spec_text: str, fixture: Dict) -> None:
    listed = [line for line in _fenced_block(spec_text, "oacp-memory-layout").splitlines() if line.strip()]
    assert listed == fixture["entries"], "spec Layout block and fixture entries differ (order is part of the contract)"


def test_spec_gitignore_block_is_the_golden_bytes(spec_text: str, golden_gitignore: bytes) -> None:
    assert _fenced_block(spec_text, "gitignore").encode("utf-8") == golden_gitignore


_PATH_TOKEN = re.compile(
    r"`(?:\$OACP_HOME/)?((?:org-memory|projects)/[^`\s]*|\.oacp-memory-repo|\.gitignore|keys/)`"
)


def _resolves(token: str, fixture: Dict, entries: Set[str]) -> bool:
    path = token.replace("<project>", fixture["project_wildcard"])
    path = re.sub(r"<[^>]+>", "x", path)  # other placeholders stand for one segment
    path = path.replace("…", "x").replace("...", "x")
    if path.rstrip("/") + "/" == "keys/":
        return "keys" in fixture["never_synced_dirs"]
    if path in entries or path + "/" in entries:
        return True
    # A path under a fixture directory (e.g. a debrief file under org-memory/debriefs/).
    # The bare projects dir is not a prefix candidate: its non-memory children are not layout.
    container = fixture["projects_dir"] + "/"
    return any(entry.endswith("/") and entry != container and path.startswith(entry) for entry in entries)


def test_every_path_the_spec_names_is_in_the_fixture(spec_text: str, fixture: Dict) -> None:
    entries = set(fixture["entries"])
    unresolved = sorted({tok for tok in _PATH_TOKEN.findall(spec_text) if not _resolves(tok, fixture, entries)})
    assert not unresolved, f"spec names paths outside the fixture: {unresolved}"


def test_every_fixture_entry_is_named_by_the_spec(spec_text: str, fixture: Dict) -> None:
    # The Layout block covers the set; each entry must also surface in prose or
    # a tree somewhere else in the spec, so the grammar is never a bare list.
    prose = spec_text.replace(_fenced_block(spec_text, "oacp-memory-layout"), "")
    missing = [
        entry
        for entry in fixture["entries"]
        if entry.rstrip("/").split("/")[-1] not in prose
    ]
    assert not missing, f"fixture entries the spec never mentions outside the Layout block: {missing}"


# --- kernel ↔ fixture -------------------------------------------------------


def _listing(root: Path) -> List[str]:
    return sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir() if p.name != ".gitkeep")


def test_oacp_init_scaffolds_exactly_the_project_tier(fixture: Dict) -> None:
    tier = _tier(fixture, "project")
    with tempfile.TemporaryDirectory() as tmpdir:
        result = initialize_workspace("demo", oacp_root=Path(tmpdir))
        memory = Path(result["project_root"]) / "memory"
        assert memory == Path(tmpdir) / tier["pattern"].replace(fixture["project_wildcard"], "demo")
        assert _listing(memory) == sorted(tier["files"] + [d + "/" for d in tier["dirs"]])
