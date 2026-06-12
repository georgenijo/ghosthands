#!/usr/bin/env python3
"""GhostHands benchmark harness (DESIGN.md §9, ROADMAP M5).

Compares brains on identical tasks with identical hands (Cua Driver):

  scripted-ax   GhostHands wrapper, no model        — the floor (best case)
  local         own-loop + local MLX text brain on the AX tree (default
                Qwen2.5-14B-Instruct-4bit)          — free, local
  local-7b      same loop, Qwen2.5-7B-Instruct-8bit — the originally-specced
                7B (kept to show the size/reliability trade)
  mai-ui-pixel  own-loop + local MLX VISION brain, screenshot + PIXEL click
                (MAI-UI-8B)                          — the no-AX fallback path
  claude        Claude Code (subscription) + cua-driver MCP — the ceiling
  gpt           Codex CLI (subscription) + cua-driver MCP   — optional

Methodology:
- External wall-clock: starts at goal dispatch, stops the first time the task's
  done-detector reads TRUE (a WORLD check — entity state / display value, never
  the agent's words). In-process contenders block; the harness reads the
  detector right after. Subprocess brains are polled every 2s.
- Local/scripted/vision contenders cost $0 (no tokens). The local model loads
  once per contender (reused across that contender's runs), then is evicted
  before the next contender to stay within unified memory.
- N runs per (contender × task); median + min/max spread reported.

Usage: python3 bench/run_bench.py [--runs N] [--tasks calc-7x6,ha-toggle]
                                  [--contenders scripted-ax,local,mai-ui-pixel,claude]
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from ghosthands import brains, ownloop, smoke, tasks  # noqa: E402
from ghosthands.actions import App  # noqa: E402

RUN_TIMEOUT = 300.0
POLL_SECONDS = 2.0
RESULTS_DIR = REPO / "bench" / "results"

LOCAL_MODELS = {
    "local": "mlx-community/Qwen2.5-7B-Instruct-4bit",     # the specced 7B
    "local-14b": "mlx-community/Qwen2.5-14B-Instruct-4bit",  # stronger option
}
INPROC = {"scripted-ax", "local", "local-14b", "mai-ui-pixel"}


def _noop(*_a, **_k):
    return None


# --- scripted contenders (no model) -----------------------------------------

def scripted_calc() -> None:
    smoke.calculator_7x6(log=_noop)


def scripted_ha() -> None:
    app = App.launch(tasks.BRAVE_BUNDLE, urls=[tasks.HA_DASHBOARD],
                     title_contains="GhostHands")
    app.wait_for(lambda s: bool(s.find_all("GhostHands Test")), timeout=15)
    app.click("GhostHands Test", verify=lambda s: tasks.HA_TOGGLE.done_check())


def scripted_web() -> None:
    from ghosthands import driver
    result = driver.call("launch_app", {
        "bundle_id": tasks.SAFARI_BUNDLE, "urls": ["https://example.com"],
    })
    pid = result["pid"]
    deadline = time.monotonic() + 20
    window_id = None
    while time.monotonic() < deadline and window_id is None:
        time.sleep(1)
        listed = driver.call("list_windows", {"pid": pid})
        for w in (listed["windows"] if isinstance(listed, dict) else listed) or []:
            if w.get("is_on_screen") and "Example Domain" in (w.get("title") or ""):
                window_id = w["window_id"]
                break
    if window_id is None:
        raise RuntimeError("example.com window never appeared")
    app = App(pid, window_id)
    link = lambda el: el.role == "AXLink" and "Learn more" in el.text
    app.wait_for(lambda s: bool(s.find_all(link)), timeout=15)
    app.click(link, verify=lambda s: tasks._safari_title_active(tasks._IANA_TITLE))


SCRIPTED = {"calc-7x6": scripted_calc, "ha-toggle": scripted_ha,
            "web-example-iana": scripted_web}


# --- in-process model contenders --------------------------------------------

def run_local(brain, task: tasks.Task) -> int:
    """Own-loop with a local text brain; returns the number of executed steps."""
    n = [0]
    on_step = lambda *_a: n.__setitem__(0, n[0] + 1)
    kw = dict(done_check=task.done_check, on_step=on_step, log=_noop, max_turns=10)
    if task.nature == "native":
        ownloop.run_loop(brain, task.loop_goal(), task.bundle_id, **kw)
    else:
        ownloop.run_loop(brain, task.loop_goal(), task.bundle_id,
                         urls=[task.url], title_contains=task.window_title, **kw)
    return n[0]


def run_vision(brain, task: tasks.Task) -> int:
    from ghosthands.visionloop import run_vision_loop
    n = [0]
    on_action = lambda: n.__setitem__(0, n[0] + 1)
    kw = dict(done_check=task.done_check, on_action=on_action, log=_noop, max_turns=10)
    if task.nature == "native":
        run_vision_loop(brain, task.loop_goal(), task.bundle_id, **kw)
    else:
        run_vision_loop(brain, task.loop_goal(), task.bundle_id,
                        urls=[task.url], title_contains=task.window_title, **kw)
    return n[0]


def make_brain(contender: str):
    if contender in LOCAL_MODELS:
        return ownloop.LocalBrain(LOCAL_MODELS[contender])
    if contender == "mai-ui-pixel":
        from ghosthands.visionloop import VisionBrain
        return VisionBrain()
    return None


def evict(brain) -> None:
    """Drop a local model and free unified memory before the next contender."""
    if brain is None:
        return
    brain._model = None
    brain._tok = getattr(brain, "_tok", None) and None
    if hasattr(brain, "_processor"):
        brain._processor = None
    gc.collect()
    try:
        import mlx.core as mx
        mx.clear_cache()
    except Exception:
        pass


# --- measurement core --------------------------------------------------------

def run_once(contender: str, task: tasks.Task, brain, workdir: str) -> dict:
    task.setup()
    if task.done_check():
        raise RuntimeError(f"{task.name}: done-detector already true after setup")

    rec: dict = {"contender": contender, "task": task.name, "success": False,
                 "seconds": None, "steps": None, "cost": None, "error": None}
    t0 = time.monotonic()

    if contender in INPROC:
        try:
            if contender == "scripted-ax":
                SCRIPTED[task.name]()
            elif contender in LOCAL_MODELS:
                rec["steps"] = run_local(brain, task)
            elif contender == "mai-ui-pixel":
                rec["steps"] = run_vision(brain, task)
            rec["cost"] = "$0"
        except Exception as e:  # noqa: BLE001 - a failed run is data, not a crash
            rec["error"] = str(e)[:300]
        # confirm via the WORLD detector (poll briefly for async settle)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if task.done_check():
                rec["success"] = True
                rec["seconds"] = round(time.monotonic() - t0, 1)
                break
            time.sleep(1)
        return rec

    # subprocess brains (claude / gpt) — full-auto, polled done-detector
    brain_spec = brains.select(contender)
    argv, _ = brain_spec.command(task.goal, brains.skill_text())
    if contender == "claude":
        argv += ["--output-format", "stream-json", "--verbose"]
    out_path = Path(workdir) / f"{contender}-{task.name}-{int(t0)}.log"
    with open(out_path, "w") as out_file:
        proc = subprocess.Popen(argv, cwd=workdir, stdin=subprocess.DEVNULL,
                                stdout=out_file, stderr=subprocess.STDOUT, text=True)
        done_at = None
        while proc.poll() is None:
            if done_at is None and task.done_check():
                done_at = time.monotonic()
            if time.monotonic() - t0 > RUN_TIMEOUT:
                proc.kill()
                rec["error"] = f"timeout {RUN_TIMEOUT}s"
                break
            time.sleep(POLL_SECONDS)
        proc.wait(timeout=30)
    if done_at is None and task.done_check():
        done_at = time.monotonic()

    output = out_path.read_text()
    rec["success"] = done_at is not None
    rec["seconds"] = round(done_at - t0, 1) if done_at else None
    rec["steps"] = count_steps(contender, output)
    rec["cost"] = extract_cost(contender, output)
    rec["transcript"] = str(out_path)
    return rec


def count_steps(contender: str, output: str) -> int | None:
    if contender == "claude":
        return output.count('"type":"tool_use"') or None
    if contender == "gpt":
        return len(re.findall(r"mcp: cua-driver/\S+ started", output)) or None
    return None


def extract_cost(contender: str, output: str) -> str | None:
    if contender == "claude":
        m = re.search(r'"total_cost_usd":\s*([0-9.]+)', output)
        return f"${float(m.group(1)):.2f}" if m else None
    if contender == "gpt":
        m = re.search(r"tokens used\s*\n?\s*([\d,]+)", output)
        return f"{m.group(1)} tok" if m else None
    return None


# --- reporting ----------------------------------------------------------------

def summarize(records: list[dict]) -> str:
    rows = []
    keys = sorted({(r["contender"], r["task"]) for r in records})
    for contender, task in keys:
        rs = [r for r in records if r["contender"] == contender and r["task"] == task]
        times = [r["seconds"] for r in rs if r["success"] and r["seconds"] is not None]
        steps = [r["steps"] for r in rs if r["steps"]]
        costs = [r["cost"] for r in rs if r["cost"]]
        rows.append({
            "contender": contender, "task": task, "runs": len(rs),
            "success": f"{100 * sum(r['success'] for r in rs) // len(rs)}%",
            "median_s": round(statistics.median(times), 1) if times else "—",
            "spread_s": f"{min(times):.0f}–{max(times):.0f}" if times else "—",
            "median_steps": int(statistics.median(steps)) if steps else "—",
            "cost": costs[len(costs) // 2] if costs else "—",
        })
    header = f"| {'contender':<12} | {'task':<16} | n | success | median s | spread | steps | cost |"
    sep = "|" + "-" * (len(header) - 2) + "|"
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"| {r['contender']:<12} | {r['task']:<16} | {r['runs']} | {r['success']:>7} "
            f"| {r['median_s']!s:>8} | {r['spread_s']:>9} | {r['median_steps']!s:>5} | {r['cost']!s} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--tasks", default="calc-7x6,ha-toggle")
    ap.add_argument("--contenders", default="scripted-ax,local,mai-ui-pixel,claude")
    args = ap.parse_args()

    suite = [tasks.ALL_TASKS[t] for t in args.tasks.split(",")]
    contenders = args.contenders.split(",")
    workdir = tempfile.mkdtemp(prefix="ghosthands-bench-")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    # Contender-outer so each local model loads once, then is evicted.
    for contender in contenders:
        brain = make_brain(contender)
        for task in suite:
            for i in range(args.runs):
                label = f"[{contender} × {task.name} {i + 1}/{args.runs}]"
                print(f"{label} running…", flush=True)
                try:
                    rec = run_once(contender, task, brain, workdir)
                except Exception as e:  # noqa: BLE001
                    rec = {"contender": contender, "task": task.name, "success": False,
                           "seconds": None, "steps": None, "cost": None,
                           "error": f"harness: {e}"[:300]}
                records.append(rec)
                print(f"{label} success={rec['success']} t={rec['seconds']}s "
                      f"steps={rec['steps']} err={rec['error']}", flush=True)
                (RESULTS_DIR / "latest.json").write_text(json.dumps(records, indent=2))
        evict(brain)

    print()
    print(summarize(records))
    print(f"\nraw: {RESULTS_DIR / 'latest.json'}  transcripts: {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
