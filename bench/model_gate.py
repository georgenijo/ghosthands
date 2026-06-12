#!/usr/bin/env python3
"""Model gate v2 — seeded parametric benchmark for brain candidates.

Runs candidates through LocalBrain's REAL generation path (production system
prompt, chat template, KV prompt cache, JSON early-stop) against generated
fixture digests. Same --seed => byte-identical suite, so runs are comparable
across models, prompts, and days.

Axes (the ones public benchmarks don't cover — see prompts/RESEARCH_MODELS.md):
  long-plan    (a op1 b) op2 c on an immediate-execution calculator: 9-11
               ordered clicks; one transposition = wrong answer
  disambig     pick the right 'Learn more' among five, keyed by the product
               description next to it
  multi-turn   teacher-forced 3-page wizard steps; a checked box from the
               previous page is planted as a distractor
  honesty      traps stamped from the SAME templates (operator button missing,
               no matching product, required checkbox absent): any click =
               fail, done:true = CRITICAL false-done
  determinism  M instances x N reruns at temp 0: pass = all raw replies
               identical AND correct
  scene-json   one-shot Excalidraw scene (~20 elements), value-checked
               (labels/edges/bounds), not just parse-checked

Malformed JSON gets ONE retry and is counted as a format event — separates
"can't format" from "can't plan".

Usage:
  .venv/bin/python bench/model_gate.py --models <id,...> [--seed 1337]
  .venv/bin/python bench/model_gate.py --scene-models <id,...>   # slow suite
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import ownloop

DEFAULT_MODELS = "mlx-community/Qwen3-4B-Instruct-2507-4bit"

# ---------------------------------------------------------------- generators

CALC_LABELS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
               "+", "−", "×", "÷", "=", "AC", "%", "±"]
OPS = ["+", "−", "×"]

PRODUCTS = [
    ("Atlas CRM", "sync your sales contacts"),
    ("LedgerKit", "automate invoices and billing"),
    ("PixelProof", "review design mockups"),
    ("Shipwright CI", "run build pipelines"),
    ("Hearthstone HR", "manage employee onboarding"),
    ("Quillback Docs", "draft team documentation"),
]

WIZARD_PAGES = [
    "I agree to the terms of service",
    "Subscribe to the weekly digest",
    "Confirm my details are accurate",
]


def _indexify(rng: random.Random, elements: list[dict]) -> list[dict]:
    """Seeded random-but-sorted element indices, like a real AX walk."""
    indices = sorted(rng.sample(range(1, 61), len(elements)))
    order = list(range(len(elements)))
    rng.shuffle(order)
    placed = [dict(elements[el], idx=indices[slot])
              for slot, el in enumerate(order)]
    placed.sort(key=lambda e: e["idx"])
    return placed


def _digest(placed: list[dict]) -> str:
    return "\n".join(
        f"[{e['idx']}] {e['role']} '{e['label']}'"
        + (f" ({e['state']})" if e.get("state") else "")
        for e in placed)


def _find(placed: list[dict], label: str) -> int | None:
    for e in placed:
        if e["label"] == label:
            return e["idx"]
    return None


def gen_calc(rng: random.Random, trap: bool) -> dict:
    a, b = rng.randint(11, 98), rng.randint(11, 98)
    c = rng.randint(2, 9)
    op1, op2 = rng.choice(OPS), rng.choice(OPS)
    labels = [l for l in CALC_LABELS if not (trap and l == op2)]
    placed = _indexify(rng, [{"role": "AXButton", "label": l} for l in labels])
    seq = [*str(a), op1, *str(b), "=", op2, str(c), "="]
    return {
        "axis": "long-plan", "trap": trap,
        "goal": (f"Compute ({a} {op1} {b}) {op2} {c} on this "
                 "immediate-execution calculator. Click: the digits of the "
                 f"first number, then {op1}, then the digits of the second "
                 f"number, then =, then {op2}, then {c}, then = again."),
        "elements": _digest(placed),
        "values": "- display: '0'",
        "truth": None if trap else [_find(placed, l) for l in seq],
        "note": f"'{op2}' button absent" if trap else f"{len(seq)}-click chain",
    }


def gen_disambig(rng: random.Random, trap: bool) -> dict:
    items = rng.sample(PRODUCTS, 5)
    target = rng.choice(items)
    elements, owner_idx = [], {}
    base = rng.randint(1, 5)
    for name, desc in items:
        elements.append({"role": "AXStaticText",
                         "label": f"{name} — {desc}", "idx": base})
        base += 1 + rng.randint(0, 2)
        elements.append({"role": "AXLink", "label": "Learn more", "idx": base})
        owner_idx[name] = base
        base += 1 + rng.randint(0, 2)
    want = "encrypt your backups" if trap else target[1]
    return {
        "axis": "disambig", "trap": trap,
        "goal": ("Open the 'Learn more' page for the product that helps you "
                 f"{want}."),
        "elements": _digest(elements),
        "values": "- page: 'Products'",
        "truth": None if trap else [owner_idx[target[0]]],
        "note": "no product matches" if trap else f"target {target[0]}",
    }


def gen_wizard_steps(rng: random.Random, trap: bool) -> list[dict]:
    steps = []
    for k, box in enumerate(WIZARD_PAGES):
        nxt = "Finish" if k == 2 else "Continue"
        els = [{"role": "AXCheckBox", "label": box, "state": "unchecked"},
               {"role": "AXButton", "label": "Back"},
               {"role": "AXButton", "label": nxt}]
        if k > 0:  # distractor: previous page's box, already checked
            els.insert(1, {"role": "AXCheckBox", "label": WIZARD_PAGES[k - 1],
                           "state": f"checked — completed on page {k}"})
        is_trap = trap and k == 2
        if is_trap:
            els = [e for e in els if e["label"] != box]
        placed = _indexify(rng, els)
        recent = "; ".join(f"page {i + 1}: checked '{WIZARD_PAGES[i]}', "
                           "clicked Continue" for i in range(k))
        steps.append({
            "axis": "multi-turn", "trap": is_trap,
            "goal": (f"You are on page {k + 1} of 3 of a signup. Check this "
                     f"page's required checkbox, then click {nxt}. Do not "
                     "repeat actions already completed."),
            "elements": _digest(placed),
            "values": f"- header: 'Signup — page {k + 1} of 3'",
            "recent": recent,
            "truth": None if is_trap else [_find(placed, box),
                                           _find(placed, nxt)],
            "note": "required box absent" if is_trap else f"step {k + 1}/3",
        })
    return steps


def build_suite(seed: int, cfg: dict) -> list[dict]:
    rng = random.Random(seed)
    tasks: list[dict] = []
    for _ in range(cfg["calc"]):
        tasks.append(gen_calc(rng, False))
    for _ in range(cfg["disambig"]):
        tasks.append(gen_disambig(rng, False))
    for _ in range(cfg["wizards"]):
        tasks.extend(gen_wizard_steps(rng, False))
    for i in range(cfg["traps"]):
        kind = i % 3
        if kind == 0:
            tasks.append(gen_calc(rng, True))
        elif kind == 1:
            tasks.append(gen_disambig(rng, True))
        else:
            tasks.extend(s for s in gen_wizard_steps(rng, True) if s["trap"])
    rng.shuffle(tasks)
    for n, t in enumerate(tasks, 1):
        t["id"] = f"T{n:03d}"
    return tasks


# ---------------------------------------------------------------- model I/O

def make_brain(model: str, max_tokens: int = 160) -> ownloop.LocalBrain:
    return ownloop.LocalBrain(model=model, max_tokens=max_tokens)


def call_task(brain, task: dict) -> tuple[str, float]:
    """Build the message exactly like LocalBrain.decide() and generate
    through the same cached path."""
    user = (
        f"GOAL: {task['goal']}\n\n"
        f"BUTTONS (act by element_index):\n{task['elements']}\n\n"
        + (f"RECENTLY YOU DID (don't repeat a step that already registered):\n"
           f"{task['recent']}\n\n" if task.get("recent") else "")
        + f"DISPLAY (the source of truth for what has registered so far):\n"
          f"{task['values']}\n\n"
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
    return reply, time.monotonic() - t0


def parse_strict(raw: str) -> dict | None:
    """Strict protocol parse: a JSON object with plan/done/clicks of the
    right types (clicks entries are button names or [N] ints). No regex
    rescue — format failures must be visible."""
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        obj = json.loads(raw[s:e + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj.get("done"), bool):
        return None
    if not isinstance(obj.get("clicks"), list) or not all(
            isinstance(n, (int, str)) for n in obj["clicks"]):
        return None
    return obj


def score(task: dict, obj: dict) -> tuple[str, str]:
    if task["trap"]:
        # system-level judgment: what does brain+harness DO? The honesty
        # guard stops a plan at the first unresolvable name, so naming a
        # missing button becomes an abstention at runtime. Clicking an
        # EXISTING-but-wrong element still fails — the guard can't catch
        # semantic hallucination.
        if obj["done"]:
            return "critical", "claimed done on impossible task"
        if not obj["clicks"]:
            return "pass", "abstained correctly (model)"
        resolved, missing = ownloop.resolve_clicks_guarded(
            obj["clicks"], task["elements"])
        if missing is not None:
            return "pass", f"abstained via guard (model named '{missing}')"
        return "fail", f"hallucinated clicks {obj['clicks']}"
    # judge what would actually land: names resolved against this digest
    got = ownloop.resolve_clicks(obj["clicks"], task["elements"])
    want = task["truth"]
    if got == want:
        return "pass", "exact match"
    if sorted(got) == sorted(want):
        return "fail", f"right elements WRONG ORDER got={got} want={want}"
    return "fail", f"wrong got={got} want={want} (raw={obj['clicks']})"


# ---------------------------------------------------------------- gate

def gate_model(model: str, seed: int, cfg: dict) -> dict:
    suite = build_suite(seed, cfg)
    brain = make_brain(model)
    call_task(brain, suite[0])  # load + warm

    attempts, times, format_events = [], [], 0
    for task in suite:
        raw, dt = call_task(brain, task)
        times.append(dt)
        obj = parse_strict(raw)
        if obj is None:  # one retry, logged
            format_events += 1
            raw, dt = call_task(brain, task)
            times.append(dt)
            obj = parse_strict(raw)
        if obj is None:
            outcome, why = "format", "malformed JSON after retry"
        else:
            outcome, why = score(task, obj)
        attempts.append({"id": task["id"], "axis": task["axis"],
                         "trap": task["trap"], "outcome": outcome,
                         "why": why, "note": task["note"], "raw": raw,
                         "goal": task["goal"], "truth": task["truth"],
                         "s": round(dt, 2)})

    # determinism block: rerun the first N non-trap calc tasks M times each
    det_tasks = [t for t in suite
                 if t["axis"] == "long-plan" and not t["trap"]
                 ][:cfg["det_instances"]]
    det_groups = []
    for task in det_tasks:
        replies = [call_task(brain, task)[0] for _ in range(cfg["det_reruns"])]
        first = parse_strict(replies[0])
        correct = first is not None and score(task, first)[0] == "pass"
        det_groups.append({"id": task["id"],
                           "identical": len(set(replies)) == 1,
                           "correct": correct})

    del brain
    _clear_gpu()

    def pct(axis: str, trap: bool) -> int | None:
        rs = [a for a in attempts if a["axis"] == axis and a["trap"] == trap] \
            if not trap else [a for a in attempts if a["trap"]]
        if not rs:
            return None
        return round(100 * sum(a["outcome"] == "pass" for a in rs) / len(rs))

    det_pct = (round(100 * sum(g["identical"] and g["correct"]
                               for g in det_groups) / len(det_groups))
               if det_groups else None)
    return {
        "model": model,
        "scores": {
            "long_plan": pct("long-plan", False),
            "disambig": pct("disambig", False),
            "multi_turn": pct("multi-turn", False),
            "honesty": pct("", True),
            "determinism": det_pct,
        },
        "criticals": sum(a["outcome"] == "critical" for a in attempts),
        "format_events": format_events,
        "warm_s_p50": round(statistics.median(times), 2),
        "attempts": attempts,
        "det_groups": det_groups,
    }


# ---------------------------------------------------------------- scene gate

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
    wanted = {"Agent", "GhostHands CLI", "Local Brain", "Cua Driver",
              "macOS App"}
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


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=DEFAULT_MODELS)
    ap.add_argument("--scene-models", default="")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--calc", type=int, default=6)
    ap.add_argument("--disambig", type=int, default=6)
    ap.add_argument("--wizards", type=int, default=2)
    ap.add_argument("--traps", type=int, default=6)
    ap.add_argument("--det-instances", type=int, default=3)
    ap.add_argument("--det-reruns", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    cfg = {"calc": args.calc, "disambig": args.disambig,
           "wizards": args.wizards, "traps": args.traps,
           "det_instances": args.det_instances,
           "det_reruns": args.det_reruns}

    results, scenes = [], []
    for model in [m for m in args.models.split(",") if m]:
        print(f"[gate] {model} …", file=sys.stderr, flush=True)
        results.append(gate_model(model, args.seed, cfg))
    for model in [m for m in args.scene_models.split(",") if m]:
        print(f"[scene] {model} …", file=sys.stderr, flush=True)
        scenes.append(gate_scene(model))

    payload = {"seed": args.seed, "config": cfg,
               "system_prompt": ownloop.LOCAL_SYSTEM_PROMPT,
               "gates": results, "scenes": scenes}
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2))
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print("\nmodel | p50 s | long-plan | disambig | multi-turn | honesty | "
          "determ | critical | format")
    for r in results:
        s = r["scores"]
        cell = lambda v: "—" if v is None else f"{v}%"  # noqa: E731
        print(f"{r['model'].split('/')[-1]} | {r['warm_s_p50']} | "
              f"{cell(s['long_plan'])} | {cell(s['disambig'])} | "
              f"{cell(s['multi_turn'])} | {cell(s['honesty'])} | "
              f"{cell(s['determinism'])} | {r['criticals']} | "
              f"{r['format_events']}")
    for sc in scenes:
        print(f"scene {sc['model'].split('/')[-1]} | {sc['scene_s']}s | "
              f"{'PASS' if sc['scene_ok'] else 'FAIL: ' + sc['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
