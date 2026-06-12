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
from .driver import StaleIndexError, TransientDriverError

MAX_TURNS = 20
ALLOWED_TOOLS = {"click", "type_text", "press_key", "hotkey", "set_value", "scroll"}
# Let the UI repaint after an action before the next snapshot, or the brain
# reads a stale screen (e.g. presses a digit twice because the display had not
# updated yet). The App wrapper settles per-action; this raw loop must too.
SETTLE_SECONDS = 0.2
SETTLE_MAX_SECONDS = 1.6  # poll for a changed snapshot up to this long
# Gap between fired actions in one turn's batch. An AX action lands within
# ~250ms of dispatch even though the daemon pads its *response* to ~1.1s
# (measured, cua-driver 0.5.1) — fire-and-go with this gap keeps ordering
# without paying the padding per action.
ACTION_GAP_SECONDS = 0.3

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
- BUTTONS: the clickable elements, listed as [N] role 'name'.

PLAN AHEAD: "clicks" is the FULL ordered click sequence you can determine from
the CURRENT screen to reach the goal — usually several clicks, not one. After
they run you'll see the new screen and can finish or continue.

Reply with ONLY this JSON object, nothing else:
{"plan": "<one short sentence>", "done": <bool>, "clicks": ["<name>", ...]}

Rules:
- Each click is a button's exact 'name' from THIS turn's list — the quoted
  name only, e.g. "7" or "Save". If several buttons share the same name, use
  that button's [N] number instead (a bare number, e.g. 18).
- Click only the buttons the GOAL needs — never the text or headings near
  them.
- Set done=true with empty clicks ONLY when the DISPLAY already shows the
  final goal state. Never claim done for clicks whose effect you have not
  seen.
