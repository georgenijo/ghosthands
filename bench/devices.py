"""Hardware/OS capture so benchmark results are per-device and contributable.

`capture()` stamps a result file with the machine it ran on (chip, RAM, macOS,
cua-driver version, contributor). Numbers only mean something next to the
hardware that produced them — an Apple M4 mini and an M4 Pro MBP run the same
local model at very different speeds — so every results file carries this block
and `render.py` groups the leaderboard by it.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys


def _run(argv: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:  # noqa: BLE001 - missing tool / non-mac: degrade, don't crash
        return ""


def _sysctl(key: str) -> str:
    return _run(["sysctl", "-n", key])


def _git_user() -> str:
    return _run(["git", "config", "user.name"]) or os.environ.get("USER", "anon")


def _cua_version() -> str:
    out = _run([os.path.expanduser("~/.local/bin/cua-driver"), "--version"], 10)
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else out


def slug(chip: str, ram_gb: int) -> str:
    """Stable filename key, e.g. 'Apple M4 Pro' + 48 -> 'apple-m4-pro-48gb'."""
    base = re.sub(r"[^a-z0-9]+", "-", chip.lower()).strip("-") or "unknown"
    return f"{base}-{ram_gb}gb" if ram_gb else base


def capture(name: str = "", contributor: str = "", date: str = "") -> dict:
    """Describe the current machine. `name` overrides the auto label (use it for
    a friendly form factor, e.g. 'M4 Pro MacBook Pro 16\" 48GB')."""
    chip = _sysctl("machdep.cpu.brand_string") or "unknown"
    mem = _sysctl("hw.memsize")
    ram_gb = round(int(mem) / 1024**3) if mem.isdigit() else 0
    return {
        "name": name or f"{chip} · {ram_gb}GB",
        "slug": slug(chip, ram_gb),
        "chip": chip,
        "ram_gb": ram_gb,
        "model": _sysctl("hw.model"),
        "macos": _run(["sw_vers", "-productVersion"]),
        "cua_driver": _cua_version(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "contributor": contributor or _git_user(),
        "date": date or datetime.date.today().isoformat(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(capture(), indent=2))
