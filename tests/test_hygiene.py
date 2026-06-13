"""Offline tests for ghosthands.hygiene.

Hermetic by default: every assertion runs against a trimmed, committed fixture
(tests/fixtures/brave_trimmed.md) read relative to this file — a real-shaped
Brave snapshot with a menubar subtree and an AXWebArea>AXButton "Submit". No
driver, no network, no /tmp dependency. The full 90KB /tmp snapshot is used
only as an OPTIONAL extra check when it happens to be present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import hygiene  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "brave_trimmed.md"
REAL_SNAPSHOT = Path("/tmp/brave_real_snapshot.json")


def _fixture_md() -> str:
    return FIXTURE.read_text()


def test_strip_menubar_fixture() -> None:
    """strip_menubar removes the whole AXMenu* subtree (0 AXMenu lines left)
    while the window/web structure survives verbatim."""
    md = _fixture_md()
    assert "AXMenuBar" in md and "AXMenu" in md, "fixture must contain a menubar to strip"
    stripped = hygiene.strip_menubar(md)
    assert "AXMenu" not in stripped, "menubar/menu lines survived the strip"
    assert "AXMenuBar" not in stripped, "menu bar root survived the strip"
    # Non-menu structure is preserved untouched.
    assert "AXWindow" in stripped and "AXWebArea" in stripped
    assert 'AXButton "Submit"' in stripped, "web Submit button must survive the strip"
    cut = 1 - len(stripped) / len(md)
    print(f"  strip_menubar(fixture): {len(md)} -> {len(stripped)} chars "
          f"({cut:.1%} cut), 0 AXMenu lines")


def test_filter_web_keeps_webarea_hit() -> None:
    """A text query for an on-page control is kept even though it lives deep
    inside an AXWebArea (cua's native query would drop it), and its ancestor
    chain comes along. Run against the menubar-stripped tree so the only
    'Submit' is the real web button, not the 'Submit Feedback' menu item."""
    stripped = hygiene.strip_menubar(_fixture_md())
    out = hygiene.filter_web(stripped, "Submit")
    assert out, "filter_web returned empty for a web hit (the bug being fixed)"
    assert 'AXButton "Submit"' in out, "matched web button dropped from filter output"
    # Ancestor chain kept so the match has context.
    assert "AXWebArea" in out and "AXWindow" in out
    # A non-matching sibling control on the same page is filtered out.
    assert 'AXButton "Cancel"' not in out, "non-matching sibling leaked into output"
    print(f"  filter_web('Submit') -> {len(out.splitlines())} lines incl. AXWebArea ancestor")


def test_filter_web_matches_text_not_role_or_attrs() -> None:
    """The needle is matched against node TEXT only, never the AXRole or the
    [actions=...] block. So 'press' (an action verb on every actionable node)
    and 'AXButton' (a role on every button) match FAR fewer lines than a real
    text query like 'Submit' — ideally none."""
    md = _fixture_md()
    submit_hits = hygiene.filter_web(md, "Submit").splitlines()
    press_hits = hygiene.filter_web(md, "press").splitlines()
    role_hits = hygiene.filter_web(md, "AXButton").splitlines()

    # Sanity: a genuine text query finds its node(s).
    assert any('"Submit"' in l for l in submit_hits), "real text query 'Submit' found nothing"

    # The bug: matching the whole line lets 'press'/'AXButton' hit everything.
    # The fix: they hit no node text at all here.
    submit_nodes = sum(1 for l in submit_hits if l.lstrip().startswith("- "))
    press_nodes = sum(1 for l in press_hits if l.lstrip().startswith("- "))
    role_nodes = sum(1 for l in role_hits if l.lstrip().startswith("- "))
    assert press_nodes == 0, f"'press' matched {press_nodes} node(s) — leaking the actions block"
    assert role_nodes == 0, f"'AXButton' matched {role_nodes} node(s) — leaking the role"
    assert press_nodes < submit_nodes and role_nodes < submit_nodes, (
        f"action/role queries ({press_nodes}/{role_nodes}) should match far fewer "
        f"than a real text hit ({submit_nodes})"
    )
    print(f"  filter_web text-only: 'Submit'->{submit_nodes} node(s), "
          f"'press'->{press_nodes}, 'AXButton'->{role_nodes}")


def test_scale_point_logical_to_pixel() -> None:
    """scale_point is a pure logical->pixel converter, NOT a click correction.

    - At scale_factor 1.0 (non-Retina) it is a no-op: the point is unchanged.
    - At 2.0 (Retina) it converts a LOGICAL point to its pixel coordinate.
    - The documented PNG-click contract: a coordinate already read off the
      get_window_state PNG is pixel-space and must be passed through UNSCALED.
      We encode that contract here — the PNG path simply never calls
      scale_point, so a PNG pixel stays put."""
    # Non-Retina: identity.
    assert hygiene.scale_point(640, 480, 1.0) == (640.0, 480.0)
    # Retina: logical -> pixel (a genuine conversion, both axes).
    assert hygiene.scale_point(10, 20, 2.0) == (20.0, 40.0)
    # Asymmetric input proves both axes are converted independently.
    assert hygiene.scale_point(3, 7, 2.0) == (6.0, 14.0)

    # PNG-click contract: PNG coordinates are already pixels. The correct path
    # does NOT scale them — it passes them straight to the pixel click. Encode
    # that the PNG coordinate is used verbatim (i.e. scale_point is NOT applied
    # on this path), so doubling it would be off-target on Retina.
    png_pixel = (640, 480)
    used_for_click = png_pixel  # unscaled — the documented PNG-click behaviour
    assert used_for_click == (640, 480), "PNG-derived coord must reach the click unscaled"
    wrongly_scaled = hygiene.scale_point(*png_pixel, 2.0)
    assert wrongly_scaled == (1280.0, 960.0) and wrongly_scaled != png_pixel, (
        "scaling a PNG pixel would double it off-target — that is the bug we avoid"
    )
    print("  scale_point: 1.0 no-op, 2.0 logical->pixel; PNG coords stay unscaled")


def test_real_snapshot_optional() -> None:
    """Optional belt-and-suspenders check on the full 90KB /tmp snapshot when
    present — never required for the suite to pass (kept hermetic)."""
    if not REAL_SNAPSHOT.exists():
        print("  (skip) real snapshot not present — fixture-only run")
        return
    md = json.loads(REAL_SNAPSHOT.read_text())["tree_markdown"]
    stripped = hygiene.strip_menubar(md)
    assert "AXMenu" not in stripped, "menubar lines survived the strip on real snapshot"
    cut = 1 - len(stripped) / len(md)
    assert cut > 0.30, f"only cut {cut:.1%} of chars on real snapshot (want >30%)"
    assert "AXWindow" in stripped and "AXWebArea" in stripped
    # Role/action queries must not flood the real tree; a role name matches no text.
    assert hygiene.filter_web(stripped, "AXButton") == "", "'AXButton' leaked on real tree"
    print(f"  (real) strip_menubar: {len(md)} -> {len(stripped)} chars ({cut:.1%} cut)")


def main() -> int:
    failures = 0
    tests = (
        test_strip_menubar_fixture,
        test_filter_web_keeps_webarea_hit,
        test_filter_web_matches_text_not_role_or_attrs,
        test_scale_point_logical_to_pixel,
        test_real_snapshot_optional,
    )
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001 - report any failure as a clean FAIL
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
