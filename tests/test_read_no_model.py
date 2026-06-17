#!/usr/bin/env python3
"""Acceptance guard: the no-brain tiers are MODEL-FREE.

`ghosthands.read` (issue #35) and `ghosthands.act` (model-free driving) must
never drag in the model stack — importing either must pull in NO `mlx*` module
and NOT `ghosthands.ownloop`. That boundary is the whole point: read/verify and
name-targeted click/type are instant + $0 precisely because they never spin up
a brain.

This runs each import in a FRESH subprocess (so nothing another test or this
harness already imported can mask a regression) and inspects sys.modules.

Run: python3 tests/test_read_no_model.py
"""

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")

# The modules that must never appear in a no-brain tier's fresh import.
MODEL_FREE_MODULES = ("ghosthands.read", "ghosthands.act")

# Imported in a clean interpreter; prints the offending modules (if any).
PROBE = r"""
import sys
sys.path.insert(0, {src!r})
import {module}  # noqa: F401

bad = sorted(
    m for m in sys.modules
    if m == "mlx" or m.startswith("mlx.") or m == "ghosthands.ownloop"
)
print("\n".join(bad))
"""


def _modules_after_importing(module: str) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", PROBE.format(src=SRC, module=module)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"importing {module} failed:\n{proc.stderr}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_imports_no_model_stack() -> None:
    for module in MODEL_FREE_MODULES:
        offenders = _modules_after_importing(module)
        assert not offenders, (
            f"{module} pulled in the model stack — the no-brain tier must be "
            f"model-free. Offending modules: {offenders}")


def test_module_imports_no_ownloop_in_source() -> None:
    """Belt-and-braces: no `from . import ownloop` / `import ownloop` statement
    on any code path, so a lazy import can't sneak the model stack in past the
    runtime probe above. (Docstring prose mentioning the names is fine — we
    look only for actual import statements.)"""
    import re
    for module in MODEL_FREE_MODULES:
        path = Path(SRC) / Path(*module.split(".")).with_suffix(".py")
        src = path.read_text()
        bad = re.findall(r"^\s*(?:from\s+\S+\s+)?import\s+.*\b(?:ownloop|mlx)\b",
                         src, re.MULTILINE)
        assert not bad, (
            f"{path.name} has a model-stack import statement: {bad} "
            "— keep the no-brain path model-free")


def main() -> int:
    tests = [
        test_imports_no_model_stack,
        test_module_imports_no_ownloop_in_source,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\nALL {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
