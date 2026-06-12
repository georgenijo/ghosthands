"""Track B — standalone own-loop with a swappable API brain (DESIGN.md §6B).

The loop:
    state   = snapshot (AX tree markdown with element indices)
    actions = brain.decide(goal, state, history)     # one model call
    execute each action through the hardened wrapper
    stop when the brain says done (or max_turns)

Brains implement `decide(goal, state, history) -> Decision`. Two API adapters
ship here (pay-per-call tokens — NOT subscription-covered, see DESIGN.md §3):
- AnthropicAPIBrain — Claude messages API over raw HTTP. This project is
  stdlib-only by design (no third-party packages, see README), hence raw
  urllib instead of the official `anthropic` SDK.
- OpenAIAPIBrain — OpenAI chat completions, same protocol.
Plus MockBrain, a scripted brain that validates the loop machinery with no
API key and no network.

The wire protocol is model-agnostic JSON. The brain must reply with ONLY:
    {"done": bool, "reason": str, "actions": [{"tool": str, "args": {...}}]}
Allowed tools: click, type_text, press_key, hotkey, set_value, scroll —
args take element_index/text/key/keys; pid and window_id are injected by the
loop. The brain acts only on the CURRENT snapshot's element indices.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from . import ax, driver
from .actions import App
from .driver import DriverError, StaleIndexError, TransientDriverError

MAX_TURNS = 20
ALLOWED_TOOLS = {"click", "type_text", "press_key", "hotkey", "set_value", "scroll"}
# Let the UI repaint after an action before the next snapshot, or the brain
# reads a stale screen (e.g. presses a digit twice because the display had not
# updated yet). The App wrapper settles per-action; this raw loop must too.
SETTLE_SECONDS = 0.2
SETTLE_MAX_SECONDS = 1.6  # poll for a changed snapshot up to this long

SYSTEM_PROMPT = """\
You drive a macOS app through accessibility (AX) actions. Each turn you get
the goal and the CURRENT window state: a markdown AX tree where actionable
elements carry [element_index N] tags (rendered as [N] at line start).

Reply with ONLY a JSON object, no prose, matching:
{"done": <bool>, "reason": "<short>", "actions": [{"tool": "<name>", "args": {...}}]}

Rules:
- tools: click (args: element_index), type_text (args: text), press_key
  (args: key), hotkey (args: keys), set_value (args: element_index, value),
  scroll (args: direction).
- element_index values are ONLY valid for the snapshot you were just shown;
  they change every turn. Never reuse an index from an earlier turn.
- Issue few actions per turn (1-3); you will see the new state next turn.
- Set done=true with no actions once the goal is verifiably reached in the
  shown state. Never claim done for actions whose effect you have not seen.
