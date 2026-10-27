#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""trust_cli.py — Import, inspect, and revoke OACP trust-root entries.

Usage:
    trust_cli.py import <stub.pub.json> --project <name> --agent <receiver>
                 [--catalog-only] [--oacp-dir <path>] [--json]
    trust_cli.py list --project <name> [--agent <receiver>]
                 [--oacp-dir <path>] [--json]
    trust_cli.py revoke <kid> --project <name>
                 (--agent <receiver> | --all-receivers)
                 [--oacp-dir <path>] [--json]
    trust_cli.py sign-policy --project <name> --agent <receiver>
                 [--kid <kid>] [--oacp-dir <path>] [--json]
    trust_cli.py pin-script <path> --agent <receiver>
                 (--project <name> | --all-projects) [--env <NAME> ...]
                 [--kid <kid>] [--oacp-dir <path>] [--json]

`import` records a `<kid>.pub.json` public stub (written by `oacp trust keygen`)
in the project's zero-authority catalog and pins it `active` in the
receiver's `allowed_signers.yaml` — the pins are what grant trust at verify
time. `--catalog-only` records the identity without granting anything.
`list` shows the catalog and per-receiver pins. Drift between the two is
`oacp doctor --project <name>` territory. `revoke` flips a pinned kid to
`status: revoked` through the canonical writer (one command per receiver,
or `--all-receivers` for fleet compromise response); a revoked pin rejects
unseen messages with no crypto attempted and is never reactivated by
import.

`sign-policy` signs the receiver's two policy files — `config.yaml` and
`trust/allowed_signers.yaml` — with the receiver agent's own local signing
key (`oacp trust keygen`), appending a policy-class auth trailer to each. Run it
after creating the workspace and again after every policy edit; loaders
verify the signatures at load time and fail closed on tamper. Bootstrap
order for a fresh project: `oacp init` → `oacp trust keygen --agent <receiver>`
→ `oacp trust sign-policy` (unsigned policy files load with the unsigned
state recorded until then).

`pin-script` records a script under the receiver's `envelope.trusted_scripts`
— the path as spelled (absolute or `~/`-rooted), the sha256 of the file's
bytes, and the `--env` names a caller may prefix — appending the entry or
refreshing the one that names the same file, validates the block with the
envelope compiler's own loader, writes `config.yaml` with every other key
byte-for-byte unchanged, and re-signs it like `sign-policy` (enrolling the
config target). A signed or enrolled config is never written when the
receiver's key is unavailable; an unsigned bootstrap config is written
unsigned. The pin is the human's review of the script's bytes: run it once
per script version, and again after every edit to a pinned file.

Both writers hold each policy file's lock from their read to their replace
and commit only a signed candidate that already verifies, in one atomic
step: overlapping runs serialize, a failed run leaves the previous bytes,
and an edit by a writer that bypassed the lock is refused when the compare
before the replace sees it (best-effort detection; the lock is the contract).

Exit codes: 0 success · 1 trust operation error · 2 usage error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import ExitStack
from typing import Any, Dict, List, Optional, Sequence, Tuple
from pathlib import Path

_scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_scripts_dir))

from _oacp_constants import AGENT_RE, is_agent_dir  # noqa: E402
from message_signing import AuthFormatError  # noqa: E402
from message_verify import (  # noqa: E402
    ALLOWED_SIGNERS_RELPATH,
    TrustRootError,
)
from trust_root import (  # noqa: E402
    CATALOG_RELPATH,
    TrustImportError,
    TrustRevokeError,
    _load_pins_authorized,
    import_public_stub,
    load_catalog,
    revoke_pin,
)


def _resolve_project_dir(project: str, oacp_dir: str) -> Path:
    """Resolve a project workspace, refusing path-escaping names.

    The project value is an untrusted CLI flag: it must be a plain name
    (no separators, no leading dot — the workspace-init rules) and the
    resolved directory must sit directly under ``$OACP_HOME/projects/``.
    """
    from _oacp_env import resolve_oacp_home

    if not project or project.startswith(".") or "/" in project or "\\" in project:
        raise ValueError(
            "project name must not contain path separators or start with '.'"
        )
    home = resolve_oacp_home(oacp_dir) if oacp_dir else resolve_oacp_home()
    projects_root = home / "projects"
    project_dir = projects_root / project
    if not project_dir.is_dir():
        raise ValueError(f"project workspace not found: {project_dir}")
    if project_dir.resolve().parent != projects_root.resolve():
        raise ValueError(f"project {project!r} escapes the projects directory")
    return project_dir


