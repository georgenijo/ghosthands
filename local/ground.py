#!/usr/bin/env python3
"""Grounding probe: image + target text -> click coordinate, via a local MLX VLM.

Usage:
    ground.py <image.png> "<target description>" [--model <mlx repo or path>]

Prints the RAW model output (so we can see the action format) and a best-effort
parsed (x, y). First run is for inspecting the format; parsing is heuristic and
adapts to whatever MAI-UI / Holo / UI-TARS emit (JSON {x,y} / point_2d / box / a
click(x,y) string). Coordinates are interpreted in the input IMAGE's pixel space,
which is exactly Cua's window-local screenshot space, so the (x, y) can be fed
straight back into Cua's pixel click.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


DEFAULT_MODEL = "mlx-community/MAI-UI-8B-4bit"

# Ask for absolute image pixels explicitly — MAI-UI's coordinate convention is
# under-documented, so we pin it in the prompt and verify empirically.
PROMPT_TMPL = (
    "You are a GUI grounding model. Look at the screenshot and output the click "
    "location for the described UI element. Respond with ONLY the absolute pixel "
    "coordinate as JSON: {{\"x\": <int>, \"y\": <int>}} where x is pixels from the "
    "left edge and y is pixels from the top edge of the image.\n"
    "Target element: {target}"
)


def extract_xy(text: str):
    """Best-effort pull of an (x, y) from any of the known output shapes."""
    # 1) strict JSON {"x":..,"y":..}
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if "x" in obj and "y" in obj:
            return float(obj["x"]), float(obj["y"]), "json-xy"
        for k in ("point_2d", "point", "coordinate", "click"):
            v = obj.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                return float(v[0]), float(v[1]), f"json-{k}"
        if "bbox_2d" in obj and len(obj["bbox_2d"]) == 4:
            x1, y1, x2, y2 = obj["bbox_2d"]
            return (x1 + x2) / 2, (y1 + y2) / 2, "json-bbox-center"
    # 2) click(x, y) / (x,y) / box(x1,y1,x2,y2) string forms
    pairs = re.findall(r"\(?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)?", text)
    if pairs:
        nums = [float(n) for n in pairs[0]]
        return nums[0], nums[1], "regex-pair"
    return None, None, "unparsed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("target")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=128)
    args = ap.parse_args()

    img = Path(args.image)
    if not img.exists():
        print(f"image not found: {img}", file=sys.stderr)
        return 2

    from mlx_vlm import load, generate
    from mlx_vlm.prompt_utils import apply_chat_template

    model, processor = load(args.model)
    config = model.config if hasattr(model, "config") else processor

    prompt = PROMPT_TMPL.format(target=args.target)
    try:
        formatted = apply_chat_template(processor, config, prompt, num_images=1)
    except Exception:
        formatted = prompt  # some processors accept the raw string

    result = generate(
        model, processor, formatted, image=[str(img)],
        max_tokens=args.max_tokens, temperature=0.0, verbose=False,
    )
    raw = result.text if hasattr(result, "text") else str(result)

    x, y, how = extract_xy(raw)
    print("=== RAW MODEL OUTPUT ===")
    print(raw.strip())
    print("=== PARSED ===")
    print(json.dumps({"x": x, "y": y, "via": how}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
