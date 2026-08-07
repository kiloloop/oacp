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

`import` records a `<kid>.pub.json` public stub (written by `oacp key gen`)
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
key (`oacp key gen`), appending a policy-class auth trailer to each. Run it
after creating the workspace and again after every policy edit; loaders
verify the signatures at load time and fail closed on tamper. Bootstrap
order for a fresh project: `oacp init` → `oacp key gen --agent <receiver>`
→ `oacp trust sign-policy` (unsigned policy files load with the unsigned
state recorded until then).

Exit codes: 0 success · 1 trust operation error · 2 usage error
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import and inspect OACP trust-root entries."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser(
        "import", help="Import a public key stub into catalog + receiver pins"
    )
    imp.add_argument("stub", help="Path to a <kid>.pub.json stub from `oacp key gen`")
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

    args = parser.parse_args(list(argv) if argv is not None else None)

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
            POLICY_STATUS_VERIFIED,
            policy_context,
            record_policy_enrollment,
            sign_policy_file,
            verify_policy_file,
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

        reports = []
        for path, kind in targets:
            context = policy_context(args.project, args.agent, kind)
            report = sign_policy_file(path, signers, context=context)
            check = verify_policy_file(
                path, home, receiver=args.agent, context=context
            )
            report["verify_status"] = check["status"]
            reports.append(report)
            if check["status"] != POLICY_STATUS_VERIFIED:
                print(
                    f"ERROR: {path} did not round-trip verify after signing "
                    f"({check['status']}: {check['reason']})",
                    file=sys.stderr,
                )
                return 1
        # Enrollment is the point of no return for the unsigned-bootstrap
        # path, so it is recorded only after EVERY target round-trips: a
        # partial signing failure never strands an unsigned file behind
        # downgrade resistance.
        for path, kind in targets:
            record_policy_enrollment(
                home,
                policy_context(args.project, args.agent, kind),
                signers[0].kid,
            )
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