def _sign_targets(
    targets: Sequence[Tuple[Path, str]],
    project: str,
    agent: str,
    home: Path,
    signers: Sequence[Any],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Sign each policy target under its lock, committing only bytes that
    verify, then enroll them all.

    Per target: read the file under `policy_file_lock`, build the signed
    candidate off-path, verify THAT candidate against the keystore pins and
    the target context, and only then replace the file in one atomic step —
    a candidate that does not round-trip never reaches disk, so an enrolled
    file is never left invalid. Enrollment is the point of no return for the
    unsigned-bootstrap path, so it is recorded only after EVERY target
    committed: a signing failure never strands an unsigned file behind
    downgrade resistance. Returns the per-file reports and an error message
    (None on success).
    """
    from policy_signing import (
        POLICY_STATUS_VERIFIED,
        PolicyWriteError,
        policy_context,
        policy_file_lock,
        record_policy_enrollment,
        replace_policy_file,
        signed_policy_bytes,
        verify_policy_data,
    )

    reports: List[Dict[str, Any]] = []
    for path, kind in targets:
        context = policy_context(project, agent, kind)
        with policy_file_lock(path):
            raw = path.read_bytes()
            candidate, resigned = signed_policy_bytes(raw, signers, context=context)
            check = verify_policy_data(candidate, home, receiver=agent, context=context)
            reports.append({
                "path": str(path),
                "resigned": resigned,
                "context": context,
                "signer_kids": [signer.kid for signer in signers],
                "verify_status": check["status"],
            })
            if check["status"] != POLICY_STATUS_VERIFIED:
                return reports, (
                    f"{path} did not round-trip verify after signing "
                    f"({check['status']}: {check['reason']}); the file was not written"
                )
            try:
                replace_policy_file(path, candidate, expected=raw)
            except (OSError, PolicyWriteError) as exc:
                return reports, f"{path}: {exc}"
    for path, kind in targets:
        record_policy_enrollment(home, policy_context(project, agent, kind), signers[0].kid)
    return reports, None


_TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:")
_PIN_PATH_EXPANSION_RE = re.compile(r"[$`*?\[\]{}]")


class PinScriptError(ValueError):
    """A pin-script operation that must not write."""


def _spelled_script(spelled: str) -> Tuple[str, str]:
    """Validate the pin's path spelling and hash the file it names."""
    if not (spelled.startswith("/") or spelled.startswith("~/")):
        raise PinScriptError("script path must be absolute or ~/-rooted (as the pin records it)")
    if _PIN_PATH_EXPANSION_RE.search(spelled):
        raise PinScriptError("script path carries shell expansion syntax")
    actual = Path(os.path.expanduser(spelled))
    if not actual.is_file():
        raise PinScriptError(f"script not found: {actual}")
    return spelled, hashlib.sha256(actual.read_bytes()).hexdigest()


def _render_envelope_block(envelope: Dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(
        {"envelope": envelope}, sort_keys=False, default_flow_style=False, width=1000
    )


def _splice_envelope_block(text: str, block: str) -> str:
    """Replace the top-level ``envelope:`` block in *text* with *block*, or
    insert it before the ``auth:`` trailer (else append). Every other line is
    kept byte for byte; the caller re-parses the result and refuses it when
    anything but ``envelope`` changed."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith("envelope:")), None)
    if start is None:
        auth = next((i for i, line in enumerate(lines) if line.startswith("auth:")), None)
        head = "".join(lines[:auth]) if auth is not None else text
        if head and not head.endswith("\n"):
            head += "\n"
        tail = "".join(lines[auth:]) if auth is not None else ""
        return head + block + tail
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i][0].isspace() and not lines[i].startswith("#"):
            end = i
            break
    return "".join(lines[:start]) + block + "".join(lines[end:])


def _pinned_config_text(
    text: str, spelled: str, digest: str, env: Sequence[str]
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """The config text with the pin added or refreshed, and what changed."""
    import yaml

    from envelope_compiler import EnvelopeCompileError, trusted_scripts
    from message_signing import split_signed_message

    payload, _trailer = split_signed_message(text.encode("utf-8"))
    data = yaml.safe_load(payload.decode("utf-8")) or {}
    if not isinstance(data, dict):
        raise PinScriptError("config.yaml must be a mapping")
    try:
        pins = trusted_scripts(data)
    except EnvelopeCompileError as exc:
        raise PinScriptError(str(exc)) from exc
    entry: Dict[str, Any] = {"path": spelled, "sha256": digest}
    if env:
        entry["env"] = list(env)
    target = os.path.realpath(os.path.expanduser(spelled))
    outcome = "added"
    updated: List[Dict[str, Any]] = []
    for pin in pins:
        if os.path.realpath(os.path.expanduser(pin["path"])) == target:
            if outcome == "added":
                before = {"path": pin["path"], "sha256": pin["sha256"]}
                if pin["env"]:
                    before["env"] = list(pin["env"])
                outcome = "unchanged" if before == entry else "refreshed"
                updated.append(entry)
            continue
        kept = {"path": pin["path"], "sha256": pin["sha256"]}
        if pin["env"]:
            kept["env"] = list(pin["env"])
        updated.append(kept)
    if outcome == "added":
        updated.append(entry)
    envelope = dict(data.get("envelope") or {})
    envelope["trusted_scripts"] = updated
    candidate = dict(data)
    candidate["envelope"] = envelope
    try:
        validated = trusted_scripts(candidate)
    except EnvelopeCompileError as exc:
        raise PinScriptError(str(exc)) from exc
    new_text = _splice_envelope_block(text, _render_envelope_block(envelope))
    reparsed_payload, _t = split_signed_message(new_text.encode("utf-8"))
    reparsed = yaml.safe_load(reparsed_payload.decode("utf-8")) or {}
    expected = dict(data)
    expected["envelope"] = envelope
    if reparsed != expected:
        raise PinScriptError(
            "config.yaml layout could not be edited in place without touching other "
            "keys; add the pin by hand and run oacp trust sign-policy"
        )
    return new_text, outcome, validated


def _cmd_pin_script(args: argparse.Namespace) -> int:
    if not AGENT_RE.fullmatch(args.agent):
        print(f"ERROR: agent name must match {AGENT_RE.pattern}", file=sys.stderr)
        return 2
    if bool(args.project) == bool(args.all_projects):
        print("ERROR: exactly one of --project or --all-projects is required", file=sys.stderr)
        return 2
    from _oacp_env import resolve_oacp_home
    from message_signing import SigningUnavailableError, load_signers, split_signed_message
    from policy_signing import (
        POLICY_KIND_RECEIVER_CONFIG,
        POLICY_STATUS_UNSIGNED,
        POLICY_STATUS_VERIFIED,
        PolicyAuthError,
        PolicyWriteError,
        policy_context,
        policy_enrolled,
        policy_file_lock,
        record_policy_enrollment,
        replace_policy_file,
        signed_policy_bytes,
        verify_policy_data,
    )

    home = resolve_oacp_home(args.oacp_dir) if args.oacp_dir else resolve_oacp_home()
    try:
        spelled, digest = _spelled_script(args.script)
    except PinScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    projects_dir = Path(home) / "projects"
    if args.all_projects:
        projects = sorted(
            p.name
            for p in projects_dir.iterdir()
            if (p / "agents" / args.agent / "config.yaml").is_file()
        ) if projects_dir.is_dir() else []
        if not projects:
            print(f"ERROR: no project under {projects_dir} has agents/{args.agent}/config.yaml", file=sys.stderr)
            return 1
    else:
        projects = [args.project]

    try:
        signers: Optional[Sequence[Any]] = load_signers(args.agent, home, kid=args.kid)
    except SigningUnavailableError:
        signers = None

    configs: List[Tuple[str, Path]] = []
    for project in projects:
        try:
            project_dir = _resolve_project_dir(project, args.oacp_dir)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        config_path = project_dir / "agents" / args.agent / "config.yaml"
        if not config_path.is_file():
            print(f"ERROR: receiver config not found: {config_path}", file=sys.stderr)
            return 1
        configs.append((project, config_path))

    def _state_line(landed: List[str]) -> str:
        if len(configs) == 1:
            return ""
        rest = [project for project, _path in configs if project not in landed]
        return (
            f"  written: {', '.join(landed) or 'none'}; "
            f"not written: {', '.join(rest) or 'none'}"
        )

    # One transaction: every config is locked, in project order, from its
    # read to its replace, so overlapping registrations serialize instead
    # of the later one discarding the earlier; every candidate is built and
    # verified before the first write, so a refusal in one project leaves
    # every project untouched. The commit phase is per file (there is no
    # multi-file atomic replace): a failure there names what landed.
    with ExitStack() as locks:
        for _project, config_path in configs:
            locks.enter_context(policy_file_lock(config_path))

        plans: List[Dict[str, Any]] = []
        for project, config_path in configs:
            raw = config_path.read_bytes()
            context = policy_context(project, args.agent, POLICY_KIND_RECEIVER_CONFIG)
            _payload, trailer = split_signed_message(raw)
            signed_before = trailer is not None or policy_enrolled(home, context)
            if signed_before and signers is None:
                print(
                    f"ERROR: {config_path} is signed or enrolled and no signing key for "
                    f"{args.agent!r} is available; the pin was not written "
                    "(oacp trust keygen, or run as the receiver that owns the key)",
                    file=sys.stderr,
                )
                return 1
            try:
                new_text, outcome, pins = _pinned_config_text(
                    raw.decode("utf-8"), spelled, digest, args.env
                )
            except PinScriptError as exc:
                print(f"ERROR: {project}: {exc}", file=sys.stderr)
                return 1
            if signers is not None:
                candidate, _resigned = signed_policy_bytes(
                    new_text.encode("utf-8"), signers, context=context
                )
                expected_status = POLICY_STATUS_VERIFIED
            else:
                candidate = new_text.encode("utf-8")
                expected_status = POLICY_STATUS_UNSIGNED
            check = verify_policy_data(candidate, home, receiver=args.agent, context=context)
            if check["status"] != expected_status:
                print(
                    f"ERROR: {project}: the pinned config would not load as {expected_status} "
                    f"({check['status']}: {check['reason']}); nothing was written",
                    file=sys.stderr,
                )
                return 1
            plans.append({
                "project": project,
                "config_path": config_path,
                "context": context,
                "raw": raw,
                "candidate": candidate,
                "outcome": outcome,
                "pins": len(pins),
                "sign": signers is not None,
            })

        reports: List[Dict[str, Any]] = []
        landed: List[str] = []
        for plan in plans:
            try:
                replace_policy_file(plan["config_path"], plan["candidate"], expected=plan["raw"])
            except (OSError, PolicyWriteError) as exc:
                print(f"ERROR: {plan['project']}: {exc}", file=sys.stderr)
                if _state_line(landed):
                    print(_state_line(landed), file=sys.stderr)
                return 1
            report: Dict[str, Any] = {
                "project": plan["project"],
                "path": str(plan["config_path"]),
                "script": spelled,
                "sha256": digest,
                "env": list(args.env),
                "outcome": plan["outcome"],
                "pins": plan["pins"],
                "signed": plan["sign"],
                "enrolled": False,
            }
            if plan["sign"]:
                try:
                    record_policy_enrollment(home, plan["context"], signers[0].kid)
                except (OSError, PolicyAuthError) as exc:
                    print(
                        f"ERROR: {plan['project']}: the config was written and signed but "
                        f"its enrollment was not recorded ({exc}); run oacp trust sign-policy "
                        f"--project {plan['project']} --agent {args.agent} to enroll it",
                        file=sys.stderr,
                    )
                    if _state_line(landed + [plan["project"]]):
                        print(_state_line(landed + [plan["project"]]), file=sys.stderr)
                    return 1
                report["enrolled"] = True
                report["signer_kids"] = [signer.kid for signer in signers]
            reports.append(report)
            landed.append(plan["project"])

    if args.json_output:
        print(json.dumps(reports, indent=2))
    else:
        for report in reports:
            state = "signed" if report["signed"] else "unsigned (bootstrap; oacp trust keygen + sign-policy)"
            print(
                f"OK: {report['project']}: {report['outcome']} pin for {spelled} "
                f"({report['pins']} pin(s), {state})"
            )
            print(f"  config: {report['path']}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import and inspect OACP trust-root entries."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser(
        "import", help="Import a public key stub into catalog + receiver pins"
    )
    imp.add_argument("stub", help="Path to a <kid>.pub.json stub from `oacp trust keygen`")
    imp.add_argument("--project", required=True, help="Project workspace name")
    imp.add_argument(
        "--agent",
        default=None,
        help="Receiver whose pins gain the entry (required unless --catalog-only)",
    )
    imp.add_argument(
        "--catalog-only",
        action="store_true",
        help="Record the identity in the catalog only — grant no authority",
    )
    imp.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    imp.add_argument("--json", action="store_true", dest="json_output")

    lst = sub.add_parser("list", help="List catalog entries and receiver pins")
    lst.add_argument("--project", required=True, help="Project workspace name")
    lst.add_argument("--agent", default=None, help="Only this receiver's pins")
    lst.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    lst.add_argument("--json", action="store_true", dest="json_output")

    rvk = sub.add_parser(
        "revoke", help="Revoke a pinned kid in receiver allowed_signers.yaml"
    )
    rvk.add_argument("kid", help="Canonical RFC 7638 thumbprint of the pin to revoke")
    rvk.add_argument("--project", required=True, help="Project workspace name")
    rvk.add_argument(
        "--agent",
        default=None,
        help="Receiver whose pin is revoked (or use --all-receivers)",
    )
    rvk.add_argument(
        "--all-receivers",
        action="store_true",
        help="Revoke the kid for every receiver in the project that pins it",
    )
    rvk.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    rvk.add_argument("--json", action="store_true", dest="json_output")

    sgn = sub.add_parser(
        "sign-policy",
        help="Sign a receiver's config.yaml + allowed_signers.yaml with its own key",
    )
    sgn.add_argument("--project", required=True, help="Project workspace name")
    sgn.add_argument(
        "--agent", required=True, help="Receiver whose policy files are signed"
    )
    sgn.add_argument(
        "--kid", default=None, help="Sign with this specific key only"
    )
    sgn.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    sgn.add_argument("--json", action="store_true", dest="json_output")

    pin = sub.add_parser(
        "pin-script",
        help="Pin a script (path + sha256) under a receiver's envelope.trusted_scripts "
        "and re-sign its config.yaml",
    )
    pin.add_argument(
        "script",
        help="Script path, absolute or ~/-rooted; recorded as spelled, hashed as found",
    )
    pin.add_argument("--project", default=None, help="Project workspace name")
    pin.add_argument(
        "--all-projects",
        action="store_true",
        help="Every project under the OACP home where the receiver has a config.yaml",
    )
    pin.add_argument("--agent", required=True, help="Receiver whose config carries the pin")
    pin.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME",
        help="Assignment name a caller may prefix (repeatable; replaces the pin's list)",
    )
    pin.add_argument("--kid", default=None, help="Sign with this specific key only")
    pin.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    pin.add_argument("--json", action="store_true", dest="json_output")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "pin-script":
        return _cmd_pin_script(args)

    try:
        project_dir = _resolve_project_dir(args.project, args.oacp_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.command == "import":
        if not args.catalog_only and not args.agent:
            print(
                "ERROR: --agent is required unless --catalog-only", file=sys.stderr
            )
            return 2
        try:
            report = import_public_stub(
                Path(args.stub),
                project_dir,
                receiver=args.agent,
                catalog_only=args.catalog_only,
            )
        except (TrustImportError, TrustRootError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json_output:
            print(json.dumps(report, indent=2))
        else:
            print(f"OK: imported {report['kid']} ({report['agent']})")
            print(f"  catalog: {report['catalog']}  {report['catalog_path']}")
            if report["pins"] == "skipped":
                print("  pins:    skipped (--catalog-only; no authority granted)")
            else:
                print(f"  pins:    {report['pins']}  {report['pins_path']}")
        return 0

    if args.command == "revoke":
        if bool(args.agent) == bool(args.all_receivers):
            print(
                "ERROR: exactly one of --agent or --all-receivers is required",
                file=sys.stderr,
            )
            return 2
        try:
            report = revoke_pin(
                project_dir,
                args.kid,
                receiver=args.agent,
                all_receivers=args.all_receivers,
            )
        except (
            AuthFormatError,
            TrustImportError,
            TrustRevokeError,
            TrustRootError,
        ) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json_output:
            print(json.dumps(report, indent=2))
        else:
            print(f"OK: revoked {report['kid']}")
            for agent, state in sorted(report["receivers"].items()):
                print(f"  {agent}: {state}")
        return 0

    if args.command == "sign-policy":
        if not AGENT_RE.fullmatch(args.agent):
            print(
                f"ERROR: agent name must match {AGENT_RE.pattern}",
                file=sys.stderr,
            )
            return 2
        from _oacp_env import resolve_oacp_home

        from message_signing import SigningUnavailableError, load_signers
        from policy_signing import (
            POLICY_KIND_ALLOWED_SIGNERS,
            POLICY_KIND_RECEIVER_CONFIG,
        )

        home = (
            resolve_oacp_home(args.oacp_dir)
            if args.oacp_dir
            else resolve_oacp_home()
        )
        targets = [
            (
                project_dir / "agents" / args.agent / "config.yaml",
                POLICY_KIND_RECEIVER_CONFIG,
            ),
            (
                project_dir / "agents" / args.agent / ALLOWED_SIGNERS_RELPATH,
                POLICY_KIND_ALLOWED_SIGNERS,
            ),
        ]
        missing = [str(path) for path, _kind in targets if not path.is_file()]
        if missing:
            print(
                "ERROR: policy file(s) not found: " + ", ".join(missing),
                file=sys.stderr,
            )
            return 1
        try:
            signers = load_signers(args.agent, home, kid=args.kid)
        except SigningUnavailableError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        reports, error = _sign_targets(targets, args.project, args.agent, home, signers)
        if error is not None:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        if args.json_output:
            print(json.dumps(reports, indent=2))
        else:
            for report in reports:
                verb = "re-signed" if report["resigned"] else "signed"
                print(f"OK: {verb} {report['path']}")
                for kid in report["signer_kids"]:
                    print(f"  kid: {kid}")
        return 0

    if args.command == "list":
        if args.agent and not AGENT_RE.fullmatch(args.agent):
            print(
                f"ERROR: agent name must match {AGENT_RE.pattern}",
                file=sys.stderr,
            )
            return 2
        catalog_path = project_dir / CATALOG_RELPATH
        payload = {"catalog": [], "pins": {}}
        try:
            catalog = load_catalog(catalog_path)
        except TrustRootError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        for kid, entry in sorted(
            catalog.items(), key=lambda item: (item[1]["agent"], item[0])
        ):
            payload["catalog"].append({"kid": kid, **entry})

        agents_dir = project_dir / "agents"
        agent_names = (
            [args.agent]
            if args.agent
            else sorted(
                d.name for d in agents_dir.iterdir() if is_agent_dir(d)
            )
            if agents_dir.is_dir()
            else []
        )
        for agent in agent_names:
            pins_path = agents_dir / agent / ALLOWED_SIGNERS_RELPATH
            if not pins_path.is_file():
                continue
            try:
                # Authorized read: listed trust contents must come through
                # the policy-checked loader, never a bare parse.
                pins = _load_pins_authorized(
                    pins_path, project_dir=project_dir, receiver=agent
                )
            except TrustRootError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            payload["pins"][agent] = [
                {"kid": kid, **entry} for kid, entry in sorted(
                    pins.items(), key=lambda item: (item[1]["agent"], item[0])
                )
            ]

        if args.json_output:
            print(json.dumps(payload, indent=2))
            return 0
        if not payload["catalog"]:
            print("Catalog: empty")
        else:
            print(f"Catalog ({len(payload['catalog'])} identities):")
            for entry in payload["catalog"]:
                print(f"  {entry['agent']}  {entry['kid']}")
        for agent, pins in payload["pins"].items():
            print(f"Pins — {agent} ({len(pins)}):")
            for entry in pins:
                print(f"  {entry['agent']}  {entry['kid']}  {entry['status']}")
        if not payload["pins"]:
            print("Pins: none (no receiver has an allowed_signers.yaml)")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
