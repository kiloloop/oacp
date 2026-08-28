# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Tests for the org-memory initializer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from init_org_memory import initialize_org_memory  # noqa: E402


def test_init_creates_full_layout(tmp_path: Path) -> None:
    result = initialize_org_memory(tmp_path)

    org = tmp_path / "org-memory"
    assert (org / "events").is_dir()
    assert (org / "debriefs").is_dir()
    assert (org / "debriefs" / ".gitkeep").is_file()
    for scaffold in ("recent.md", "decisions.md", "rules.md"):
        assert (org / scaffold).is_file()
    assert "debriefs/.gitkeep" in result["created"]


def test_init_is_idempotent(tmp_path: Path) -> None:
    initialize_org_memory(tmp_path)
    result = initialize_org_memory(tmp_path)

    assert result["created"] == []
    assert "debriefs/.gitkeep" in result["skipped"]


def test_init_backfills_debriefs_on_existing_store(tmp_path: Path) -> None:
    # An org-memory tree created before the debrief store existed gains
    # debriefs/ on re-init without touching existing content.
    org = tmp_path / "org-memory"
    (org / "events").mkdir(parents=True)
    (org / "recent.md").write_text("# Recent\nexisting\n", encoding="utf-8")

    result = initialize_org_memory(tmp_path)

    assert (org / "debriefs" / ".gitkeep").is_file()
    assert (org / "recent.md").read_text(encoding="utf-8") == "# Recent\nexisting\n"
    assert "recent.md" in result["skipped"]
