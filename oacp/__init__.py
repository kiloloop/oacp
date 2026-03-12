# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""OACP package metadata."""

from __future__ import annotations

from importlib import metadata


def _detect_version() -> str:
    try:
        return metadata.version("oacp-cli")
    except metadata.PackageNotFoundError:
        return "0.1.0"


__version__ = _detect_version()

