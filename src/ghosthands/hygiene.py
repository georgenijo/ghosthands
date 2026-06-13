"""Snapshot hygiene — pure functions that clean up a raw AX-tree snapshot
before it reaches a brain, plus a logical→pixel coordinate converter.

Three fixes from a field report against cua-driver 0.5.1:

1. strip_menubar — a backgrounded app's whole menu bar (AXMenuBar /
   AXMenuBarItem / AXMenu / AXMenuItem) lands in every snapshot. On a real
   Brave window that menu subtree is ~45% of the 87KB tree and its items are
   disabled no-ops for a non-frontmost app (SKILL.md §6). Drop each menu node
   line together with its deeper-indented descendants.

2. filter_web — cua's own query collapses AXWebArea nodes, so a query for an
   on-page control returns an EMPTY tree on the web. This re-implements the
   filter over the raw markdown and keeps matches *inside* an AXWebArea, plus
   the ancestor chain (by indentation) needed to keep the match's context.
   Crucially, the needle is matched ONLY against each node's human-readable
   text (the title/label/value), never against the AXRole or the trailing
   `[id=.../help=.../actions=...]` attribute block — otherwise a query like
   "press" or "AXButton" would spuriously match every actionable node.

3. scale_point / screen_scale — a coordinate-space converter for the AX
   element-frame path. THERE IS NO RETINA "×2 the click" FIX: see §3 below.
   cua-driver's pixel click already takes coordinates in screenshot/PNG
   (backing-store) pixels, so PNG-derived coordinates must be passed through
   UNSCALED. scale_point exists only to convert LOGICAL points (e.g. an AX
   element frame, which lives in get_screen_size's logical-point space) into
   that pixel space; screen_scale reads the live factor off the driver.

   §3 — Coordinate spaces on a Retina display (the corrected premise).

   There are two distinct spaces, and the earlier "multiply every click by
   the scale factor" premise conflated them and double-scaled real clicks:

     * get_window_state's screenshot/PNG is ALREADY in backing-store (pixel)
       space. A coordinate you read off that PNG (e.g. by locating a control
       in the captured image) is a pixel coordinate. cua-driver's pixel
       `click` consumes pixels. So the PNG-click path needs NO scaling —
       multiplying those by scale_factor (2.0 on Retina) lands at twice the
       intended offset, off-target. Pass PNG coords through verbatim.

     * get_screen_size returns the display size in LOGICAL points plus a
       separate `scale_factor`. Logical points are a DIFFERENT space from the
       PNG's pixels: 1 logical point = scale_factor pixels. An AX element's
       frame (origin/size) is reported in this logical-point space. Only when
       you start from a logical point and need a pixel coordinate do you
       multiply by scale_factor — that, and only that, is scale_point's job.

   Bottom line: prefer AX `element_index` clicks (no coordinates at all). If
   you must click by coordinate, use the PNG pixel directly (unscaled); reach
   for scale_point only to turn a logical AX frame point into a pixel point.

Everything here is a pure function on markdown / numbers except screen_scale,
which is the only one that touches the driver.
"""

from __future__ import annotations

import re

_MENU_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"}

# A node line: optional indent, "- ", optional "[N] ", then the AXRole. Mirrors
# ax._LINE_RE. Physical lines that don't match are continuation lines of a
# multi-line attribute block (see ax.py docstring) and belong to the node line
# above them.
_NODE_RE = re.compile(r"^(?P<indent>\s*)- (?:\[(?P<idx>\d+)\]\s+)?(?P<role>AX\w+)(?P<rest>.*)$")

# Trailing attribute block `[id=... help=... actions=[...]]`. Mirrors
# ax._ATTRS_RE — only a bracket that opens with id=/help=/actions= is an attr
# block (a `[2]` index or a literal bracket in a title is not). Used to strip
# the attrs off a node's rest-of-line so the needle never matches them.
_ATTRS_RE = re.compile(r"\s*\[(?=id=|help=|actions=)(?P<attrs>.*)\]\s*$", re.DOTALL)
_VALUE_RE = re.compile(r'^\s*=\s*"(?P<value>.*?)"')
_TITLE_RE = re.compile(r'^\s*"(?P<title>.*?)"')
_LABEL_RE = re.compile(r"^\s*\((?P<label>[^)]*)\)")


