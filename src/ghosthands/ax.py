"""Parser for the Markdown AX tree emitted by Cua's `get_window_state`.

Observed line shapes (cua-driver 0.5.1):

    - [0] AXWindow "Calculator" [id=main actions=[raise]]
        - AXStaticText = "81" (Edit field)
        - [5] AXButton (7) [id=Seven actions=[press]]
        - [22] AXButton (Show Sidebar) [id= help="Show Sidebar" actions=[press]]
    - [27] AXMenuBar [actions=[cancel]]
      - [30] AXMenuItem "About This Mac" [id=_aboutThisMacRequested: actions=[...]]

Quirks handled:
- A snapshot may contain TWO (or more) top-level AXWindow subtrees with
  different element_index ranges for the same on-screen window (DESIGN.md
  §8.2). Each element records which top-level subtree it belongs to so the
  action layer can fall back to the duplicate set when an index does not
  resolve.
- Some attribute blocks embed newlines (e.g. toolbar customization actions);
  physical lines that do not parse as elements are ignored.
- Non-actionable nodes (no [N] tag, e.g. AXStaticText) are kept — they carry
  the readable values used for verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LINE_RE = re.compile(r"^(?P<indent>\s*)- (?:\[(?P<idx>\d+)\]\s+)?(?P<role>AX\w+)(?P<rest>.*)$")
_VALUE_RE = re.compile(r'^\s*=\s*"(?P<value>.*?)"')
_TITLE_RE = re.compile(r'^\s*"(?P<title>.*?)"')
_LABEL_RE = re.compile(r"^\s*\((?P<label>[^)]*)\)")
_ATTRS_RE = re.compile(r"\s*\[(?=id=|help=|actions=)(?P<attrs>.*)\]\s*$", re.DOTALL)
_AX_ID_RE = re.compile(r"id=(?P<id>.*?)(?=\s+help=|\s+actions=|$)", re.DOTALL)
_ACTIONS_RE = re.compile(r"actions=\[(?P<actions>.*?)\]", re.DOTALL)


@dataclass
class Element:
    role: str
    index: int | None = None  # element_index; None for non-actionable nodes
    title: str | None = None  # quoted title, e.g. AXMenuItem "Copy"
    label: str | None = None  # parenthesized label, e.g. AXButton (7)
    value: str | None = None  # = "..." value, e.g. the Edit field text
    ax_id: str | None = None
    actions: list[str] = field(default_factory=list)
    depth: int = 0
    subtree: int = 0  # ordinal of the top-level node this element sits under

    @property
    def text(self) -> str:
        """Best human-readable name for matching."""
        return self.title or self.label or self.value or ""

    def __repr__(self) -> str:  # compact, for logs
        idx = f"[{self.index}] " if self.index is not None else ""
        return f"<{idx}{self.role} {self.text!r} subtree={self.subtree}>"


def parse_tree(markdown: str) -> list[Element]:
    elements: list[Element] = []
    subtree = -1
    for line in markdown.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue  # continuation of a multi-line attribute block
        indent = m.group("indent")
        depth = len(indent) // 2
        if not indent:
            subtree += 1

        rest = m.group("rest")
        value = title = label = ax_id = None
        actions: list[str] = []

        attrs_m = _ATTRS_RE.search(rest)
        if attrs_m:
            attrs = attrs_m.group("attrs")
            rest = rest[: attrs_m.start()]
            id_m = _AX_ID_RE.search(attrs)
            if id_m:
                ax_id = id_m.group("id").strip() or None
            act_m = _ACTIONS_RE.search(attrs)
            if act_m:
                actions = [a.strip() for a in act_m.group("actions").split(",") if a.strip()]

        val_m = _VALUE_RE.match(rest)
        if val_m:
            value = val_m.group("value")
            rest = rest[val_m.end():]
        else:
            title_m = _TITLE_RE.match(rest)
            if title_m:
                title = title_m.group("title")
                rest = rest[title_m.end():]

        label_m = _LABEL_RE.match(rest)
        if label_m:
            label = label_m.group("label")

        idx = m.group("idx")
        elements.append(
            Element(
                role=m.group("role"),
                index=int(idx) if idx is not None else None,
                title=title,
                label=label,
                value=value,
                ax_id=ax_id,
                actions=actions,
                depth=depth,
                subtree=max(subtree, 0),
            )
        )
    return elements
