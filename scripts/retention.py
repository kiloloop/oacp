#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Apply project-wide retention policy to OACP message history."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

DEFAULT_RETENTION_POLICY: Dict[str, Dict[str, Optional[int]]] = {
    "outbox": {"max_age_days": 30, "max_count": 1000},
    "dead_letter": {"max_age_days": 30, "max_count": 1000},
    "inbox_archive": {"max_age_days": 30, "max_count": 1000},
}

TARGET_PATHS = {
    "outbox": ("outbox",),
    "dead_letter": ("dead_letter",),
    "inbox_archive": ("inbox", "archive"),
}

# message_verify.quarantine_write_aside() produces
# <original>.<sha256[:8]>.<UTC stamp>[.<counter>]. That format is an
# allowlisted evidence marker, so every legacy and current quarantine copy is
# protected without parsing attacker-controlled content.
QUARANTINE_EVIDENCE_RE = re.compile(
    r"\.[0-9a-fA-F]{8}\.\d{8}T\d{6}Z(?:\.\d+)?$"
)


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    device: int
    inode: int
    mtime_ns: int
    size: int


def _is_real_directory(path: Path, *, visible: bool = False) -> bool:
    """Return whether *path* is a non-symlink directory."""
    if visible and path.name.startswith("."):
        return False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(metadata.st_mode)


def _target_directory(
    agent_dir: Path,
    relative_parts: Sequence[str],
) -> Optional[Path]:
    """Resolve a target only when every directory component is real."""
    current = agent_dir
    for part in relative_parts:
        current = current / part
        if not _is_real_directory(current):
            return None
    return current


def default_retention_policy() -> Dict[str, Dict[str, Optional[int]]]:
    """Return a mutable copy of the protocol defaults."""
    return {
        target: dict(values)
        for target, values in DEFAULT_RETENTION_POLICY.items()
    }


def _retention_value(path: str, value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{path} must be a positive integer or null")
    return value


def resolve_retention_policy(
    workspace: Mapping[str, Any],
) -> Dict[str, Dict[str, Optional[int]]]:
    """Validate and deep-merge the optional workspace retention override."""
    policy = default_retention_policy()
    if "retention" not in workspace:
        return policy

    raw = workspace["retention"]
    if not isinstance(raw, dict):
        raise ValueError("retention must be an object")

    unknown_targets = sorted(set(raw) - set(DEFAULT_RETENTION_POLICY))
    if unknown_targets:
        raise ValueError(
            "retention contains unknown target(s): " + ", ".join(unknown_targets)
        )

    for target, override in raw.items():
        if not isinstance(override, dict):
            raise ValueError(f"retention.{target} must be an object")
        unknown_keys = sorted(set(override) - {"max_age_days", "max_count"})
        if unknown_keys:
            raise ValueError(
                f"retention.{target} contains unknown key(s): "
                + ", ".join(unknown_keys)
            )
        for key, value in override.items():
            policy[target][key] = _retention_value(
                f"retention.{target}.{key}", value
            )
    return policy


def _validate_project_name(project: str) -> None:
    if not project or project.startswith(".") or "/" in project or "\\" in project:
        raise ValueError(
            "project name must not be empty, start with '.', or contain path separators"
        )


def _load_workspace(project_dir: Path) -> Dict[str, Any]:
    workspace_path = project_dir / "workspace.json"
    if not workspace_path.is_file():
        raise ValueError(f"workspace.json not found at {workspace_path}")
    try:
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"workspace.json is invalid JSON: {exc}") from exc
    if not isinstance(workspace, dict):
        raise ValueError("workspace.json root must be an object")
    return workspace


def _is_quarantine_evidence(path: Path) -> bool:
    if QUARANTINE_EVIDENCE_RE.search(path.name):
        return True
    if path.name.endswith(".quarantine"):
        return True
    return path.with_name(path.name + ".retain").is_file()


def _snapshot_directory(
    directory: Path,
    *,
    protect_quarantine: bool,
) -> Tuple[List[FileSnapshot], List[Path]]:
    candidates: List[FileSnapshot] = []
    protected: List[Path] = []
    if not _is_real_directory(directory):
        return candidates, protected

    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or path.name.endswith(".retain"):
            continue
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if protect_quarantine and _is_quarantine_evidence(path):
            protected.append(path)
            continue
        candidates.append(
            FileSnapshot(
                path=path,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                mtime_ns=metadata.st_mtime_ns,
                size=metadata.st_size,
            )
        )
    return candidates, protected


def _plan_pruning(
    candidates: Sequence[FileSnapshot],
    policy: Mapping[str, Optional[int]],
    *,
    now: dt.datetime,
) -> List[Tuple[FileSnapshot, List[str]]]:
    overflow: set[Path] = set()
    max_count = policy["max_count"]
    if max_count is not None:
        newest_first = sorted(
            candidates,
            key=lambda item: (item.mtime_ns, item.path.name),
            reverse=True,
        )
        overflow = {item.path for item in newest_first[max_count:]}

    cutoff_ns: Optional[int] = None
    max_age_days = policy["max_age_days"]
    if max_age_days is not None:
        cutoff = now - dt.timedelta(days=max_age_days)
        cutoff_ns = int(cutoff.timestamp() * 1_000_000_000)

    planned: List[Tuple[FileSnapshot, List[str]]] = []
    for item in sorted(candidates, key=lambda candidate: str(candidate.path)):
        reasons: List[str] = []
        if cutoff_ns is not None and item.mtime_ns < cutoff_ns:
            reasons.append("age")
        if item.path in overflow:
            reasons.append("count")
        if reasons:
            planned.append((item, reasons))
    return planned


