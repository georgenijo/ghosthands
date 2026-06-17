"""The no-brain ACT tier — click / type by name, no model.

The symmetric act half of the read tier (`read.py`). `find` *locates* a target
by name; these *press* or *type* it. The model is only ever needed to DECIDE
which element to touch — once you NAME the target, acting is pure AXPress
mechanics, so it needs no brain (the same way `replay` drives recorded targets
with no model). Naming the target is what removes the model from the loop.

ACCEPTANCE: imports neither `ownloop` nor `mlx` (enforced by
tests/test_read_no_model.py). Reuses `actions.App` — resolve a name against a
fresh snapshot, fire-and-go, settle, and honesty-refuse when the target isn't
on screen — and `read.resolve_target` (pid / bundle id / process name).
"""

from __future__ import annotations

import sys

from . import driver, read
from .actions import App, GhostHandsError

# Errors the act verbs turn into a clean one-line failure + exit 1, never a
# traceback: target-not-on-screen (ElementNotFoundError, a GhostHandsError) and
# a persistent driver fault the wrapper's retries couldn't ride out.
_CLEAN = (GhostHandsError, driver.DriverError)


def _resolve(verb: str, spec: str, *, title_contains: str | None) -> App | int:
    try:
        return read.resolve_target(spec, title_contains=title_contains)
    except _CLEAN as e:
        print(f"{verb} failed: {e}", file=sys.stderr)
        return 1


def click(name: str, spec: str, *, title_contains: str | None = None,
          action: str | None = None, out=sys.stdout) -> int:
    """Press the element named `name` in `spec`'s window. NO model.

    Honest by construction: `App.click` re-resolves the name against a fresh
    snapshot and refuses (ElementNotFoundError) if it isn't on screen, so a
    miss is a clean exit 1 — never a press into the void reported as success.

    `action` invokes a named AX action instead of the default AXPress
    (`open` / `confirm` / `pick` / `show_menu` / `cancel`, or `raise` on a
    window) — what file pickers and openable rows need (press just renames
    them). It's still an AX action: background-safe, no focus steal.
    """
    app = _resolve("click", spec, title_contains=title_contains)
    if isinstance(app, int):
        return app
    try:
        el = app.click(name, action=action)
    except _CLEAN as e:
        print(f"click failed: {e}", file=sys.stderr)
        return 1
    did = f"invoked {action!r} on" if action else "clicked"
    print(f"{did} {name!r} (role={el.role} index={el.index}) "
          f"in {app.name or spec}", file=out)
    return 0


def doubleclick(name: str, spec: str, *, title_contains: str | None = None,
                out=sys.stdout) -> int:
    """Double-click the element named `name` in `spec`'s window. NO model.

    cua's `double_click` (AXOpen when advertised, else a pixel double at the
    element's centre) — for rows/items that select or open on double-click.
    Same honesty contract as `click`: re-resolves against a fresh snapshot and
    exits 1 cleanly when the name isn't on screen.
    """
    app = _resolve("doubleclick", spec, title_contains=title_contains)
    if isinstance(app, int):
        return app
    try:
        el = app.double_click(name)
    except _CLEAN as e:
        print(f"doubleclick failed: {e}", file=sys.stderr)
        return 1
    print(f"double-clicked {name!r} (role={el.role} index={el.index}) "
          f"in {app.name or spec}", file=out)
    return 0


# NOTE: a `type` verb is deliberately NOT here yet. Synthetic keystrokes
# (driver `type_text`) route to the FRONTMOST app, so they silently no-op on a
# backgrounded window — verified: `type "8*8="` left Calculator's display at
# "0". A background-safe `type` must write via AX `set_value` on a named field
# and then honesty-verify the value actually changed (never report success on a
# no-op). That, plus how to target an unnamed/empty field, is a follow-up.
