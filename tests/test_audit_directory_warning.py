# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Synthetic custom-location diagnostics; no live receiver state is used."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import autonomy_gate  # noqa: E402
import oacp_doctor  # noqa: E402


@pytest.mark.parametrize("name,warns", [("autonomy_decisions", False), ("audit", True)])
def test_gate_warns_without_changing_decision_or_output_path(tmp_path, capsys, name, warns):
    config = tmp_path / "config.yaml"
    config.write_text("autonomy:\n  default_mode: always_pause\n")
    message = tmp_path / "message.yaml"
    message.write_text('id: msg-synthetic\nfrom: author\nto: reviewer\ntype: question\npriority: P2\ncreated_at_utc: "2026-01-01T00:00:00Z"\nsubject: Test\nbody: Test\n')
    audit = tmp_path / name
    assert autonomy_gate.main(["--config", str(config), "--message", str(message),
                               "--receiver", "reviewer", "--audit-dir", str(audit),
                               "--oacp-dir", str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert ("expected basename 'autonomy_decisions'" in output.err) is warns
    assert json.loads(output.out)["decision"] == "paused"
    assert len(list(audit.glob("*.yaml"))) == 1


@pytest.mark.parametrize("name,warns", [("autonomy_decisions", False), ("custom", True)])
def test_doctor_reports_supplied_location_without_scanning_it(tmp_path, monkeypatch, capsys, name, warns):
    monkeypatch.setattr(oacp_doctor, "run_doctor", lambda **kwargs: [])
    path = tmp_path / name
    assert oacp_doctor.main(["--oacp-dir", str(tmp_path), "--audit-dir", str(path), "--json"]) == 0
    output = capsys.readouterr()
    assert ("expected basename 'autonomy_decisions'" in output.err) is warns
    report = json.loads(output.out)
    assert report["categories"][0]["name"] == "Audit"
    assert report["categories"][0]["results"][0]["severity"] == ("warn" if warns else "ok")
    assert not path.exists()
