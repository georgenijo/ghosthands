#!/usr/bin/env python3
"""Acceptance guard for issue #35: the read tier is MODEL-FREE.

`ghosthands.read` must never drag in the model stack — importing it must pull
in NO `mlx*` module and NOT `ghosthands.ownloop`. That boundary is part of the
epic's acceptance criteria: read/verify is instant + $0 precisely because it
never spins up a brain.

This runs the import in a FRESH subprocess (so nothing another test or this
harness already imported can mask a regression) and inspects sys.modules.

Run: python3 tests/test_read_no_model.py
"""

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")

# Imported in a clean interpreter; prints the offending modules (if any).
PROBE = r"""
import sys
sys.path.insert(0, {src!r})
import ghosthands.read  # noqa: F401

bad = sorted(
    m for m in sys.modules
    if m == "mlx" or m.startswith("mlx.") or m == "ghosthands.ownloop"
)
print("\n".join(bad))
"""


def _modules_after_importing_read() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", PROBE.format(src=SRC)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"importing ghosthands.read failed:\n{proc.stderr}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_read_imports_no_model_stack() -> None:
    offenders = _modules_after_importing_read()
    assert not offenders, (
        "ghosthands.read pulled in the model stack — the read tier must be "
        f"model-free (issue #35). Offending modules: {offenders}")


def test_read_module_imports_no_ownloop_in_source() -> None:
    """Belt-and-braces: no `from . import ownloop` / `import ownloop` statement
    on any code path, so a lazy import can't sneak the model stack in past the
    runtime probe above. (Docstring prose mentioning the names is fine — we
    look only for actual import statements.)"""
    import re
    src = (Path(SRC) / "ghosthands" / "read.py").read_text()
    bad = re.findall(r"^\s*(?:from\s+\S+\s+)?import\s+.*\b(?:ownloop|mlx)\b",
                     src, re.MULTILINE)
    assert not bad, (
        f"ghosthands/read.py has a model-stack import statement: {bad} "
        "— keep the read path model-free (issue #35)")


def main() -> int:
    tests = [
        test_read_imports_no_model_stack,
        test_read_module_imports_no_ownloop_in_source,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\nALL {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
