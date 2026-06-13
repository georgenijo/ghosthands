"""Enable `python -m ghosthands ...` (mirrors the `ghosthands` console script).

The hub's `-m ghosthands hub` fallback registration relies on this when the
console script isn't on PATH inside an agent's MCP-spawn environment.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
