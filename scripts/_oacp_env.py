# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Shared helper for resolving $OACP_HOME.

Provides a single ``resolve_oacp_home()`` function used by all Python
scripts that need the OACP root directory. Resolution order:

1. Explicit *path* argument (from ``--oacp-dir`` CLI flag)
2. ``$OACP_HOME`` environment variable
3. Repo/workspace marker discovery from ``.oacp`` or ``workspace.json``
4. ``~/oacp`` default
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _home_from_workspace_marker(path: Path) -> Optional[Path]:
    """Resolve ``<oacp_home>`` from a workspace marker path when possible."""
    if not path.exists() and not path.is_symlink():
        return None

    try:
        resolved = path.resolve()
    except OSError:
        return None

    if resolved.name != "workspace.json":
        return None

    project_dir = resolved.parent
    projects_dir = project_dir.parent
    if projects_dir.name != "projects":
        return None

    return projects_dir.parent


def _discover_oacp_home(cwd: Path) -> Optional[Path]:
    """Try to infer the OACP home from repo-local workspace markers.

    Both ``.oacp`` (symlink) and bare ``workspace.json`` are checked.
    False positives from non-OACP ``workspace.json`` files (e.g. VS Code)
    are prevented by ``_home_from_workspace_marker``'s structural guard:
    the resolved path must be named ``workspace.json`` inside a
    ``projects/<name>/`` subtree, which is OACP-specific.
    """
    for root in (cwd, *cwd.parents):
        for name in (".oacp", "workspace.json"):
            home = _home_from_workspace_marker(root / name)
            if home is not None:
                return home
    return None


def resolve_oacp_home(explicit: Optional[str] = None, cwd: Optional[Path] = None) -> Path:
    """Return the OACP home directory.

    Parameters
    ----------
    explicit:
        Value passed via ``--oacp-dir`` (or programmatic override).
        Takes highest precedence when non-``None``.
    cwd:
        Starting directory for marker discovery when ``OACP_HOME`` is unset.
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    oacp_home = os.environ.get("OACP_HOME")
    if oacp_home:
        return Path(oacp_home).expanduser()

    detected = _discover_oacp_home((cwd or Path.cwd()).expanduser())
    if detected is not None:
        return detected

    return Path(os.path.expanduser("~/oacp"))
