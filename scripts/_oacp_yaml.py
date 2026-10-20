# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Ordinary YAML mapping loading from already-read text.

This is not a signed-policy, audit, or duplicate-key-strict loader. Callers
retain their file/snapshot reads, bounded fallback parsers, and error policy.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


class YAMLMappingError(ValueError):
    """A parsed document has a non-mapping root."""


def load_yaml_mapping(
    text: str,
    *,
    string_scalars: bool = False,
    allow_empty: bool = False,
    fallback: Optional[Callable[[str], Any]] = None,
    parse_error: Optional[Callable[[Exception], Exception]] = None,
) -> Dict[str, Any]:
    """Load one mapping using SafeLoader or BaseLoader's string scalars.

    ``allow_empty`` maps a null/empty-document result to an empty mapping.
    A fallback is used only when importing PyYAML fails, never when parsing
    fails. Its errors pass through unchanged. ``parse_error`` lets callers
    retain their native-parser error wording without wrapping fallback errors.
    """
    try:
        import yaml
    except ImportError:
        if fallback is None:
            raise
        loaded = fallback(text)
    else:
        try:
            loaded = yaml.load(
                text, Loader=yaml.BaseLoader if string_scalars else yaml.SafeLoader
            )
        except Exception as exc:
            if parse_error is None:
                raise
            raise parse_error(exc) from exc

    if loaded is None and allow_empty:
        return {}
    if not isinstance(loaded, dict):
        raise YAMLMappingError("top-level YAML must be a mapping")
    return loaded