"""


@dataclass
class Decision:
    done: bool
    reason: str
    actions: list[dict]


class Brain:
    name = "base"

    def decide(self, goal: str, state: str, history: list[dict]) -> Decision:
        raise NotImplementedError

    @staticmethod
    def _parse(reply: str) -> Decision:
        start, end = reply.find("{"), reply.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"brain reply is not JSON: {reply[:200]!r}")
        data = json.loads(reply[start:end + 1])
        return Decision(
            done=bool(data.get("done")),
            reason=str(data.get("reason", "")),
            actions=list(data.get("actions", [])),
        )


def _post_json(url: str, headers: dict, payload: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url}: HTTP {e.code}: {e.read()[:500]!r}")


class AnthropicAPIBrain(Brain):
    """Claude over the messages API (raw HTTP — stdlib-only project)."""

    name = "claude-api"

    def __init__(self, model: str = "claude-opus-4-8"):
        self.model = model
        self.api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set (Track B is pay-per-call)")

    def decide(self, goal: str, state: str, history: list[dict]) -> Decision:
        messages = [*history, {"role": "user", "content": f"GOAL: {goal}\n\nCURRENT STATE:\n{state}"}]
        body = _post_json(
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": self.model,
                "max_tokens": 2048,
                "system": SYSTEM_PROMPT,
                "messages": messages,
            },
        )
        if body.get("stop_reason") == "refusal":
            raise RuntimeError("model refused the request")
        text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        return self._parse(text)


class OpenAIAPIBrain(Brain):
    """GPT over chat completions (raw HTTP — stdlib-only project)."""

    name = "gpt-api"

    def __init__(self, model: str = "gpt-5.1"):
        self.model = model
        self.api_key = os.environ.get("OPENAI_API_KEY") or ""
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set (Track B is pay-per-call)")

    def decide(self, goal: str, state: str, history: list[dict]) -> Decision:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": f"GOAL: {goal}\n\nCURRENT STATE:\n{state}"},
        ]
        body = _post_json(
            "https://api.openai.com/v1/chat/completions",
            {"Authorization": f"Bearer {self.api_key}"},
            {"model": self.model, "messages": messages},
        )
        return self._parse(body["choices"][0]["message"]["content"])


@dataclass
class MockBrain(Brain):
    """Scripted brain: validates the loop with no API key. `script` maps a
    turn to a function of the state markdown returning a Decision — it must
    resolve element indices from the CURRENT state, exactly like a real brain."""

    script: list[Callable[[str], Decision]] = field(default_factory=list)
    name: str = "mock"
    turn: int = 0

    def decide(self, goal: str, state: str, history: list[dict]) -> Decision:
        if self.turn >= len(self.script):
            return Decision(done=True, reason="script exhausted", actions=[])
        decision = self.script[self.turn](state)
        self.turn += 1
        return decision


LOCAL_SYSTEM_PROMPT = """\
You operate a macOS app by clicking buttons to reach the GOAL.

Each turn you are given:
- DISPLAY: the app's current on-screen value(s). This is the SOURCE OF TRUTH.
- BUTTONS: a numbered list; act by the [N] element_index.

PLAN AHEAD: output the FULL ordered sequence of clicks you can determine from
the CURRENT screen to reach the goal — usually several actions, not one. After
they run you'll see the new screen and can finish or continue. First write ONE
short sentence describing the plan, then output ONLY a JSON object as the LAST
line, where "actions" is the ordered list:
{"done": <bool>, "reason": "<sentence>", "actions": [{"tool": "click", "args": {"element_index": <N>}}, ...]}

Rules:
- Pick each [N] by matching the button's name in THIS turn's list; never invent
  or copy an index.
- Click on-screen buttons. Use press_key only for real keys ("return",
  "escape", "delete") — never for digits/operators (no key named "multiply";
  click the "×" button).
- Set done=true with empty actions ONLY when the DISPLAY already shows the final
  goal state.