- If a button the GOAL needs is not in the list, the goal cannot be done on
  this screen. Reply {"plan": "cannot: <the missing button> is not on
  screen", "done": false, "clicks": []}. NEVER substitute a similar button.

Arithmetic on a calculator: "compute A op B" from a cleared display = the
digits of A, then op, then the digits of B, then "=". Chained
"(A op1 B) op2 C" = digits of A, op1, digits of B, "=", op2, digits of C,
"=" again. Multi-digit numbers are clicked one digit at a time in reading
order: 47 = "4" then "7".
Example "compute 7 × 6":
{"plan": "7 times 6", "done": false, "clicks": ["7", "×", "6", "="]}

FIRST check every button your plan needs is in THIS turn's list. Example:
the goal needs "÷" but no '÷' is listed:
{"plan": "cannot: ÷ is not on screen", "done": false, "clicks": []}
"""


# Menu-bar subtrees explode the element list (Recent Items, every open app, …)
# and a backgrounded app's menu items are disabled no-ops anyway (SKILL.md §6).
# Drop them so the brain sees only in-window controls.
_MENU_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"}


_DIGEST_LINE = re.compile(r"\[(\d+)\] \S+ '([^']*)'")

# Symbol -> spoken-name aliases. Real macOS apps name AX buttons with words
# ("Multiply", "Equals") while goals and small models speak symbols ("×",
# "="). Tried only when the literal name has no match, and only if the alias
# resolves uniquely — so a page that really has a "×" button still wins.
_NAME_ALIASES: dict[str, list[str]] = {
    "×": ["Multiply"], "*": ["Multiply", "×"],
    "÷": ["Divide"], "/": ["Divide", "÷"],
    "+": ["Add", "Plus"],
    "−": ["Subtract", "Minus", "-"], "-": ["Subtract", "Minus", "−"],
    "=": ["Equals"],
    ".": ["Point", "Decimal"],
    "AC": ["All Clear", "Clear"], "C": ["Clear", "All Clear"],
    "Clear": ["All Clear"],
    "±": ["Change Sign", "Negate"],
    "%": ["Percent"],
}


def resolve_clicks(clicks: list, elements: str) -> list[int]:
    """resolve_clicks_guarded without the honesty guard: every resolvable
    click, unresolvable ones silently dropped. Kept for callers that want
    pure resolution (scoring, tests)."""
    out, _ = resolve_clicks_guarded(clicks, elements, guard=False)
    return out


def resolve_clicks_guarded(clicks: list, elements: str, *,
                           guard: bool = True) -> tuple[list[int], str | None]:
    """Map the brain's click list (button names and/or [N] indices) onto the
    element indices of THIS turn's digest. Names beat indices for a small
    model — it already knows the symbols from the GOAL and doesn't have to
    carry a 60-entry lookup table through a 10-step plan.

    Honesty guard (guard=True): the first name that doesn't resolve STOPS the
    plan — clicks after a step that can't land are wrong by construction, and
    blindly skipping a step is how an agent lies its way into a wrong final
    state. Returns (resolved_prefix, missing_name) so the caller can report
    "cannot: X is not on screen" instead of acting. Ambiguous names (2+
    matches) stop the plan the same way."""
    by_name: dict[str, list[int]] = {}
    valid: set[int] = set()
    for m in _DIGEST_LINE.finditer(elements):
        idx, name = int(m.group(1)), m.group(2)
        by_name.setdefault(name, []).append(idx)
        valid.add(idx)
    out: list[int] = []
    for c in clicks:
        if isinstance(c, bool):
            continue
        if isinstance(c, int):
            if c in valid:
                out.append(c)
            elif guard:
                return out, f"[{c}]"
            continue
        if isinstance(c, str):
            c = c.strip()
            hits = by_name.get(c, [])
            if len(hits) == 1:  # name wins — calculator digits ARE names
                out.append(hits[0])
                continue
            if not hits and c.lstrip("-").isdigit():
                n = int(c)      # no button by that name: treat as [N] index
                if n in valid:
                    out.append(n)
                    continue
            if not hits:        # symbol -> spoken-name alias ("×"→"Multiply")
                for alias in _NAME_ALIASES.get(c, []):
                    ahits = by_name.get(alias, [])
                    if len(ahits) == 1:
                        out.append(ahits[0])
                        hits = ahits
                        break
                if hits:
                    continue
            if guard:
                return out, c
    return out, None


def actionable_digest(state_markdown: str, *, max_elements: int = 80) -> tuple[str, str]:
    """Compress a raw AX-tree snapshot into the two things a text brain needs:
    a deduped list of actionable [N] elements (menus excluded), and the current
    value nodes (display text, field contents) used to judge progress. Keeps the
    prompt small enough for a 7B local model to choose reliably."""
    els = ax.parse_tree(state_markdown)
    seen: set[tuple] = set()
    lines: list[str] = []
    windows = 0
    for el in els:
        if el.role == "AXWindow":
            windows += 1
            if windows > 1:
                # everything after a second window root is the duplicate
                # window-twin subtree the daemon sometimes appends — its
                # indices go stale instantly and clicking them misfires
                break
        if el.index is None or el.role in _MENU_ROLES:
            continue
        # Collapse id-pinned duplicates only. Same-named elements WITHOUT ids
        # are normal UI (five 'Learn more' links on one page) and must all
        # stay visible or disambiguation is impossible.
        if el.ax_id:
            key = (el.role, el.text, el.ax_id)
            if key in seen:
                continue
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
    # Qwen3-4B-Instruct-2507 (non-thinking): measured 3.3× faster per decide()
    # than the originally-specced Qwen2.5-7B on an M4 mini with the compact
    # clicks protocol, with a *more* correct calc plan (it clears first).
    DEFAULT_MODEL = "mlx-community/Qwen3-4B-Instruct-2507-4bit"

    def __init__(self, model: str | None = None, *, max_tokens: int = 120):
        self.model_id = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._model = None
        self._tok = None
        self._cache = None          # mlx KV prompt cache, reused across turns
        self._cache_tokens: list[int] = []  # tokens the cache currently holds

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from mlx_lm import load  # lazy: keeps the package stdlib-only
            self._model, self._tok = load(self.model_id)

    def decide(self, goal: str, state: str, history: list[dict]) -> Decision:
        self._ensure_loaded()

        elements, values = actionable_digest(state)
        recent = _recent_actions(history)
        # Static-first ordering so the KV prompt cache gets the longest common
        # prefix across turns: system prompt (constant) > GOAL (per run) >
        # BUTTONS (mostly stable per app) > RECENT/DISPLAY (change every turn).
        user = (
            f"GOAL: {goal}\n\n"
            f"BUTTONS (act by element_index):\n{elements}\n\n"
            + (f"RECENTLY YOU DID (don't repeat a step that already registered):\n{recent}\n\n" if recent else "")
            + f"DISPLAY (the source of truth for what has registered so far):\n"
              f"{values or '(none)'}\n\n"
            "JSON:"
        )
        messages = [{"role": "system", "content": LOCAL_SYSTEM_PROMPT},
                    {"role": "user", "content": user}]
        try:
            # Hybrid-thinking models (Qwen3/3.5 base line) burn the whole token
            # budget on CoT unless thinking is disabled; templates that don't
            # know the kwarg ignore it or raise TypeError.
            tokens = self._tok.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True,
                enable_thinking=False,
            )
        except TypeError:
            tokens = self._tok.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True,
            )
        reply = self._generate_cached(tokens)
        return self._parse_lenient(reply, elements)

    def _generate_cached(self, tokens: list[int]) -> str:
        """Generate with a KV prompt cache reused across decide() calls: only
        the suffix that differs from the previous turn's prompt is prefilled
        (~330 static system-prompt tokens + the stable BUTTONS list skip
        re-prefill — measured ~2.8s -> <1s prefill on turn 2+)."""
        from mlx_lm import stream_generate
        from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache

        if self._cache is None:
            self._cache = make_prompt_cache(self._model)
            self._cache_tokens = []
        common = 0
        for a, b in zip(self._cache_tokens, tokens):
            if a != b:
                break
            common += 1
        # Never reuse the entire prompt verbatim (the cache must end *before*
        # the next position to be generated), and trim any diverged suffix.
        common = min(common, len(tokens) - 1)
        if common < len(self._cache_tokens):
            try:
                trim_prompt_cache(self._cache, len(self._cache_tokens) - common)
                self._cache_tokens = self._cache_tokens[:common]
            except Exception:  # noqa: BLE001 - non-trimmable cache: start over
                self._cache = make_prompt_cache(self._model)
                self._cache_tokens = []
                common = 0

        out: list[str] = []
        gen_tokens: list[int] = []
        depth = 0
        opened = False
        for r in stream_generate(self._model, self._tok, prompt=tokens[common:],
                                 max_tokens=self.max_tokens,
                                 prompt_cache=self._cache):
            out.append(r.text)
            gen_tokens.append(r.token)
            # JSON early-stop: end decoding the moment the top-level object
            # closes — every further token costs ~40-80ms on this hardware.
            for ch in r.text:
                if ch == "{":
                    depth += 1
                    opened = True
                elif ch == "}":
                    depth -= 1
            if opened and depth <= 0:
                break
        self._cache_tokens = list(tokens) + gen_tokens
        return "".join(out)

    @staticmethod
    def _parse_lenient(reply: str, elements: str = "") -> Decision:
        """Parse the clicks-by-NAME protocol: {"plan", "done",
        "clicks": ["name"… | N…]}. Names resolve against this turn's digest
        via resolve_clicks (missing/ambiguous names drop to safe no-ops);
        bare integers still work. A totally unparseable reply becomes a safe
        no-op so the loop re-decides next turn."""
        start, end = reply.find("{"), reply.rfind("}")
        if start != -1 and end != -1:
            try:
                data = json.loads(reply[start:end + 1])
                raw = data.get("clicks", [])
                clicks, missing = resolve_clicks_guarded(
                    raw if isinstance(raw, list) else [], elements)
                reason = str(data.get("plan") or data.get("reason") or "")
                done = bool(data.get("done"))
                if missing is not None:
                    # honesty guard: the plan needs a button this screen does
                    # not have — stop at it and say so instead of acting.
                    clicks = []
                    done = False
                    reason = f"cannot: '{missing}' is not on screen ({reason})"
                return Decision(
                    done=done,
                    reason=reason,
                    actions=[{"tool": "click", "args": {"element_index": n}} for n in clicks],
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        arr = re.search(r'clicks["\']?\s*[:=]\s*\[([^\]]*)\]', reply)
        done = re.search(r'done["\']?\s*[:=]\s*true', reply, re.IGNORECASE) is not None
        if arr:
            raw = re.findall(r'"([^"]+)"|(\d+)', arr.group(1))
            items: list = [s if s else int(n) for s, n in raw]
            clicks = resolve_clicks(items, elements)
            return Decision(done=done, reason="parsed (regex)",
                            actions=[{"tool": "click", "args": {"element_index": n}} for n in clicks])
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

        # Fire-and-go: an AX action lands within ~250ms but the daemon pads its
        # response to ~1.1s — so dispatch each action without blocking, keep
        # ACTION_GAP_SECONDS between them for ordering, and classify errors
        # afterwards (during the settle, whose wall-clock we'd pay anyway).
        # A failed action is a NO-OP (a stale/unknown index errors out rather
        # than clicking the wrong element), so the worst case of a mid-batch
        # failure is a partial sequence — the next turn re-decides on the
        # fresh snapshot either way.
        acted = False
        navigated = False
        pending: list[tuple[str, dict, driver.PendingCall]] = []
        for action in decision.actions:
            tool = action.get("tool", "")
            if tool not in ALLOWED_TOOLS:
                log(f"  ! refused tool {tool!r}")
                continue
            raw_args = action.get("args", {})
            args = {**raw_args, "pid": app.pid, "window_id": app.window_id}
            if pending:
                time.sleep(ACTION_GAP_SECONDS)
            el = None
            if "element_index" in raw_args:
                el = next((e for e in snap.elements
                           if e.index == raw_args["element_index"]), None)
            pending.append((tool, raw_args, driver.fire(tool, args)))
            acted = True
            if on_step is not None:
                on_step(tool, el, raw_args)
            # Navigation cut: clicking a link (or a page-changing button)
            # invalidates every index that follows — the rest of the plan
            # was made for a page that no longer exists. Stop the batch and
            # let the next turn re-decide on the new screen. (Calculator
            # digits etc. are plain AXButtons and batch straight through.)
            if el is not None and (
                    el.role == "AXLink"
                    or (el.role == "AXButton" and (el.text or "").split(" ")[0]
                        in ("Continue", "Finish", "Next", "Submit", "Back",
                            "Sign", "Log"))):
                navigated = True
                if action is not decision.actions[-1]:
                    log(f"  ⤵ navigation click {el.text!r} — dropping "
                        f"{len(decision.actions) - len(pending)} stale "
                        "follow-up clicks, re-deciding on the new page")
                break

        if acted:
            if navigated:
                # A page load can look "stable" while the web area is still
                # empty (chrome-only tree) — wait for the tree to actually
                # CHANGE from the pre-click page first, then settle. Reading
                # too early makes an honest brain say "button not on screen"
                # and a sloppy one click Safari chrome.
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    try:
                        if app.snapshot().markdown != state:
                            break
                    except driver.DriverError:
                        break
                    time.sleep(0.25)
            _settle_until_stable(app)
        for tool, raw_args, call in pending:
            err = call.error()
            if err is None:
                log(f"  ✓ {tool} {raw_args}")
            elif isinstance(err, StaleIndexError):
                log(f"  ! stale index on {tool} {raw_args} — brain re-decides on fresh state")
            elif isinstance(err, TransientDriverError):
                log(f"  ~ transient on {tool} ({err}); next snapshot shows truth")
            else:
                log(f"  ! {tool} {raw_args} rejected ({err}); brain re-decides")

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
