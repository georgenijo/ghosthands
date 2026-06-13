#!/usr/bin/env python3
"""Validate the funnel compactor on a REAL 87KB Brave AX snapshot — no model,
no driver, no network. Asserts the digest is a fraction of the original yet
still carries actionable [N] element lines, that the full markdown round-trips
through the offload handle, and that diff_lines surfaces only new lines.

Run: python3 tests/test_compaction.py
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands.compaction import compact, diff_lines  # noqa: E402

SNAPSHOT = "/tmp/brave_real_snapshot.json"


def main() -> int:
    markdown = json.load(open(SNAPSHOT))["tree_markdown"]
    result = compact(markdown)

    # Shrinks hard: 87k -> a few k.
    assert result["original_chars"] == len(markdown), "original_chars mismatch"
    assert result["compact_chars"] == len(result["text"]), "compact_chars mismatch"
    assert result["compact_chars"] < result["original_chars"], "did not shrink"
    assert result["reduction_pct"] > 80, f"only {result['reduction_pct']:.1f}% saved"

    # Still actionable: real [N] element lines survive (e.g. "[1] AXButton ...").
    assert re.search(r"\[\d+\] AX", result["text"]), "no [N] element line in digest"
    assert "BUTTONS" in result["text"], "no BUTTONS section"

    # Full original offloaded and re-reads byte-for-byte (original > max_chars).
    handle = result["handle"]
    assert handle is not None, "expected an offload handle for an 87KB snapshot"
    assert Path(handle).exists(), f"handle file missing: {handle}"
    assert Path(handle).read_text(encoding="utf-8") == markdown, "handle != original"

    # Small input under max_chars: no handle, still a valid digest.
    tiny = compact('- [0] AXWindow "Calc"\n  - [1] AXButton (7) [id=Seven actions=[press]]')
    assert tiny["handle"] is None, "tiny input should not offload"
    assert tiny["compact_chars"] > 0, "tiny input produced empty digest"

    # diff_lines: only lines new to `cur` show, in current order; blanks ignored.
    prev = "- [0] AXWindow\n- [1] AXButton (A)\n- [2] AXButton (B)"
    cur = "- [0] AXWindow\n- [1] AXButton (A)\n\n- [9] AXButton (NEW)"
    diff = diff_lines(prev, cur)
    assert diff == ["- [9] AXButton (NEW)"], f"unexpected diff: {diff!r}"
    assert diff_lines(prev, prev) == [], "identical snapshots should diff to []"

    o, c = result["original_chars"], result["compact_chars"]
    print(f"PASS: {o // 1000}k -> {c // 1000 or 1}k, "
          f"{result['reduction_pct']:.0f}% saved; handle={handle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
