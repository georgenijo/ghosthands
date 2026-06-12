"""Scene generation — the second half of the two-model routing.

The click loop runs a small fast model (LocalBrain, 4B); canvas work routes
HERE, to a larger model that the model gate showed is the only local one that
produces schema-valid, fully-labeled Excalidraw scenes (and the only one that
declines impossible goals instead of guessing). One scene generation replaces
minutes of mouse-drawing — see skills/CANVAS.md for the injection recipes.

Usage:
    from ghosthands.scene import generate_scene
    elements = generate_scene("five boxes connected left to right ...")

CLI:  ghosthands scene "<description>" [--raw] [--model <id>]
      prints the excalidraw/clipboard payload (pipe to pbcopy, paste via the
      AX menu item per CANVAS.md Recipe 2), or the raw element array (--raw)
      for the localStorage recipe.
"""

from __future__ import annotations

import json
import re

SCENE_MODEL = "mlx-community/Qwen3-8B-4bit"

SCENE_SYSTEM = """\
You generate Excalidraw scenes as JSON. Reply with ONLY a JSON object:
{"elements": [ ... ]}
Each element needs: id (string), type (one of rectangle/arrow/text), x, y,
width, height (numbers), seed (int), version (1), versionNonce (int).
rectangle also: strokeColor "#1e1e1e", backgroundColor "transparent".
text also: text (string), fontSize 20, fontFamily 1.
arrow: position/size spanning from near its source box edge to near its
target box edge. Lay out with >=80px gaps between boxes, no overlaps,
coordinates within 0..1600 x 0..900. Prefer free-floating text elements over
bound container labels. No prose, no markdown fences."""

_REQUIRED = ("id", "type", "x", "y", "width", "height")
_DEFAULTS = {"seed": 1, "version": 1, "versionNonce": 1}


class SceneError(RuntimeError):
    pass


def generate_scene(description: str, *, model: str | None = None,
                   max_tokens: int = 4000) -> list[dict]:
    """One-shot scene generation on the routed (larger) model. Returns the
    validated element array; raises SceneError when the output can't be made
    schema-valid (callers should fall back to the Mermaid dialog recipe)."""
    from mlx_lm import load, stream_generate

    mdl, tok = load(model or SCENE_MODEL)
    messages = [{"role": "system", "content": SCENE_SYSTEM},
                {"role": "user", "content": description}]
    try:
        tokens = tok.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True,
            enable_thinking=False)
    except TypeError:
        tokens = tok.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True)

    out: list[str] = []
    depth, opened = 0, False
    for r in stream_generate(mdl, tok, prompt=tokens, max_tokens=max_tokens):
        out.append(r.text)
        for ch in r.text:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            break
    return _validate("".join(out))


def _validate(reply: str) -> list[dict]:
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if not m:
        raise SceneError("no JSON object in model reply")
    try:
        elements = json.loads(m.group(0)).get("elements")
    except json.JSONDecodeError as e:
        raise SceneError(f"JSON parse failed: {e}") from e
    if not isinstance(elements, list) or not elements:
        raise SceneError("empty or missing elements array")
    for el in elements:
        if el.get("type") == "text":
            # cosmetic for text — Excalidraw's restoreElements recomputes
            # from fontSize, so repair instead of failing the whole scene
            el.setdefault("width", 10 * len(el.get("text", "") or "x"))
            el.setdefault("height", 25)
            el.setdefault("text", "")
        missing = [k for k in _REQUIRED if k not in el]
        if missing:
            raise SceneError(f"element {el.get('id', '?')} missing {missing}")
        for k, v in _DEFAULTS.items():  # cosmetic fields: repair, don't fail
            el.setdefault(k, v)
    return elements


def clipboard_payload(elements: list[dict]) -> str:
    """The plain-text pasteboard payload Excalidraw accepts (CANVAS.md
    Recipe 2)."""
    return json.dumps({"type": "excalidraw/clipboard", "elements": elements})
