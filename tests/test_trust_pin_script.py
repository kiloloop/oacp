# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""`oacp trust pin-script`: append or refresh an envelope.trusted_scripts pin,
keep every other config key byte for byte, and re-sign like sign-policy."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import policy_signing  # noqa: E402
import trust_cli  # noqa: E402
from message_signing import CRYPTO_AVAILABLE, generate_keypair, split_signed_message  # noqa: E402
from policy_signing import (  # noqa: E402
    POLICY_KIND_RECEIVER_CONFIG,
    POLICY_STATUS_VERIFIED,
    policy_context,
    verify_policy_data,
    verify_policy_file,
)

AGENT = "dave"
CONFIG_TEXT = (
    "# receiver policy\n"
    "autonomy:\n"
    "  default_mode: always_pause\n"
    "signing:\n"
    "  sign_messages: true\n"
    "  verify_mode: warn\n"
)


def _home(tmp_path: Path, *projects: str) -> Path:
    home = tmp_path / "oacp_home"
    (home / "keys").mkdir(parents=True)
    for project in projects:
        receiver_dir = home / "projects" / project / "agents" / AGENT
        (receiver_dir / "inbox").mkdir(parents=True)
        (receiver_dir / "config.yaml").write_text(CONFIG_TEXT, encoding="utf-8")
    return home


def _script(tmp_path: Path, name: str = "finish.sh", body: str = "git status\n") -> Path:
    script = tmp_path / "tools" / name
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    return script


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(*argv: str) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = trust_cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _config(home: Path, project: str) -> Path:
    return home / "projects" / project / "agents" / AGENT / "config.yaml"


def _parsed(path: Path) -> dict:
    payload, _trailer = split_signed_message(path.read_bytes())
    return yaml.safe_load(payload.decode("utf-8"))


def _enroll(home: Path, project: str = "proj") -> dict:
    """Key the receiver, pin its own kid, and sign + enroll its policy files."""
    receiver_dir = home / "projects" / project / "agents" / AGENT
    report = generate_keypair(AGENT, home)
    stub = json.loads(Path(report["public_stub_path"]).read_text(encoding="utf-8"))
    (receiver_dir / "trust").mkdir()
    (receiver_dir / "trust" / "allowed_signers.yaml").write_text(
        "version: 1\nsigners:\n"
        f"  - agent: {AGENT}\n    kid: {stub['kid']}\n"
        "    jwk:\n      kty: OKP\n      crv: Ed25519\n"
        f"      x: {stub['jwk']['x']}\n    status: active\n",
        encoding="utf-8",
    )
    code, _out, err = _run("sign-policy", "--project", project, "--agent", AGENT, "--oacp-dir", str(home))
    assert code == 0, err
    return report


def _replace_hook(target: Path, action):
    """An os.replace stand-in that runs *action*(staged) when the destination is *target*."""
    real = policy_signing.os.replace

    def hooked(src, dst):
        if Path(dst) == target:
            action(Path(src))
        return real(src, dst)

    return hooked


def _fail(_staged: Path) -> None:
    raise OSError("synthetic commit failure")


CRYPTO = pytest.mark.skipif(not CRYPTO_AVAILABLE, reason="cryptography not installed")
CONFIG_CTX = policy_context("proj", AGENT, POLICY_KIND_RECEIVER_CONFIG)


