"""GhostHands — model-agnostic local macOS computer-use harness.

The "hands" are Cua Driver (an MCP server); the brain is swappable.
This package is the hardening layer: environment doctor, reliable
action wrapper, and (later) brain selector + launcher.
"""

__version__ = "0.1.0"

# Cua Driver version this harness is tested against. Prerelease — expect churn.
PINNED_DRIVER_VERSION = "0.5.1"
