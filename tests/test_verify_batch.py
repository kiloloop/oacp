# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Streaming verifier contract; all paths and message identities are synthetic."""

import io
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import message_verify  # noqa: E402


def run_batch(monkeypatch, capsys, text, *args):
    monkeypatch.setattr(sys, "argv", ["verify", "--batch", *args])
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code = message_verify.main()
    output = capsys.readouterr()
    return code, [json.loads(line) for line in output.out.splitlines()], output.err


def test_batch_mixed_paths_keeps_order_and_verdict_shape(tmp_path, monkeypatch, capsys):
    good = tmp_path / " spaced name.yaml"
    good.write_text("from: author\nto: reviewer\n")
    bad = tmp_path / "bad.yaml"
    bad.write_text('from: author\nauth: "broken"\n')
    missing = tmp_path / "missing.yaml"
    code, rows, err = run_batch(monkeypatch, capsys, f"{good}\n{bad}\n{missing}\n\n{good}")
    assert code == 0 and not err
    assert [row["path"] for row in rows] == [str(good), str(bad), str(missing), "", str(good)]
    assert [row["status"] for row in rows] == ["unsigned", "invalid", "invalid", "invalid", "unsigned"]
    expected = message_verify.verify_message(good.read_bytes(), {})
    assert set(rows[0]) == set(expected) | {"path"}
    assert rows[0]["payload_sha256"] == expected["payload_sha256"]
    assert rows[2]["payload_sha256"] is None


def test_context_loaded_once_and_flushed_per_line(tmp_path, monkeypatch):
    path = tmp_path / "message.yaml"
    path.write_text("from: author\n")
    output = mock.Mock(wraps=io.StringIO())
    monkeypatch.setattr(sys, "argv", ["verify", "--batch", "--pins", str(tmp_path / "pins.yaml")])
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{path}\n{path}\n"))
    monkeypatch.setattr(sys, "stdout", output)
    with mock.patch.object(message_verify, "_load_pins_policy_checked", return_value=({}, None, None)) as load:
        assert message_verify.main() == 0
    load.assert_called_once()
    assert output.flush.call_count == 2


@pytest.mark.parametrize("args", [["some.yaml"], ["--attach-audit", "audit.yaml"], ["--quarantine"]])
def test_batch_rejects_mutation_or_positional_arguments(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["verify", "--batch", *args])
    with pytest.raises(SystemExit) as error:
        message_verify.main()
    assert error.value.code == 2


def test_unreadable_stdin_is_batch_failure(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["verify", "--batch"])
    stream = mock.Mock()
    stream.__iter__ = mock.Mock(side_effect=OSError("input unavailable"))
    monkeypatch.setattr(sys, "stdin", stream)
    assert message_verify.main() == 2
    assert "batch stream failed" in capsys.readouterr().err


def test_public_cli_empty_batch_and_single_file_compatibility(tmp_path):
    root = Path(__file__).resolve().parents[1]
    batch = subprocess.run([sys.executable, "-m", "oacp.cli", "verify", "--batch"],
                           input="", text=True, capture_output=True, cwd=root)
    assert batch.returncode == 0 and batch.stdout == ""
    path = tmp_path / "message.yaml"
    path.write_text("from: author\n")
    single = subprocess.run([sys.executable, "-m", "oacp.cli", "verify", str(path), "--json"],
                            text=True, capture_output=True, cwd=root)
    assert single.returncode == 0 and json.loads(single.stdout)["status"] == "unsigned"


def test_nonregular_paths_do_not_stall_stream(tmp_path):
    import os

    regular = tmp_path / "message.yaml"
    regular.write_text("from: author\n")
    paths = [tmp_path]
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo.yaml"
        os.mkfifo(fifo)
        paths.append(fifo)
    result = subprocess.run(
        [sys.executable, str(Path(message_verify.__file__)), "--batch"],
        input="\n".join(map(str, [regular, *paths, regular])),
        text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[0]["status"] == rows[-1]["status"] == "unsigned"
    assert all(row["read_error"] and row["payload_sha256"] is None for row in rows[1:-1])


@pytest.mark.skipif(not message_verify.CRYPTO_AVAILABLE, reason="crypto unavailable")
def test_signed_batch_has_no_per_message_state_leak(tmp_path, monkeypatch, capsys):
    import yaml
    from message_signing import generate_keypair, load_signers, sign_and_append

    # Realistic signing format; all agents and message content are synthetic.
    home = tmp_path / "keys"
    known = generate_keypair("author", home)
    generate_keypair("other", home)
    stub = json.loads(Path(known["public_stub_path"]).read_text())
    pins = tmp_path / "pins.yaml"
    pins.write_text(yaml.safe_dump({"version": 1, "signers": [
        {"agent": "author", "kid": stub["kid"], "jwk": stub["jwk"], "status": "active"}
    ]}))
    signed = sign_and_append(b"from: author\nto: reviewer\nbody: example\n", load_signers("author", home))
    unknown = sign_and_append(b"from: other\nto: reviewer\n", load_signers("other", home))
    raw_rows = [signed, unknown, signed.replace(b"example", b"changed"), signed]
    paths = []
    for i, raw in enumerate(raw_rows):
        path = tmp_path / f"message-{i}.yaml"
        path.write_bytes(raw)
        paths.append(path)
    code, rows, err = run_batch(monkeypatch, capsys, "\n".join(map(str, paths)), "--pins", str(pins))
    assert code == 0 and not err
    assert [row["status"] for row in rows] == ["verified", "untrusted", "invalid", "verified"]
    for path, row in zip(paths, rows):
        single = subprocess.run([sys.executable, message_verify.__file__, str(path), "--pins", str(pins), "--json"],
                                capture_output=True, text=True)
        # Each invocation stamps its own observation time; all verification
        # content must match regardless of crossing a wall-clock second.
        expected = json.loads(single.stdout)
        assert row["verified_at_utc"] and expected["verified_at_utc"]
        assert {k: v for k, v in row.items() if k not in {"path", "verified_at_utc"}} == {
            k: v for k, v in expected.items() if k != "verified_at_utc"
        }


def test_closed_stdout_exits_cleanly(tmp_path):
    path = tmp_path / "message.yaml"
    path.write_text("from: author\n")
    process = subprocess.Popen([sys.executable, message_verify.__file__, "--batch"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    process.stdout.close()
    process.stdout = None
    _, err = process.communicate(str(path) + "\n", timeout=5)
    assert process.returncode == 2
    assert "Exception ignored" not in err