For "compute A op B" (op is + - × ÷) from a cleared display, the full sequence
is: the digits of A, then the operator op, then the digits of B, then "=".
Example "compute 7 × 6": click 7, then ×, then 6, then = (four actions). The
answer appears only after "=". Use ONLY the digits in the GOAL.
"""


# Menu-bar subtrees explode the element list (Recent Items, every open app, …)
# and a backgrounded app's menu items are disabled no-ops anyway (SKILL.md §6).
# Drop them so the brain sees only in-window controls.
_MENU_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"}


def actionable_digest(state_markdown: str, *, max_elements: int = 80) -> tuple[str, str]:
    """Compress a raw AX-tree snapshot into the two things a text brain needs:
    a deduped list of actionable [N] elements (menus excluded), and the current
    value nodes (display text, field contents) used to judge progress. Keeps the
    prompt small enough for a 7B local model to choose reliably."""
    els = ax.parse_tree(state_markdown)
    seen: set[tuple] = set()
    lines: list[str] = []
    for el in els:
        if el.index is None or el.role in _MENU_ROLES:
            continue
        key = (el.role, el.text, el.ax_id)
        if key in seen:
            continue  # collapse duplicate-subtree twins; lowest index wins
        seen.add(key)
        name = el.text or el.ax_id or el.role
        idtag = f" id={el.ax_id}" if el.ax_id else ""
        lines.append(f"[{el.index}] {el.role} {name!r}{idtag}")
        if len(lines) >= max_elements:
            break
    values: list[str] = []
    seen_vals: set[str] = set()
    for el in els:
        if el.index is None and el.value not in (None, "") and el.role not in _MENU_ROLES:
            label = el.label or el.title or el.role
            line = f"- {label}: {el.value!r}"
            if line not in seen_vals:
                seen_vals.add(line)
                values.append(line)
    return "\n".join(lines), "\n".join(values[:10])


class LocalBrain(Brain):
    """Free, local AX-text brain: a small MLX text model picks the next
    element_index from the actionable elements in the current AX snapshot.
    No vision, no API, no network — $0 per run. This is the project's core
    "free brain" on the reliable AX path (DESIGN §3 cost rule, local variant).

    Requires `mlx_lm` (in the project .venv) and a cached MLX text model;
    the import is lazy so the rest of the package stays stdlib-only."""

    name = "local"
    DEFAULT_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"

    def __init__(self, model: str | None = None, *, max_tokens: int = 256):
        self.model_id = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._model = None
        self._tok = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from mlx_lm import load  # lazy: keeps the package stdlib-only
            self._model, self._tok = load(self.model_id)

    def decide(self, goal: str, state: str, history: list[dict]) -> Decision:
        self._ensure_loaded()
        from mlx_lm import generate

        elements, values = actionable_digest(state)
        recent = _recent_actions(history)
        user = (
            f"GOAL: {goal}\n\n"
            f"DISPLAY (read this FIRST to decide the next step):\n"
            f"{values or '(none)'}\n\n"
            + (f"RECENTLY YOU DID (don't repeat a step that already registered):\n{recent}\n\n" if recent else "")
            + f"BUTTONS (act by element_index):\n{elements}\n\n"
            "One sentence describing the plan, then the JSON with the full "
            "ordered list of clicks from the current screen."
        )
        prompt = self._tok.apply_chat_template(
            [{"role": "system", "content": LOCAL_SYSTEM_PROMPT},
             {"role": "user", "content": user}],
            add_generation_prompt=True, tokenize=False,
        )
        reply = generate(self._model, self._tok, prompt=prompt,
                         max_tokens=self.max_tokens, verbose=False)
        return self._parse_lenient(reply)

    @staticmethod
    def _parse_lenient(reply: str) -> Decision:
        """Local models occasionally emit Python-style dicts or trailing prose.
        Try strict JSON first, then fall back to regex; a totally unparseable
        reply becomes a safe no-op so the loop re-decides next turn."""
        try:
            return Brain._parse(reply)
        except (ValueError, json.JSONDecodeError):
            pass
        idx = re.search(r'element_index["\']?\s*[:=]\s*(\d+)', reply)
        done = re.search(r'done["\']?\s*[:=]\s*true', reply, re.IGNORECASE) is not None
        if idx:
            return Decision(done=False, reason="parsed (regex)",
                            actions=[{"tool": "click", "args": {"element_index": int(idx.group(1))}}])
        return Decision(done=done, reason="unparseable reply — re-deciding", actions=[])


def _recent_actions(history: list[dict], *, limit: int = 4) -> str:
    """A compact log of the brain's own recent decisions, pulled from the
    assistant turns in `history` (full states are too big for a local model)."""
    out: list[str] = []
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        try:
            d = json.loads(msg["content"])
        except (ValueError, KeyError, TypeError):
            continue
        acts = ", ".join(
            f"{a.get('tool')}({a.get('args', {})})" for a in d.get("actions", [])
        ) or "(none)"
        out.append(f"- {d.get('reason', '')}: {acts}")
    return "\n".join(out[-limit:])


def _settle_until_stable(app: App) -> None:
    """Poll the snapshot until it STOPS changing (two consecutive identical
    trees), so the brain's next turn sees a fully-settled screen. Waiting only
    for *a* change is not enough: an action can flip one node (e.g. a calculator
    digit press flips the Clear/AllClear button label) a beat before the value
    node it actually cares about (the display) catches up — reading in that gap
    makes the brain repeat the action. Bounded by SETTLE_MAX_SECONDS."""
    time.sleep(SETTLE_SECONDS)
    prev = None
    deadline = time.monotonic() + SETTLE_MAX_SECONDS
    while time.monotonic() < deadline:
        try:
            cur = app.snapshot().markdown
        except driver.DriverError:
            return
        if prev is not None and cur == prev:
            return
        prev = cur
        time.sleep(SETTLE_SECONDS)


def run_loop(brain: Brain, goal: str, bundle_id: str, *, max_turns: int = MAX_TURNS,
             urls: list[str] | None = None, title_contains: str | None = None,
             done_check=None, on_step=None, log=print) -> bool:
    """Drive `bundle_id` toward `goal` with `brain`. Returns True when the goal
    is reached (the brain's done flag, or `done_check` if supplied).
    `urls`/`title_contains` enable browser tasks: open the page in the
    background and bind to the right tab's window. `done_check` is an optional
    world predicate (e.g. an API state check) — when it reads true the loop
    stops immediately. It's the reliable stop signal for surfaces whose state
    isn't legible in the AX tree (a toggle that only changes an icon colour),
    where the brain otherwise can't tell it already succeeded."""
    app = App.launch(bundle_id, urls=urls, title_contains=title_contains)
    log(f"[{brain.name}] pid={app.pid} window={app.window_id} goal={goal!r}")
    history: list[dict] = []

    for turn in range(1, max_turns + 1):
        if done_check is not None and done_check():
            log(f"[{brain.name}] world goal reached")
            return True
        snap = app.snapshot()
        state = snap.markdown
        decision = brain.decide(goal, state, history)
        log(f"[{brain.name}] turn {turn}: done={decision.done} {decision.reason!r} "
            f"({len(decision.actions)} actions)")

        acted = False
        for action in decision.actions:
            tool = action.get("tool", "")
            if tool not in ALLOWED_TOOLS:
                log(f"  ! refused tool {tool!r}")
                continue
            raw_args = action.get("args", {})
            args = {**raw_args, "pid": app.pid, "window_id": app.window_id}
            try:
                driver.call(tool, args)
                acted = True
                log(f"  ✓ {tool} {raw_args}")
                if on_step is not None:
                    el = None
                    if "element_index" in raw_args:
                        el = next((e for e in snap.elements
                                   if e.index == raw_args["element_index"]), None)
                    on_step(tool, el, raw_args)
            except StaleIndexError:
                log(f"  ! stale index on {tool} — turn ends, brain re-decides on fresh state")
                break
            except TransientDriverError as e:
                log(f"  ~ transient on {tool} ({e}); continuing — next snapshot shows truth")
            except DriverError as e:
                # A bad action from the brain (unknown key, wrong element) must
                # not kill the loop — log it and let the next turn re-decide on
                # a fresh snapshot.
                log(f"  ! {tool} rejected ({e}); turn ends, brain re-decides")
                break

        if acted:
            _settle_until_stable(app)

        # Conversation history: the state we showed and the brain's reply.
        # Keep only the last 4 turns to bound the prompt.
        history.append({"role": "user", "content": f"GOAL: {goal}\n\nCURRENT STATE:\n{state}"})
        history.append({"role": "assistant", "content": json.dumps(decision.__dict__)})
        history = history[-8:]

        if done_check is not None and done_check():
            log(f"[{brain.name}] world goal reached")
            return True
        if decision.done:
            return bool(done_check()) if done_check is not None else True
    log(f"[{brain.name}] gave up after {max_turns} turns")
    return False


BRAINS = {"local": LocalBrain, "claude-api": AnthropicAPIBrain, "gpt-api": OpenAIAPIBrain}
