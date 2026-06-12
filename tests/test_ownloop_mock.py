#!/usr/bin/env python3
"""Validate the Track B own-loop machinery with a scripted MockBrain — no API
key, no network. The mock behaves like a real brain: it resolves element
indices from the CURRENT state markdown each turn (never reuses stale ones)
and drives Calculator to 8 × 3 = 24, verifying from the shown state before
claiming done.

Run: python3 tests/test_ownloop_mock.py
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import ax  # noqa: E402
from ghosthands.ownloop import Decision, MockBrain, run_loop  # noqa: E402


def index_of(state: str, ax_id: str) -> int:
    for el in ax.parse_tree(state):
        if el.ax_id == ax_id and el.index is not None:
            return el.index
    raise AssertionError(f"no element with ax_id {ax_id!r} in state")


def display(state: str) -> str:
    for el in ax.parse_tree(state):
        if (el.label or "") == "Edit field":
            return el.value or ""
    return ""


def press(ax_id: str):
    def turn(state: str) -> Decision:
        return Decision(done=False, reason=f"press {ax_id}", actions=[
            {"tool": "click", "args": {"element_index": index_of(state, ax_id)}},
        ])
    return turn


def clear(state: str) -> Decision:
    if display(state) == "0":
        return Decision(done=False, reason="already clear", actions=[])
    target = "Clear" if any(e.ax_id == "Clear" for e in ax.parse_tree(state)) else "AllClear"
    return Decision(done=False, reason="clearing", actions=[
        {"tool": "click", "args": {"element_index": index_of(state, target)}},
    ])


def verify_24(state: str) -> Decision:
    value = display(state)
    if value == "24":
        return Decision(done=True, reason="display shows 24", actions=[])
    return Decision(done=False, reason=f"display shows {value!r}, not done", actions=[])


def main() -> int:
    subprocess.run(["osascript", "-e", 'tell application "Calculator" to quit'],
                   capture_output=True, timeout=15)
    time.sleep(1.5)

    brain = MockBrain(script=[
        clear, clear,  # may take two presses (C -> AC) from a stale expression
        press("Eight"), press("Multiply"), press("Three"), press("Equals"),
        verify_24,
    ])
    done = run_loop(brain, "Compute 8 × 3 on Calculator", "com.apple.calculator",
                    max_turns=10)

    # External world-check, independent of the brain's claim (TESTS.md rule)
    from ghosthands.actions import App
    final = App.launch("com.apple.calculator").read("Edit field")
    ok = done and final == "24"
    print(f"{'PASS' if ok else 'FAIL'}: brain done={done}, display={final!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
