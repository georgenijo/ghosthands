"""Compact fat tool outputs at the funnel before they reach a model.

A raw `get_window_state` AX snapshot is huge (a real Brave tab is ~87KB of
markdown, mostly menu-bar subtrees and non-actionable chrome). Feeding that
verbatim into a context window every turn is the dominant token cost of the
loop and drowns the few lines a brain actually acts on.

`compact` runs the snapshot through the existing `ownloop.actionable_digest`
(menus dropped, id-pinned duplicates collapsed, second-window twin cut) and
returns the two things a text brain needs — the actionable BUTTONS list and the
DISPLAY values — plus the reduction stats. When the original exceeds
`max_chars` the FULL markdown is offloaded to a file and a `handle` path is
returned, so nothing is lost: a caller that needs the raw tree (a rare
disambiguation, a debug dump) can read it back on demand instead of carrying it
through every prompt.

`diff_lines` is the companion "only what changed" tactic: between two
consecutive snapshots, the lines present in the current one but absent from the
previous one are usually the entire signal (a new dialog, an updated value),
and are a fraction of the size of either full tree.
"""

from __future__ import annotations

import hashlib
import os

from .ownloop import actionable_digest


def _digest_text(markdown: str) -> str:
    """Build the brain-facing digest string: BUTTONS list + DISPLAY values,
    reusing ownloop.actionable_digest so the funnel and the loop stay in sync.
    A web snapshot (AXWebArea present) is scoped to the page's controls —
    browser chrome + structural noise dropped (issue #10), matching the loop."""
    buttons, values = actionable_digest(markdown, web_scope="AXWebArea" in markdown)
    parts = ["BUTTONS (act by element_index):", buttons or "(none)"]
    parts += ["", "DISPLAY:", values or "(none)"]
    return "\n".join(parts)


def compact(markdown: str, *, max_chars: int = 8000,
            offload_dir: str = "/tmp/gh-compaction") -> dict:
    """Compact a raw AX-tree snapshot into an actionable digest.

    Returns a dict with:
      text           — the compacted digest (BUTTONS list + DISPLAY values)
      original_chars — len of the input markdown
      compact_chars  — len of `text`
      reduction_pct  — percentage of characters saved (0.0 for empty input)
      handle         — path to a file holding the FULL original markdown when
                       original_chars > max_chars, else None (the digest alone
                       is small enough; nothing to offload)
    """
    text = _digest_text(markdown)
    original_chars = len(markdown)
    compact_chars = len(text)
    reduction_pct = (
        100.0 * (original_chars - compact_chars) / original_chars
        if original_chars else 0.0
    )

    handle: str | None = None
    if original_chars > max_chars:
        os.makedirs(offload_dir, exist_ok=True)
        digest = hashlib.sha1(markdown.encode("utf-8")).hexdigest()[:16]
        handle = os.path.join(offload_dir, f"snapshot-{digest}.md")
        with open(handle, "w", encoding="utf-8") as fh:
            fh.write(markdown)

    return {
        "text": text,
        "original_chars": original_chars,
        "compact_chars": compact_chars,
        "reduction_pct": reduction_pct,
        "handle": handle,
    }


def diff_lines(prev_markdown: str, cur_markdown: str) -> list[str]:
    """Lines present in `cur_markdown` but not in `prev_markdown`, in their
    current order — the "only what changed" view of a snapshot transition.

    Set membership (not positional diff): a line that merely moved is not a
    change, while a genuinely new line (a new control, an updated value node)
    surfaces. Blank lines are ignored so reindented chrome doesn't show up."""
    prev = set(prev_markdown.splitlines())
    out: list[str] = []
    for line in cur_markdown.splitlines():
        if line.strip() and line not in prev:
            out.append(line)
    return out
