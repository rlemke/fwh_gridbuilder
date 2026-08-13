# SPDX-License-Identifier: Apache-2.0
"""Shim: puts tools/ on sys.path and re-exports the shared library.

Handlers import from here rather than reaching into `tools/` themselves, so the
CLI and the FFL runtime exercise literally the same functions.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _gridbuilder_tools import retrieve, validate  # noqa: E402

__all__ = ["retrieve", "validate"]
