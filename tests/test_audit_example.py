# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""The documented example must retain the real writer's complete record."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import generate_audit_example as generator  # noqa: E402


def example_record(block: str) -> dict:
    return yaml.safe_load(block.split("```yaml\n", 1)[1].split("\n```", 1)[0])


def test_documented_audit_matches_actual_writer() -> None:
    document = generator.DOCUMENT_PATH.read_text(encoding="utf-8")
    assert generator.extract_block(document) == generator.render_block(), (
        "run make docs"
    )


def test_example_is_deterministic_and_ignores_host_model(monkeypatch) -> None:
    first = generator.render_block()
    monkeypatch.setenv(generator.RUNTIME_MODEL_ENV_VAR, "synthetic-host-model")
    monkeypatch.setenv(generator.RUNTIME_EFFORT_ENV_VAR, "high")
    assert generator.render_block() == first
    runtime = example_record(first)["runtime"]
    assert runtime["model"] is None
    assert runtime["effort"] is None
    assert runtime["effort_unknown_reason"]


def test_example_preserves_complete_persisted_schema(monkeypatch) -> None:
    writer = generator.write_audit_record
    captured = {}

    def capture_writer(audit_dir, decision, **kwargs):
        # Realistic extension shape; the extra evidence is synthetic.
        decision = copy.deepcopy(decision)
        decision["future_example_evidence"] = {"nested": [{"value": "synthetic"}]}
        path = writer(audit_dir, decision, **kwargs)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        expected = copy.deepcopy(raw)
        workspace = kwargs["message_path"].parents[3]
        for field in ("message_path", "policy_path"):
            expected[field] = Path(raw[field]).relative_to(workspace).as_posix()
        expected["evaluator"]["content_sha256"] = "<sha256 of evaluator file bytes>"
        expected["evaluator"]["git_sha"] = None
        captured["expected"] = expected
        return path

    monkeypatch.setattr(generator, "write_audit_record", capture_writer)
    record = example_record(generator.render_block())
    assert record == captured["expected"]
    assert record["decision"] == "auto_accepted"
    assert record["result"]["final_state"] == "pending"
    assert record["result"]["threshold_checkpoint"]["evaluated"] is False
    assert record["result"]["human_outcome"]["recorded"] is False
    assert record["scope_envelope_source"] == "task_profile"
    assert record["evaluation_id"].startswith("eval-")


def test_check_detects_drift_and_write_repairs_only_marked_block(
    tmp_path, capsys
) -> None:
    expected = generator.render_block()
    stale = expected.replace("final_state: pending", "final_state: done")
    document = tmp_path / "autonomy.md"
    document.write_text("before\n" + stale + "\nafter\n", encoding="utf-8")
    original = document.read_bytes()
    assert generator.main(["--check", "--document", str(document)]) == 1
    assert "DRIFT" in capsys.readouterr().err
    assert document.read_bytes() == original
    assert generator.main(["--write", "--document", str(document)]) == 0
    assert document.read_text(encoding="utf-8") == "before\n" + expected + "\nafter\n"
    assert generator.main(["--check", "--document", str(document)]) == 0


@pytest.mark.parametrize(
    "document", ["missing", generator.END_MARKER + generator.BEGIN_MARKER]
)
def test_marker_errors_are_not_silently_repaired(document) -> None:
    with pytest.raises(ValueError):
        generator.extract_block(document)


def test_rendering_only_writes_into_temporary_workspace(tmp_path, monkeypatch) -> None:
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("synthetic host state", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OACP_HOME", str(tmp_path / "must-not-create"))
    writer = generator.write_audit_record
    written_paths = []

    def capture_writer(audit_dir, decision, **kwargs):
        path = writer(audit_dir, decision, **kwargs)
        written_paths.append(path)
        assert path.is_relative_to(kwargs["message_path"].parents[3])
        return path

    monkeypatch.setattr(generator, "write_audit_record", capture_writer)
    generator.render_block()
    assert written_paths
    assert all(not path.exists() for path in written_paths)
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "synthetic host state"


def test_make_docs_keeps_readme_generation_and_adds_audit_example() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("\ndocs:", 1)[1].split("\n\n", 1)[0]
    assert "gen_readme_commands.py --write" in recipe
    assert "tools/generate_audit_example.py --write" in recipe
