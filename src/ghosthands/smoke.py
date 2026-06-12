"""`ghosthands smoke` — prove the action wrapper end-to-end.

Drives macOS Calculator to compute 7 × 6 = 42, cursor-less, entirely through
the hardened wrapper: background launch, AX-only snapshots, click-by-AX-id
with re-snapshot before every action, verified after every state-changing
press. This is the M0 acceptance test (ROADMAP.md) and doubles as the smoke
test M2 reuses for every brain.
"""

from __future__ import annotations

import time

from .actions import App, Snapshot

CALCULATOR_BUNDLE_ID = "com.apple.calculator"
DISPLAY = "Edit field"  # AXStaticText label of Calculator's result display


def _display_equals(expected: str):
    def predicate(snap: Snapshot) -> bool:
        try:
            return snap.value_of(DISPLAY) == expected
        except Exception:
            return False
    return predicate


def _clear_to_zero(app: App, log) -> None:
    """Press the clear key until the display reads 0. One press of C only
    drops the last entry of a pending expression, and the button's AX id
    flips between Clear (mid-entry) and AllClear (cleared) — so loop on the
    observed display value instead of trusting a single press."""
    clear = lambda el: el.ax_id in ("AllClear", "Clear")
    for _ in range(5):
        if app.read(DISPLAY) == "0":
            return
        el = app.click(clear)
        log(f"  pressed [{el.index}] {el.role} {el.text!r} (id={el.ax_id})")
    raise RuntimeError("display did not clear to 0 after 5 presses")


def calculator_7x6(log=print) -> str:
    """Compute 7 × 6 on Calculator via AX ids; return the final display."""
    started = time.monotonic()
    log(f"launching {CALCULATOR_BUNDLE_ID} in the background…")
    app = App.launch(CALCULATOR_BUNDLE_ID)
    log(f"  pid={app.pid} window_id={app.window_id} (on-screen, no focus steal)")

    # Buttons are matched by AX id (Seven/Multiply/…) — unique to the keypad,
    # so menu items with the same visible label can never be hit. Every press
    # that changes the display carries a verify predicate; Multiply re-presses
    # are idempotent so it needs none. The display ("Edit field") holds the
    # RUNNING EXPRESSION while typing ("7 × 6"), collapsing to the result
    # after Equals.
    _clear_to_zero(app, log)
    steps = [
        ("Seven", _display_equals("7")),
        ("Multiply", None),
        ("Six", _display_equals("7 × 6")),
        ("Equals", _display_equals("42")),
    ]
    for target, verify in steps:
        el = app.click(target, verify=verify)
        log(f"  pressed [{el.index}] {el.role} {el.text!r} (id={el.ax_id})"
            + ("  ✓ verified" if verify else ""))

    result = app.read(DISPLAY)
    elapsed = time.monotonic() - started
    log(f"display reads {result!r} in {elapsed:.1f}s")
    return result


def run(log=print) -> bool:
    result = calculator_7x6(log=log)
    ok = result == "42"
    log(f"{'✅ smoke PASS' if ok else '❌ smoke FAIL'} — Calculator display: {result}")
    return ok