def _same_file(snapshot: FileSnapshot, current: os.stat_result) -> bool:
    return (
        stat.S_ISREG(current.st_mode)
        and snapshot.device == current.st_dev
        and snapshot.inode == current.st_ino
        and snapshot.mtime_ns == current.st_mtime_ns
        and snapshot.size == current.st_size
    )


def _remove_if_unchanged(snapshot: FileSnapshot) -> Tuple[str, Optional[str]]:
    try:
        current = snapshot.path.lstat()
    except FileNotFoundError:
        return "skipped_missing", None
    if not _same_file(snapshot, current):
        return "skipped_changed", None
    try:
        snapshot.path.unlink()
    except OSError as exc:
        return "error", str(exc)
    return "pruned", None


def _utc_now(now: Optional[dt.datetime]) -> dt.datetime:
    value = now or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def run_retention(
    project: str,
    *,
    oacp_root: Path,
    dry_run: bool = False,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Apply one bounded retention pass and return a machine-readable report."""
    _validate_project_name(project)
    project_dir = Path(oacp_root) / "projects" / project
    if not project_dir.is_dir():
        raise ValueError(f"project '{project}' not found at {project_dir}")

    workspace = _load_workspace(project_dir)
    policy = resolve_retention_policy(workspace)
    current_time = _utc_now(now)
    agents_dir = project_dir / "agents"

    report: Dict[str, Any] = {
        "project": project,
        "dry_run": dry_run,
        "evaluated_at_utc": current_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "policy": policy,
        "scanned_count": 0,
        "protected_count": 0,
        "planned_count": 0,
        "pruned_count": 0,
        "skipped_changed_count": 0,
        "skipped_missing_count": 0,
        "error_count": 0,
        "protected": [],
        "actions": [],
    }
    if not _is_real_directory(agents_dir):
        return report

    for agent_dir in sorted(
        (
            path
            for path in agents_dir.iterdir()
            if _is_real_directory(path, visible=True)
        ),
        key=lambda path: path.name,
    ):
        for target, relative_parts in TARGET_PATHS.items():
            directory = _target_directory(agent_dir, relative_parts)
            if directory is None:
                continue
            candidates, protected = _snapshot_directory(
                directory,
                protect_quarantine=target == "dead_letter",
            )
            report["scanned_count"] += len(candidates)
            report["protected_count"] += len(protected)
            report["protected"].extend(
                str(path.relative_to(project_dir)) for path in protected
            )

            for snapshot, reasons in _plan_pruning(
                candidates, policy[target], now=current_time
            ):
                report["planned_count"] += 1
                status = "would_prune"
                error = None
                if not dry_run:
                    status, error = _remove_if_unchanged(snapshot)
                    if status == "pruned":
                        report["pruned_count"] += 1
                    elif status == "skipped_changed":
                        report["skipped_changed_count"] += 1
                    elif status == "skipped_missing":
                        report["skipped_missing_count"] += 1
                    elif status == "error":
                        report["error_count"] += 1

                action: Dict[str, Any] = {
                    "agent": agent_dir.name,
                    "target": target,
                    "path": str(snapshot.path.relative_to(project_dir)),
                    "reasons": reasons,
                    "status": status,
                }
                if error is not None:
                    action["error"] = error
                report["actions"].append(action)

    report["protected"].sort()
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prune OACP outbox, dead-letter, and inbox archive history.",
    )
    parser.add_argument("project", help="Project workspace name")
    parser.add_argument(
        "--oacp-dir",
        default=None,
        help="Override OACP home directory (default: $OACP_HOME or ~/oacp)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report eligible files without deleting them",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Emit JSON output"
    )
    return parser


def _render_report(report: Mapping[str, Any]) -> str:
    mode = "dry-run" if report["dry_run"] else "apply"
    lines = [f"Retention pass: {report['project']} ({mode})"]
    lines.append(
        "Scanned {scanned}; protected {protected}; planned {planned}; "
        "pruned {pruned}.".format(
            scanned=report["scanned_count"],
            protected=report["protected_count"],
            planned=report["planned_count"],
            pruned=report["pruned_count"],
        )
    )
    if not report["actions"]:
        lines.append("No files eligible for pruning.")
    for action in report["actions"]:
        reasons = "+".join(action["reasons"])
        lines.append(f"- {action['status']}: {action['path']} ({reasons})")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    from _oacp_env import resolve_oacp_home

    oacp_root = resolve_oacp_home(explicit=args.oacp_dir)
    try:
        report = run_retention(
            args.project,
            oacp_root=oacp_root,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        if args.json_output:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(report, indent=2))
    else:
        print(_render_report(report))
    return 1 if report["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
