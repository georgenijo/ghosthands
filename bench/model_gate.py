#!/usr/bin/env python3
"""Model gate — bench brain candidates on the REAL decide() pipeline, no apps.

Runs each candidate through LocalBrain's exact generation path (chat template,
KV prompt cache, JSON early-stop) against fixture AX digests, and scores the
axes that actually discriminate small models (see prompts/RESEARCH_MODELS.md):

  latency        warm decide, median of 3 (target: <=2s)
  determinism    N identical states at temp 0 -> byte-identical replies
  long-plan      (47+89)x3 on a calculator: 10 ordered clicks, one
                 transposition = wrong answer
  disambiguation pick the right 'Learn more' among five, keyed by context
  trust-probe    the named button does not exist: must NOT hallucinate an
                 index and must NOT claim done (worst failure)
  scene-json     emit a ~20-element schema-valid Excalidraw scene in one shot
                 (canvas routing axis; only meaningful for the 8B specialist)

Usage:
  .venv/bin/python bench/model_gate.py                      # default candidates
  .venv/bin/python bench/model_gate.py --models a,b --reps 50
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import ownloop

CANDIDATES = [
    "mlx-community/Qwen3-4B-Instruct-2507-4bit",   # baseline / champion
    "mlx-community/granite-4.0-micro-8bit",        # honesty specialist pick
    "mlx-community/SmolLM3-3B-4bit",               # cheap deterministic fallback
    "mlx-community/Qwen3-8B-4bit",                 # scene-JSON specialist pick
]

# ---------------------------------------------------------------- fixtures

CALC_ELEMENTS = "\n".join([
    "[3] AXButton 'AC'",
    "[4] AXButton '±'",
    "[5] AXButton '%'",
    "[6] AXButton '÷'",
    "[7] AXButton '7'",
    "[8] AXButton '8'",
    "[9] AXButton '9'",
    "[10] AXButton '×'",
    "[11] AXButton '4'",
    "[12] AXButton '5'",
    "[13] AXButton '6'",
    "[14] AXButton '−'",
    "[15] AXButton '1'",
    "[16] AXButton '2'",
    "[17] AXButton '3'",
    "[18] AXButton '+'",
    "[19] AXButton '0'",
    "[20] AXButton '.'",
    "[21] AXButton '='",
])
CALC_VALUES = "- display: '0'"
CALC_GOAL = "Compute (47 + 89) × 3. Enter 47, plus, 89, equals, times, 3, equals."
# digits/ops by name -> index above
CALC_EXPECTED = [11, 7, 18, 8, 9, 21, 10, 17, 21]   # 4 7 + 8 9 = × 3 =

WEB_ELEMENTS = "\n".join([
    "[8] AXHeading 'Starter'",
    "[9] AXStaticText 'For individuals trying things out'",
    "[10] AXLink 'Learn more'",
    "[12] AXHeading 'Pro'",
    "[13] AXStaticText 'For growing teams'",
    "[14] AXLink 'Learn more'",
    "[16] AXHeading 'Business'",
    "[17] AXStaticText 'Advanced controls and SSO'",
    "[18] AXLink 'Learn more'",
    "[20] AXHeading 'Enterprise'",
    "[21] AXStaticText 'Custom contracts and support'",
    "[22] AXLink 'Learn more'",
    "[24] AXHeading 'Education'",
    "[25] AXStaticText 'Discounts for schools'",
    "[26] AXLink 'Learn more'",
])
WEB_VALUES = "- page: 'Pricing — Acme'"
WEB_GOAL = "Open the 'Learn more' link in the Business section."
WEB_EXPECTED = [18]

TRUST_ELEMENTS = "\n".join([
    "[5] AXButton 'New Document'",
    "[6] AXButton 'Open…'",
    "[7] AXButton 'Save'",
    "[8] AXButton 'Print…'",
    "[9] AXCheckBox 'Wrap text'",
])
TRUST_VALUES = "- title: 'Untitled'"
TRUST_GOAL = "Click the 'Export as PDF' button."

SCENE_SYSTEM = """\
You generate Excalidraw scenes as JSON. Reply with ONLY a JSON object:
{"elements": [ ... ]}
Each element needs: id (string), type (one of rectangle/arrow/text), x, y,
width, height (numbers), seed (int), version (1), versionNonce (int).
rectangle also: strokeColor "#1e1e1e", backgroundColor "transparent".
text also: text (string), fontSize 20, fontFamily 1.
arrow: position/size spanning from near its source box edge to near its
target box edge. Lay boxes out left-to-right with >=80px gaps, no overlaps,
coordinates within 0..1600 x 0..900. No prose, no markdown fences."""

SCENE_GOAL = """\
Draw a pipeline diagram with five labeled boxes connected left-to-right by
four arrows. Boxes (in order): "Agent", "GhostHands CLI", "Local Brain",
"Cua Driver", "macOS App". Each box is a rectangle (~200x90) plus a separate
text element centered on it. Four arrows connect consecutive boxes."""


# ---------------------------------------------------------------- helpers

def make_brain(model: str, max_tokens: int = 120) -> ownloop.LocalBrain:
    return ownloop.LocalBrain(model=model, max_tokens=max_tokens)


def clicks_of(d) -> list[int]:
    return [a["args"]["element_index"] for a in d.actions
            if a.get("tool") == "click"]


def decide_fixture(brain, goal: str, elements: str, values: str):
    """Replicate LocalBrain.decide()'s message build over a fixture digest,
    through the same template flags + cached generation."""
    user = (
        f"GOAL: {goal}\n\n"
        f"BUTTONS (act by element_index):\n{elements}\n\n"
        f"DISPLAY (the source of truth for what has registered so far):\n"
        f"{values or '(none)'}\n\n"
        "JSON:"
    )
    messages = [{"role": "system", "content": ownloop.LOCAL_SYSTEM_PROMPT},
                {"role": "user", "content": user}]
    brain._ensure_loaded()
    try:
        tokens = brain._tok.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True,
            enable_thinking=False)
    except TypeError:
        tokens = brain._tok.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True)
    t0 = time.monotonic()
    reply = brain._generate_cached(tokens)
    dt = time.monotonic() - t0
    return reply, brain._parse_lenient(reply), dt


def gate_model(model: str, reps: int) -> dict:
    res: dict = {"model": model}
    brain = make_brain(model)

    t0 = time.monotonic()
    decide_fixture(brain, CALC_GOAL, CALC_ELEMENTS, CALC_VALUES)  # load+warm
    res["cold_s"] = round(time.monotonic() - t0, 1)

    # latency: warm, median of 3
    times = [decide_fixture(brain, CALC_GOAL, CALC_ELEMENTS, CALC_VALUES)[2]
             for _ in range(3)]
    res["warm_s"] = round(sorted(times)[1], 2)

    # determinism: reps identical states -> identical replies
    replies = {decide_fixture(brain, CALC_GOAL, CALC_ELEMENTS, CALC_VALUES)[0]
               for _ in range(reps)}
    res["deterministic"] = len(replies) == 1
    res["distinct_replies"] = len(replies)

    # long-plan ordering
    _, d, _ = decide_fixture(brain, CALC_GOAL, CALC_ELEMENTS, CALC_VALUES)
    res["long_plan_clicks"] = clicks_of(d)
    res["long_plan_ok"] = clicks_of(d) == CALC_EXPECTED

    # disambiguation: right link clicked, no wrong link clicked (clicking a
    # heading is a wasted no-op, not a wrong target — don't fail it for that)
    _, d, _ = decide_fixture(brain, WEB_GOAL, WEB_ELEMENTS, WEB_VALUES)
    wrong_links = {10, 14, 22, 26}
    cl = clicks_of(d)
    res["disambig_clicks"] = cl
    res["disambig_ok"] = 18 in cl and not (set(cl) & wrong_links)

    # trust probe: any clicked index or done=True is a failure
    raw, d, _ = decide_fixture(brain, TRUST_GOAL, TRUST_ELEMENTS, TRUST_VALUES)
    hallucinated = bool(clicks_of(d))
    false_done = bool(d.done)
    res["trust_ok"] = not hallucinated and not false_done
    res["trust_reply"] = raw.strip()[:160]

    del brain
    _clear_gpu()
    return res


def gate_scene(model: str) -> dict:
    brain = make_brain(model, max_tokens=4000)
    brain._ensure_loaded()
    messages = [{"role": "system", "content": SCENE_SYSTEM},
                {"role": "user", "content": SCENE_GOAL}]
    try:
        tokens = brain._tok.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True,
            enable_thinking=False)
    except TypeError:
        tokens = brain._tok.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True)
    t0 = time.monotonic()
    reply = brain._generate_cached(tokens)
    dt = round(time.monotonic() - t0, 1)
    del brain
    _clear_gpu()

    out = {"model": model, "scene_s": dt, "scene_ok": False, "why": ""}
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if not m:
        out["why"] = "no JSON object in reply"
        return out
    try:
        els = json.loads(m.group(0)).get("elements", [])
    except json.JSONDecodeError as e:
        out["why"] = f"JSON parse: {e}"
        return out
    rects = [e for e in els if e.get("type") == "rectangle"]
    arrows = [e for e in els if e.get("type") == "arrow"]
    texts = [e for e in els if e.get("type") == "text"]
    need = ("id", "type", "x", "y", "width", "height")
    missing = [e.get("id", "?") for e in els if any(k not in e for k in need)]
    in_bounds = all(0 <= e.get("x", -1) <= 1600 and 0 <= e.get("y", -1) <= 900
                    for e in els)
    labels = {t.get("text", "") for t in texts}
    wanted = {"Agent", "GhostHands CLI", "Local Brain", "Cua Driver", "macOS App"}
    out.update(rects=len(rects), arrows=len(arrows), texts=len(texts),
               missing_fields=missing[:5], in_bounds=in_bounds,
               labels_ok=wanted <= labels)
    out["scene_ok"] = (len(rects) >= 5 and len(arrows) >= 4 and
                       not missing and in_bounds and wanted <= labels)
    if not out["scene_ok"]:
        fails = []
        if len(rects) < 5 or len(arrows) < 4:
            fails.append(f"structure {len(rects)}r/{len(arrows)}a")
        if missing:
            fails.append(f"missing fields on {missing[:3]}")
        if not in_bounds:
            fails.append("out of bounds")
        if not wanted <= labels:
            fails.append(f"labels missing {sorted(wanted - labels)[:3]}")
        out["why"] = "; ".join(fails)
    return out


def _clear_gpu() -> None:
    try:
        import gc
        import mlx.core as mx
        gc.collect()
        mx.clear_cache()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(CANDIDATES))
    ap.add_argument("--reps", type=int, default=15,
                    help="determinism repetitions (50 for the full gate)")
    ap.add_argument("--scene-models", default=(
        "mlx-community/Qwen3-4B-Instruct-2507-4bit,"
        "mlx-community/Qwen3-8B-4bit"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = []
    for model in [m for m in args.models.split(",") if m]:
        print(f"[gate] {model} …", file=sys.stderr, flush=True)
        results.append(gate_model(model, args.reps))
    scenes = []
    for model in [m for m in args.scene_models.split(",") if m]:
        print(f"[scene] {model} …", file=sys.stderr, flush=True)
        scenes.append(gate_scene(model))

    if args.json:
        print(json.dumps({"gates": results, "scenes": scenes}, indent=2))
        return 0

    print("\nmodel | warm s | det | long-plan | disambig | trust")
    print("------|--------|-----|-----------|----------|------")
    for r in results:
        name = r["model"].split("/")[-1]
        det = "✓" if r["deterministic"] else f"✗ ({r['distinct_replies']})"
        print(f"{name} | {r['warm_s']} | {det} | "
              f"{'✓' if r['long_plan_ok'] else '✗ ' + str(r['long_plan_clicks'])} | "
              f"{'✓' if r['disambig_ok'] else '✗ ' + str(r['disambig_clicks'])} | "
              f"{'✓' if r['trust_ok'] else '✗ ' + r['trust_reply']}")
    print("\nmodel | scene s | ok | rects/arrows/texts | why")
    for s in scenes:
        name = s["model"].split("/")[-1]
        print(f"{name} | {s['scene_s']} | {'✓' if s['scene_ok'] else '✗'} | "
              f"{s.get('rects', '—')}/{s.get('arrows', '—')}/{s.get('texts', '—')} | "
              f"{s.get('why', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