def test_pin_script_adds_refreshes_and_keeps_other_keys(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    script = _script(tmp_path)
    config = _config(home, "proj")

    code, out, err = _run(
        "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
    )
    assert code == 0, err
    assert "added pin" in out and "unsigned (bootstrap" in out
    data = _parsed(config)
    assert data["envelope"]["trusted_scripts"] == [
        {"path": str(script), "sha256": _digest(script)}
    ]
    assert data["autonomy"] == {"default_mode": "always_pause"}
    assert data["signing"] == {"sign_messages": True, "verify_mode": "warn"}
    # Every byte outside the envelope block survives, comment included.
    assert config.read_text(encoding="utf-8").startswith(CONFIG_TEXT)

    # Same bytes again: nothing to refresh.
    code, out, _err = _run(
        "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
    )
    assert code == 0 and "unchanged pin" in out

    # The file changed: the one entry is refreshed in place, env replaced.
    script.write_text("#!/usr/bin/env bash\ngit status --short\n", encoding="utf-8")
    code, out, _err = _run(
        "pin-script", str(script), "--project", "proj", "--agent", AGENT,
        "--env", "OACP_DONE_TYPE", "--oacp-dir", str(home),
    )
    assert code == 0 and "refreshed pin" in out
    pins = _parsed(config)["envelope"]["trusted_scripts"]
    assert pins == [{"path": str(script), "sha256": _digest(script), "env": ["OACP_DONE_TYPE"]}]

    # A second script appends after the first.
    other = _script(tmp_path, "archive.sh", "mv a b\n")
    code, _out, _err = _run(
        "pin-script", str(other), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
    )
    assert code == 0
    pins = _parsed(config)["envelope"]["trusted_scripts"]
    assert [pin["path"] for pin in pins] == [str(script), str(other)]
    assert pins[0]["env"] == ["OACP_DONE_TYPE"] and "env" not in pins[1]


def test_pin_script_refreshes_an_existing_block_in_place(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    script = _script(tmp_path)
    config = _config(home, "proj")
    config.write_text(
        "# receiver policy\n"
        "autonomy:\n"
        "  default_mode: always_pause\n"
        "envelope:\n"
        "  trusted_scripts:\n"
        "    - path: /opt/tools/tidy.sh\n"
        f"      sha256: {'b' * 64}\n"
        f"    - path: {script}\n"
        f"      sha256: \"{'0' * 64}\"\n"
        "      env: [OACP_DONE_TYPE]\n"
        "signing:\n"
        "  sign_messages: true\n"
        "  verify_mode: warn\n",
        encoding="utf-8",
    )
    code, out, err = _run(
        "pin-script", str(script), "--project", "proj", "--agent", AGENT,
        "--env", "OACP_DONE_TYPE", "--oacp-dir", str(home),
    )
    assert code == 0, err
    assert "refreshed pin" in out and "2 pin(s)" in out
    text = config.read_text(encoding="utf-8")
    assert text.startswith("# receiver policy\nautonomy:\n  default_mode: always_pause\nenvelope:\n")
    assert text.endswith("signing:\n  sign_messages: true\n  verify_mode: warn\n")
    pins = _parsed(config)["envelope"]["trusted_scripts"]
    assert pins == [
        {"path": "/opt/tools/tidy.sh", "sha256": "b" * 64},
        {"path": str(script), "sha256": _digest(script), "env": ["OACP_DONE_TYPE"]},
    ]


@pytest.mark.parametrize(
    "argv, code, needle",
    [
        (["tools/relative.sh", "--project", "proj"], 1, "absolute or ~/-rooted"),
        (["/nonexistent/finish.sh", "--project", "proj"], 1, "script not found"),
        (["{script}", "--project", "proj", "--env", "PATH"], 1, "retargets the script"),
        (["{script}", "--project", "proj", "--env", "not a name"], 1, "variable names"),
        (["{script}"], 2, "exactly one of --project or --all-projects"),
        (["{script}", "--project", "proj", "--all-projects"], 2, "exactly one of"),
        (["{script}", "--project", "missing"], 1, "not found"),
    ],
)
def test_pin_script_refuses_without_writing(
    tmp_path: Path, argv: list, code: int, needle: str
) -> None:
    home = _home(tmp_path, "proj")
    script = _script(tmp_path)
    config = _config(home, "proj")
    before = config.read_bytes()
    argv = [arg.replace("{script}", str(script)) for arg in argv]
    got, _out, err = _run("pin-script", *argv, "--agent", AGENT, "--oacp-dir", str(home))
    assert got == code, err
    assert needle in err
    assert config.read_bytes() == before


def test_pin_script_all_projects_writes_every_receiver_config(tmp_path: Path) -> None:
    home = _home(tmp_path, "alpha", "beta", "gamma")
    _config(home, "gamma").unlink()  # gamma has the agent but no config: skipped
    script = _script(tmp_path)
    code, out, err = _run(
        "pin-script", str(script), "--all-projects", "--agent", AGENT, "--json", "--oacp-dir", str(home)
    )
    assert code == 0, err
    reports = json.loads(out)
    assert [r["project"] for r in reports] == ["alpha", "beta"]
    assert all(r["outcome"] == "added" and r["signed"] is False for r in reports)
    for project in ("alpha", "beta"):
        assert _parsed(_config(home, project))["envelope"]["trusted_scripts"] == [
            {"path": str(script), "sha256": _digest(script)}
        ]
    assert not _config(home, "gamma").exists()


def test_pin_script_refuses_a_malformed_existing_block(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    script = _script(tmp_path)
    config = _config(home, "proj")
    config.write_text(CONFIG_TEXT + "envelope:\n  trusted_scripts: nope\n", encoding="utf-8")
    before = config.read_bytes()
    code, _out, err = _run(
        "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
    )
    assert code == 1 and "must be a list" in err
    assert config.read_bytes() == before


@pytest.mark.skipif(not CRYPTO_AVAILABLE, reason="cryptography not installed")
def test_pin_script_resigns_an_enrolled_config_and_refuses_without_the_key(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    report = _enroll(home)
    config = _config(home, "proj")
    script = _script(tmp_path)

    code, out, err = _run(
        "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
    )
    assert code == 0, err
    assert "added pin" in out and "signed)" in out
    check = verify_policy_file(config, home, receiver=AGENT, kind=POLICY_KIND_RECEIVER_CONFIG)
    assert check["status"] == POLICY_STATUS_VERIFIED, check
    data = _parsed(config)
    assert data["envelope"]["trusted_scripts"] == [{"path": str(script), "sha256": _digest(script)}]
    assert data["autonomy"] == {"default_mode": "always_pause"}
    _payload, trailer = split_signed_message(config.read_bytes())
    assert trailer is not None

    # The key goes away: an enrolled config is never rewritten unsigned.
    Path(report["key_path"]).unlink()
    for stub_path in (home / "keys").glob(f"*{AGENT}*.pub.json"):
        stub_path.unlink()
    before = config.read_bytes()
    script.write_text("#!/usr/bin/env bash\ngit status --short\n", encoding="utf-8")
    code, _out, err = _run(
        "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
    )
    assert code == 1
    assert "no signing key" in err
    assert config.read_bytes() == before


@CRYPTO
def test_pin_script_commit_failure_leaves_an_enrolled_config_verified(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    _enroll(home)
    config = _config(home, "proj")
    before = config.read_bytes()
    script = _script(tmp_path)

    with patch.object(policy_signing.os, "replace", side_effect=_replace_hook(config, _fail)):
        code, _out, err = _run(
            "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
        )
    assert code == 1 and "synthetic commit failure" in err
    # The previous signed bytes are what is on disk, and they still verify.
    assert config.read_bytes() == before
    check = verify_policy_file(config, home, receiver=AGENT, kind=POLICY_KIND_RECEIVER_CONFIG)
    assert check["status"] == POLICY_STATUS_VERIFIED, check
    assert not list(config.parent.glob(".config.yaml.*"))


@CRYPTO
def test_pin_script_commits_the_verified_candidate_in_one_replace(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    _enroll(home)
    config = _config(home, "proj")
    script = _script(tmp_path)
    staged: list = []

    def observe(src: Path) -> None:
        # The bytes about to land on the config, judged before they land.
        check = verify_policy_data(src.read_bytes(), home, receiver=AGENT, context=CONFIG_CTX)
        staged.append(check["status"])

    with patch.object(policy_signing.os, "replace", side_effect=_replace_hook(config, observe)):
        code, _out, err = _run(
            "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
        )
    assert code == 0, err
    # Exactly one replace reached the config, and it carried a verified candidate:
    # no reader can observe the new payload under the old trailer.
    assert staged == [POLICY_STATUS_VERIFIED]
    assert _parsed(config)["envelope"]["trusted_scripts"] == [
        {"path": str(script), "sha256": _digest(script)}
    ]


def test_pin_script_serializes_overlapping_registrations(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    first = _script(tmp_path, "first.sh")
    second = _script(tmp_path, "second.sh", "mv a b\n")
    planner = trust_cli._pinned_config_text
    outcome: dict = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault(
            "code",
            trust_cli.main([
                "pin-script", str(second), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
            ]),
        )
    )

    def interleave(*args):
        candidate = planner(*args)
        if threading.current_thread() is not worker and worker.ident is None:
            # The second registration starts while the first holds the lock:
            # it must wait for the first's commit, not run inside its transaction.
            worker.start()
            worker.join(0.5)
            assert worker.is_alive(), "the second registration ran inside the first's transaction"
        return candidate

    with patch.object(trust_cli, "_pinned_config_text", side_effect=interleave):
        code, _out, err = _run(
            "pin-script", str(first), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
        )
    worker.join(10)
    assert code == 0, err
    assert outcome["code"] == 0
    pins = _parsed(_config(home, "proj"))["envelope"]["trusted_scripts"]
    assert [pin["path"] for pin in pins] == [str(first), str(second)]


def test_pin_script_refuses_when_the_config_changed_underneath(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    script = _script(tmp_path)
    config = _config(home, "proj")
    planner = trust_cli._pinned_config_text
    edited = CONFIG_TEXT + "notes: edited without the lock\n"

    def edit_by_hand(*args):
        candidate = planner(*args)
        config.write_text(edited, encoding="utf-8")
        return candidate

    with patch.object(trust_cli, "_pinned_config_text", side_effect=edit_by_hand):
        code, _out, err = _run(
            "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
        )
    assert code == 1 and "changed underneath" in err
    assert config.read_text(encoding="utf-8") == edited


def test_pin_script_all_projects_names_what_landed_on_a_commit_failure(tmp_path: Path) -> None:
    home = _home(tmp_path, "alpha", "beta")
    script = _script(tmp_path)
    beta = _config(home, "beta")
    before = beta.read_bytes()

    with patch.object(policy_signing.os, "replace", side_effect=_replace_hook(beta, _fail)):
        code, _out, err = _run(
            "pin-script", str(script), "--all-projects", "--agent", AGENT, "--oacp-dir", str(home)
        )
    assert code == 1
    assert "beta: synthetic commit failure" in err
    assert "written: alpha; not written: beta" in err
    assert _parsed(_config(home, "alpha"))["envelope"]["trusted_scripts"] == [
        {"path": str(script), "sha256": _digest(script)}
    ]
    assert beta.read_bytes() == before


def test_pin_script_all_projects_writes_nothing_when_one_config_refuses(tmp_path: Path) -> None:
    home = _home(tmp_path, "alpha", "beta")
    script = _script(tmp_path)
    _config(home, "beta").write_text(
        CONFIG_TEXT + "envelope:\n  trusted_scripts: nope\n", encoding="utf-8"
    )
    befores = {project: _config(home, project).read_bytes() for project in ("alpha", "beta")}
    code, _out, err = _run(
        "pin-script", str(script), "--all-projects", "--agent", AGENT, "--oacp-dir", str(home)
    )
    assert code == 1 and "beta:" in err and "must be a list" in err
    assert {project: _config(home, project).read_bytes() for project in ("alpha", "beta")} == befores


@CRYPTO
@pytest.mark.parametrize("command", ["pin-script", "sign-policy"])
def test_policy_writers_wait_for_the_config_lock(tmp_path: Path, command: str) -> None:
    home = _home(tmp_path, "proj")
    _enroll(home)
    config = _config(home, "proj")
    script = _script(tmp_path)
    argv = (
        ["pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)]
        if command == "pin-script"
        else ["sign-policy", "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)]
    )
    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("code", trust_cli.main(argv)))

    with policy_signing.policy_file_lock(config):
        before = config.read_bytes()
        worker.start()
        worker.join(0.5)
        assert worker.is_alive(), f"{command} wrote while another writer held the lock"
        assert config.read_bytes() == before
    worker.join(10)
    assert outcome["code"] == 0
    check = verify_policy_file(config, home, receiver=AGENT, kind=POLICY_KIND_RECEIVER_CONFIG)
    assert check["status"] == POLICY_STATUS_VERIFIED, check


def test_pin_script_refuses_when_the_config_changed_during_staging(tmp_path: Path) -> None:
    home = _home(tmp_path, "proj")
    script = _script(tmp_path)
    config = _config(home, "proj")
    edited = CONFIG_TEXT + "notes: edited without the lock, mid-stage\n"
    real_fsync = policy_signing.os.fsync
    fired: list = []

    def edit_during_fsync(fd):
        # The staged candidate is being made durable; an editor that never
        # took the lock writes the config in that window.
        real_fsync(fd)
        if not fired:
            fired.append(True)
            config.write_text(edited, encoding="utf-8")

    with patch.object(policy_signing.os, "fsync", side_effect=edit_during_fsync):
        code, _out, err = _run(
            "pin-script", str(script), "--project", "proj", "--agent", AGENT, "--oacp-dir", str(home)
        )
    assert fired
    assert code == 1 and "changed underneath" in err
    assert config.read_text(encoding="utf-8") == edited
    assert not list(config.parent.glob(".config.yaml.*"))
