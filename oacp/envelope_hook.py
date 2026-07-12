# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Console entry point for the Claude PreToolUse envelope hook.

``oacp setup claude`` registers this command (``oacp-envelope-hook``) once in
``.claude/settings.json``; the per-task policy lives in the compiled
``active_envelope.json``, so the settings entry never changes per dispatch.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from oacp.cli import _run_script


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return _run_script("claude_envelope_hook.py", args)


if __name__ == "__main__":
    raise SystemExit(main())
