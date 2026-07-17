#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""key_cli.py — Generate and inspect OACP message-signing keys.

Usage:
    key_cli.py gen [--agent <name>] [--oacp-dir <path>] [--json]
    key_cli.py list [--agent <name>] [--oacp-dir <path>] [--json]

`gen` creates a dedicated Ed25519 keypair for the agent under
$OACP_HOME/keys/<trust_domain>/<agent>/<instance_id>/<kid>.json (0600, dirs
0700) plus a public catalog stub (<kid>.pub.json) for receiver-side trust
import (`oacp trust import`). Private keys never leave $OACP_HOME/keys/ and must never be
synced. Requires the optional crypto extra: pip install 'oacp-cli[crypto]'.

Exit codes: 0 success · 1 signing/key error · 2 usage error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_scripts_dir))

from message_signing import (  # noqa: E402
    SigningUnavailableError,
    generate_keypair,
    list_keys,
)


def _resolve_home(oacp_dir: str) -> Path:
    from _oacp_env import resolve_oacp_home

    return resolve_oacp_home(oacp_dir) if oacp_dir else resolve_oacp_home()


def _resolve_agent(agent: str) -> str:
    if agent:
        return agent
    env_agent = os.environ.get("OACP_AGENT", "").strip() or os.environ.get(
        "AGENT_NAME", ""
    ).strip()
    if env_agent:
        return env_agent
    from send_inbox_message import infer_current_runtime

    runtime = infer_current_runtime()
    if runtime:
        return runtime
    raise ValueError("cannot infer agent — use --agent, OACP_AGENT, or AGENT_NAME")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate and inspect OACP message-signing keys."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen", help="Generate a new Ed25519 signing key")
    gen.add_argument("--agent", default=None, help="Agent name (inferred if omitted)")
    gen.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    gen.add_argument("--json", action="store_true", dest="json_output")

    lst = sub.add_parser("list", help="List local signing keys")
    lst.add_argument("--agent", default=None, help="Filter by agent name")
    lst.add_argument("--oacp-dir", default=None, help="Override OACP home directory")
    lst.add_argument("--json", action="store_true", dest="json_output")

    args = parser.parse_args()

    try:
        oacp_home = _resolve_home(args.oacp_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.command == "gen":
        try:
            agent = _resolve_agent(args.agent)
            report = generate_keypair(agent, oacp_home)
        except (SigningUnavailableError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json_output:
            print(json.dumps(report, indent=2))
        else:
            print(f"OK: generated Ed25519 key for {report['agent']}")
            print(f"  kid:      {report['kid']}")
            print(f"  agent:    {report['agent_urn']}")
            print(f"  instance: {report['instance_urn']}")
            print(f"  key:      {report['key_path']}")
            print(f"  catalog:  {report['public_stub_path']}")
        return 0

    if args.command == "list":
        entries = list_keys(oacp_home, agent=args.agent)
        if args.json_output:
            print(json.dumps(entries, indent=2))
        elif not entries:
            print("No signing keys found.")
        else:
            for entry in entries:
                print(
                    f"{entry['agent']}  {entry['kid']}  "
                    f"{entry['created_at_utc']}  {entry['key_path']}"
                )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
