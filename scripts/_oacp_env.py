# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Shared helper for resolving $OACP_HOME.

Provides a single ``resolve_oacp_home()`` function used by all Python
scripts that need the OACP root directory.  Resolution order:

1. Explicit *path* argument (from ``--oacp-dir`` CLI flag)
2. ``$OACP_HOME`` environment variable
3. ``~/oacp`` default
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def resolve_oacp_home(explicit: Optional[str] = None) -> Path:
    """Return the OACP home directory.

    Parameters
    ----------
    explicit:
        Value passed via ``--oacp-dir`` (or programmatic override).
        Takes highest precedence when non-``None``.
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    oacp_home = os.environ.get("OACP_HOME")
    if oacp_home:
        return Path(oacp_home).expanduser()

    return Path(os.path.expanduser("~/oacp"))
