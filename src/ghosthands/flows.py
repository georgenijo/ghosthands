"""Record-assert-replay — the project's "ultimate free" path (DESIGN goal).

Run a flow ONCE with a model in the loop to capture NAME-TARGETED steps (each
action stored by the element's AX id / title / role+text, never a volatile
element_index). Then REPLAY it deterministically with NO model: each step is
re-resolved against a fresh snapshot through the hardened action wrapper, so it
survives index churn and duplicate-window subtrees. Replay self-heals — if a
target no longer resolves, ONE model call picks the element again and the flow is
rewritten — but the happy path never touches a model, so reruns are free. Testing
a UI means running the same flow many times; this makes that $0.

Flow files are JSON under flows/<name>.json.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import ax, driver
from .actions import App, ElementNotFoundError

FLOWS_DIR = Path(__file__).resolve().parent.parent.parent / "flows"
SETTLE_SECONDS = 0.35


@dataclass
class Step:
    action: str                      # click | type_text | press_key
    target: dict | None = None       # {role, ax_id, title, label, text} for click
    text: str | None = None          # type_text payload
    key: str | None = None           # press_key payload
    note: str = ""                   # human label / self-heal hint


@dataclass
class Flow:
    name: str
    bundle_id: str
    steps: list[Step] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    title_contains: str | None = None

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "Flow":
        steps = [Step(**s) for s in d.get("steps", [])]
        return cls(name=d["name"], bundle_id=d["bundle_id"], steps=steps,
                   urls=d.get("urls", []), title_contains=d.get("title_contains"))

    def path(self) -> Path:
        return FLOWS_DIR / f"{self.name}.json"

    def save(self) -> Path:
        FLOWS_DIR.mkdir(parents=True, exist_ok=True)
        p = self.path()
        p.write_text(self.to_json())
        return p


def load(name: str) -> Flow:
    p = name if name.endswith(".json") else FLOWS_DIR / f"{name}.json"
    p = Path(p)
    if not p.exists():
        raise FileNotFoundError(f"no flow {name!r} at {p}")
    return Flow.from_dict(json.loads(p.read_text()))


def target_of(el: ax.Element) -> dict:
    """A name-target for an element — everything that lets us re-find it later
    without an index. ax_id is the most stable; title/label/text are fallbacks."""
    return {"role": el.role, "ax_id": el.ax_id, "title": el.title,
            "label": el.label, "text": el.text}


def matcher_for(target: dict):
    """Build an actions.Matcher from a stored name-target. Prefers an exact
    AX-id match; falls back to role + visible-text match."""
    axid = (target.get("ax_id") or "").casefold()
    want_text = (target.get("title") or target.get("label") or target.get("text") or "").casefold()
    role = target.get("role") or ""

    def match(el: ax.Element) -> bool:
        if axid and (el.ax_id or "").casefold() == axid:
            return True
        if want_text and el.text.casefold() == want_text and (not role or el.role == role):
            return True
        return False

    return match


# --- record -----------------------------------------------------------------

def record(brain, name: str, goal: str, bundle_id: str, *, urls=None,
           title_contains=None, done_check=None, max_turns: int = 12,
           log=print) -> tuple[Flow, bool]:
    """Drive the goal once with `brain`, capturing each successful action as a
    name-targeted Step. Saves and returns (flow, succeeded)."""
    from . import ownloop

    steps: list[Step] = []

    def on_step(tool: str, el, raw_args: dict) -> None:
        if tool == "click" and el is not None:
            steps.append(Step("click", target=target_of(el),
                              note=el.text or el.ax_id or el.role))
        elif tool == "type_text":
            steps.append(Step("type_text", text=raw_args.get("text"), note="type"))
        elif tool == "press_key":
            steps.append(Step("press_key", key=raw_args.get("key"), note="key"))

    ok = ownloop.run_loop(brain, goal, bundle_id, urls=urls,
                          title_contains=title_contains, done_check=done_check,
                          on_step=on_step, max_turns=max_turns, log=log)
    flow = Flow(name=name, bundle_id=bundle_id, steps=steps,
                urls=list(urls or []), title_contains=title_contains)
    flow.save()
    log(f"[record] saved {len(steps)} steps to {flow.path()} (succeeded={ok})")
    return flow, ok


# --- replay -----------------------------------------------------------------

def replay(flow: Flow, *, heal_brain=None, done_check=None, log=print) -> bool:
    """Replay a flow with NO model on the happy path. Returns True on success
    (done_check if given, else "all steps executed"). On a target that won't
    resolve, calls `heal_brain` once to re-pick the element and rewrites the
    step so the next replay is model-free again."""
    app = App.launch(flow.bundle_id, urls=flow.urls or None,
                     title_contains=flow.title_contains)
    log(f"[replay {flow.name}] pid={app.pid} window={app.window_id} "
        f"({len(flow.steps)} steps, no model)")
    healed = False

    for i, step in enumerate(flow.steps, 1):
        if step.action == "click":
            try:
                el = app.click(matcher_for(step.target or {}))
                log(f"  {i}. click {step.note!r} -> [{el.index}] {el.role}")
            except ElementNotFoundError:
                if heal_brain is None:
                    log(f"  {i}. click {step.note!r} -> NOT FOUND (no heal brain)")
                    return False
                log(f"  {i}. click {step.note!r} -> not found; self-healing (1 model call)")
                el = _heal(app, heal_brain, step, log)
                step.target = target_of(el)
                healed = True
        elif step.action == "type_text":
            app.type_text(step.text or "")
            log(f"  {i}. type_text {step.text!r}")
        elif step.action == "press_key":
            app.press_key(step.key or "")
            log(f"  {i}. press_key {step.key!r}")
        else:
            log(f"  {i}. unknown action {step.action!r} — skipped")
        time.sleep(SETTLE_SECONDS)

    if healed:
        flow.save()
        log(f"[replay {flow.name}] flow rewritten after self-heal -> {flow.path()}")

    if done_check is not None:
        ok = done_check()
        log(f"[replay {flow.name}] done_check -> {ok}")
        return ok
    return True


def _heal(app: App, brain, step: Step, log) -> ax.Element:
    """One model call to re-find a step's target on the current screen."""
    snap = app.snapshot()
    hint = step.note or json.dumps(step.target)
    decision = brain.decide(f"Click the element: {hint}", snap.markdown, [])
    for action in decision.actions:
        idx = action.get("args", {}).get("element_index")
        if idx is None:
            continue
        el = next((e for e in snap.elements if e.index == idx), None)
        if el is not None:
            driver.call("click", {"pid": app.pid, "window_id": app.window_id,
                                  "element_index": idx})
            return el
    raise ElementNotFoundError(f"self-heal failed to find {hint!r}")