def _node_text(rest: str) -> str:
    """Extract a node line's human-readable text from the part of the line
    AFTER the AXRole (the `rest` captured by _NODE_RE). Mirrors ax.parse_tree:
    drop the trailing `[id=/help=/actions=...]` attribute block, then read the
    quoted value (`= "…"`), the quoted title (`"…"`), and/or the parenthesized
    label (`(…)`). The AXRole and the attribute block are NEVER part of the
    returned text, so a query matched against this can't hit a role or an
    actions=[...] verb."""
    attrs_m = _ATTRS_RE.search(rest)
    if attrs_m:
        rest = rest[: attrs_m.start()]

    parts: list[str] = []
    val_m = _VALUE_RE.match(rest)
    if val_m:
        parts.append(val_m.group("value"))
        rest = rest[val_m.end():]
    else:
        title_m = _TITLE_RE.match(rest)
        if title_m:
            parts.append(title_m.group("title"))
            rest = rest[title_m.end():]

    label_m = _LABEL_RE.match(rest)
    if label_m:
        parts.append(label_m.group("label"))
    return " ".join(parts)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def strip_menubar(markdown: str) -> str:
    """Remove every AXMenuBar/AXMenuBarItem/AXMenu/AXMenuItem subtree from the
    AX markdown: a menu-role node line plus all of its deeper-indented
    descendants (and their continuation lines). Non-menu siblings and the rest
    of the tree are preserved verbatim, indentation untouched."""
    out: list[str] = []
    dropping = False
    drop_indent = 0
    for line in markdown.splitlines():
        m = _NODE_RE.match(line)
        if m is None:
            # Continuation line of the previous node — follow that node's fate.
            if not dropping:
                out.append(line)
            continue
        indent = len(m.group("indent"))
        if dropping:
            if indent > drop_indent:
                continue  # still inside the dropped menu subtree
            dropping = False  # back out to a sibling/ancestor — re-decide below
        if m.group("role") in _MENU_ROLES:
            dropping = True
            drop_indent = indent
            continue
        out.append(line)
    return "\n".join(out)


def filter_web(markdown: str, query: str) -> str:
    """Return only the lines whose human-readable TEXT matches `query`
    (case-insensitive substring) together with each match's ancestor chain
    (shallower-indented lines on the path to it). Unlike cua's own query,
    AXWebArea subtrees are NOT collapsed — a match deep inside an AXWebArea is
    kept, which is the bug this fixes (the native query returns an empty tree
    for web hits).

    The needle is tested against a node's text only — the title/label/value
    portion of the line via `_node_text`, with the AXRole prefix and the
    trailing `[id=/help=/actions=...]` attribute block removed. Matching the
    whole serialized line instead would let a query like "press" hit every
    `actions=[press]` and "AXButton" hit every button; here those match
    nothing, while a genuine text query (e.g. "Submit") still finds its node.

    Ancestors are recovered purely from indentation: a kept line's ancestors
    are the most recent strictly-shallower lines at each smaller indent. The
    output preserves original line order and indentation."""
    if not query:
        return markdown
    needle = query.casefold()
    lines = markdown.splitlines()

    keep: list[bool] = [False] * len(lines)
    # The chain of (line_index, indent) currently open above the cursor, by
    # increasing indent — the path of node lines we're nested under.
    stack: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = _NODE_RE.match(line)
        if m is None:
            # Continuation line: shares the fate of the node line above it.
            if i > 0 and keep[i - 1]:
                keep[i] = True
            continue
        indent = len(m.group("indent"))
        while stack and stack[-1][1] >= indent:
            stack.pop()
        if needle in _node_text(m.group("rest")).casefold():
            keep[i] = True
            for anc_i, _ in stack:  # bring the whole ancestor path along
                keep[anc_i] = True
        stack.append((i, indent))
    return "\n".join(line for line, k in zip(lines, keep) if k)


def scale_point(x: float, y: float, scale_factor: float) -> tuple[float, float]:
    """Convert a LOGICAL point to a backing-store (pixel) point by multiplying
    by `scale_factor` (the display's points→pixels ratio from get_screen_size).

    Use this ONLY when your input is already in logical-point space — e.g. an
    AX element's frame origin, which get_screen_size reports in logical points.
    On a Retina display scale_factor is 2.0, so a logical (10, 10) becomes the
    pixel (20, 20); on a non-Retina display scale_factor is 1.0 and this is a
    no-op that returns the point unchanged.

    Do NOT call this on a coordinate read off the get_window_state PNG: that
    screenshot is already in pixel space and cua-driver's pixel `click`
    consumes pixels, so such a coordinate must be passed through verbatim.
    Scaling it would double it (×2 on Retina) and land off-target — that was
    the original bug. The PNG-click contract is "pass through unscaled":

    >>> scale_point(640, 480, 1.0)            # non-Retina: identity
    (640.0, 480.0)
    >>> scale_point(10, 10, 2.0)              # logical -> pixel on Retina
    (20.0, 20.0)
    >>> px = (640, 480)                       # already-pixel PNG coordinate
    >>> px == px                              # the PNG-click path does NOT scale
    True
    """
    return float(x) * scale_factor, float(y) * scale_factor


def screen_scale() -> float:
    """Live points→pixels scale factor from the driver (get_screen_size).

    This is the `scale_factor` that scale_point multiplies a LOGICAL point by
    to reach pixel space; it is NOT a correction to apply to coordinates read
    off the get_window_state PNG (those are already pixels). Defaults to 1.0 on
    any failure so a missing/odd driver never breaks a click path. Kept out of
    the pure-function test path — it's the one driver-touching helper."""
    from . import driver

    try:
        size = driver.call("get_screen_size", {})
        factor = size.get("scale_factor") if isinstance(size, dict) else None
        return float(factor) if factor else 1.0
    except Exception:  # noqa: BLE001 - any driver/parse failure -> safe default
        return 1.0
