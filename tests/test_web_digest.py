"""Offline tests for the web-scoped digest (issue #10) and surface routing
(issue #9).

Hermetic: every assertion runs against the committed Brave snapshot
(tests/fixtures/brave_trimmed.md) — a real-shaped tree with browser chrome
(Back/Forward/address bar) OUTSIDE an AXWebArea and the page's own controls
(Submit/Cancel/version field) inside it. No driver, no Brave, no network. The
live DOM-piercing proof lives in tests/test_webtier.py behind GH_LIVE."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import compaction, webtier  # noqa: E402
from ghosthands.ownloop import (  # noqa: E402
    _ACTIONABLE_ROLES, _web_area_members, actionable_digest,
)
from ghosthands import ax  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "brave_trimmed.md"


def _md() -> str:
    return FIXTURE.read_text()


def test_web_scope_drops_chrome_and_structural() -> None:
    """web_scope keeps only the page's actionable controls: browser chrome
    (outside the AXWebArea) and non-actionable structural nodes are dropped,
    while the real web controls survive."""
    before, _ = actionable_digest(_md())
    after, _ = actionable_digest(_md(), web_scope=True)
    b_lines = [l for l in before.splitlines() if l.strip()]
    a_lines = [l for l in after.splitlines() if l.strip()]

    assert len(a_lines) < len(b_lines), "web_scope did not shrink the digest"
    # the page's own controls survive
    assert "'Submit'" in after and "'Cancel'" in after and "'Version number'" in after
    # browser chrome (outside the web area) is gone
    assert "'Back'" not in after and "'Forward'" not in after
    assert "Address and search bar" not in after
    # zero non-actionable lines remain (AXWebArea/AXHeading/AXList/AXStaticText)
    def role_of(line: str) -> str:
        return line.strip().split(maxsplit=2)[1]
    assert all(role_of(l) in _ACTIONABLE_ROLES for l in a_lines), \
        "a non-actionable structural node leaked into the web-scoped digest"
    print(f"  web_scope: {len(b_lines)} -> {len(a_lines)} lines, "
          f"{len(before)} -> {len(after)} chars")


def test_native_digest_unchanged_regression_guard() -> None:
    """The regression guard for #10: a native snapshot is NEVER web-scoped,
    because the trigger is `"AXWebArea" in markdown` and a native tree has none.
    So the loop/funnel run the default path on native, byte-identical to the
    pre-#10 behaviour."""
    native = (
        '- [0] AXWindow "Calculator"\n'
        '  - AXStaticText = "0" (Edit field)\n'
        '  - [5] AXButton (7) [id=Seven actions=[press]]\n'
        '  - [6] AXButton (8) [id=Eight actions=[press]]\n'
    )
    # 1. the production trigger never fires on native
    assert "AXWebArea" not in native, "native fixture must have no web area"
    assert "AXWebArea" in _md(), "the web fixture must have an AXWebArea"

    # 2. the default native digest is unchanged (the calculator buttons survive,
    #    the structural window node stays exactly as it did pre-#10)
    default, dval = actionable_digest(native)
    assert "'7'" in default and "'8'" in default
    assert "AXWindow" in default, "native digest shape changed (regression!)"
    assert dval.strip() != "", "the Edit field value must still be read"
    print("  native is never web-scoped (no AXWebArea) -> default path unchanged")


def test_web_area_members_marks_descendants() -> None:
    """_web_area_members marks exactly the AXWebArea's descendants by depth —
    chrome before it and the menubar after it stay unmarked."""
    els = ax.parse_tree(_md())
    members = _web_area_members(els)
    by_idx = {el.index: members[i] for i, el in enumerate(els) if el.index is not None}
    # inside the web area
    assert by_idx.get(73) is True, "Submit (inside AXWebArea) not marked"
    assert by_idx.get(49) is True, "App Store Connect link (inside) not marked"
    # browser chrome, before the web area
    assert by_idx.get(1) is False, "Back button (chrome) wrongly marked"
    assert by_idx.get(6) is False, "address bar (chrome) wrongly marked"


def test_compaction_funnel_web_scopes() -> None:
    """ghosthands compact auto-web-scopes a snapshot that has an AXWebArea, so
    the CLI funnel and the live loop agree (issue #10)."""
    res = compaction.compact(_md())
    assert "'Submit'" in res["text"]
    assert "'Back'" not in res["text"], "browser chrome leaked through the funnel"
    # a native tree (no AXWebArea) is NOT web-scoped by the funnel
    native = '- [0] AXWindow "Calculator"\n  - [5] AXButton (7) [id=Seven]\n'
    assert "'7'" in compaction.compact(native)["text"]


def test_route_surface() -> None:
    """route_surface: browser bundle OR an AXWebArea in the snapshot -> web;
    everything else -> native (issue #9)."""
    assert webtier.route_surface(bundle_id="com.brave.Browser") == "web"
    assert webtier.route_surface(bundle_id="com.google.Chrome") == "web"
    assert webtier.route_surface(bundle_id="com.apple.Safari") == "web"
    assert webtier.route_surface(bundle_id="com.apple.calculator") == "native"
    assert webtier.route_surface(bundle_id="com.apple.systempreferences") == "native"
    # snapshot signal, no bundle id
    assert webtier.route_surface(markdown="- [0] AXWindow\n  - [1] AXWebArea") == "web"
    assert webtier.route_surface(markdown="- [0] AXWindow\n  - [1] AXButton") == "native"
    # an embedded web view inside a native app still routes web for its content
    assert webtier.route_surface(bundle_id="com.acme.app",
                                 markdown="- [0] AXWindow\n  - [9] AXWebArea") == "web"
    assert webtier.route_surface() == "native"  # nothing known -> native


def main() -> int:
    failures = 0
    tests = (
        test_web_scope_drops_chrome_and_structural,
        test_native_digest_unchanged_regression_guard,
        test_web_area_members_marks_descendants,
        test_compaction_funnel_web_scopes,
        test_route_surface,
    )
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
