"""DOM-tier own-loop for web surfaces — the execution target of surface
routing (issue #9).

The AX loop (`ownloop.run_loop`) is the right hammer for native AppKit apps.
For a Chromium web view it loses (background tab reads as empty chrome, AXPress
ignored by React, hidden <select> options, type-needs-focus — all documented in
`webtier`). So when `route_surface` says "web", drive the page through the DOM
tier instead: build the per-turn digest from the live DOM (piercing open shadow
roots), let the SAME local brain pick a click by name, and execute it with a
real `element.click()` that fires the page's handlers.

It reuses `LocalBrain.decide_from_digest` verbatim, so the clicks-by-name
protocol, honesty guard, and KV cache all carry over unchanged — only the
source of the element list (DOM, not AX) and the executor (CDP, not AXPress)
differ. $0, local, no vision."""

from __future__ import annotations

import json
import time

from . import webtier

GH_IDX = "data-gh-idx"

# Walk the live DOM (piercing open shadow roots via the resolver), tag each
# actionable, visible, enabled control with a stable per-turn data-gh-idx, and
# return [{idx, role, name, value}]. role is mapped to an AX-ish name so the
# brain's symbol/name logic reads identically to the AX path.
_DIGEST_BODY = r"""
  const roleMap = {button:'AXButton', a:'AXLink', input:'AXTextField',
                   select:'AXPopUpButton', textarea:'AXTextArea'};
  const out = [];
  let i = 0;
  for (const e of H.deepAll(document)) {
    const tag = e.tagName ? e.tagName.toLowerCase() : '';
    const role = e.getAttribute && e.getAttribute('role');
    if (!(['button','a','input','select','textarea'].includes(tag) || role)) continue;
    if (e.disabled) continue;
    if (e.type === 'hidden') continue;
    const r = e.getBoundingClientRect ? e.getBoundingClientRect() : null;
    if (r && r.width === 0 && r.height === 0) continue;  // collapsed/hidden
    e.setAttribute('data-gh-idx', String(i));
    out.push({idx: i, role: roleMap[tag] || 'AX' + (role || 'Group'),
              name: H.accName(e), value: (e.value != null ? String(e.value) : '')});
    i++;
  }
  return out;
"""


def dom_digest(ws_url: str) -> tuple[str, str, int]:
    """Build (BUTTONS, DISPLAY, count) from the live DOM, in the exact shape
    LocalBrain expects from the AX path."""
    items = webtier.cdp_eval(ws_url, webtier._expr(_DIGEST_BODY)) or []
    buttons = "\n".join(f"[{o['idx']}] {o['role']} {o['name']!r}" for o in items)
    values = "\n".join(f"- {o['name']}: {o['value']!r}"
                       for o in items if o.get("value"))
    return buttons, values, len(items)


def _location(ws_url: str) -> str:
    try:
        return webtier.cdp_eval(ws_url, "location.href") or ""
    except webtier.WebTierError:
        return ""


def run_web_loop(brain, goal: str, url: str, *, port: int = webtier.DEFAULT_PORT,
                 user_data_dir: str | None = None, max_turns: int = 12,
                 done_check=None, on_step=None, ws_url: str | None = None,
                 log=print) -> bool:
    """Drive a web `url` toward `goal` through the DOM tier with `brain`.

    Launches Brave on `url` with the debug port open (isolated when
    `user_data_dir` is given) unless an existing `ws_url` is passed (the bench
    reuses one tab). Each turn: digest the DOM -> brain picks a click by name ->
    `cdp_click` by the tagged index. A click that changes `location` ends the
    turn's batch (the rest of the plan was made for the old page). Stops on
    `done_check` (a world predicate) or the brain's done flag."""
    if ws_url is None:
        webtier.launch_web(url, port=port, user_data_dir=user_data_dir,
                           new_instance=bool(user_data_dir))
        frag = url.split("://", 1)[-1].split("/", 1)[-1] or url
        target = None
        for _ in range(60):
            try:
                target = webtier.target_for_url(port, frag)
                break
            except webtier.WebTierError:
                time.sleep(0.5)
        if target is None:
            raise webtier.WebTierError(f"no tab for {url!r} on port {port}")
        ws_url = target["webSocketDebuggerUrl"]

    log(f"[{brain.name}/web] {url} goal={goal!r}")
    history: list[dict] = []
    for turn in range(1, max_turns + 1):
        if done_check is not None and done_check():
            log(f"[{brain.name}/web] world goal reached")
            return True
        buttons, values, n = dom_digest(ws_url)
        decision = brain.decide_from_digest(goal, buttons, values, history)
        log(f"[{brain.name}/web] turn {turn}: done={decision.done} "
            f"{decision.reason!r} ({len(decision.actions)} actions, {n} controls)")

        before_loc = _location(ws_url)
        for action in decision.actions:
            idx = action.get("args", {}).get("element_index")
            if idx is None:
                continue
            ok = webtier.cdp_click(ws_url, f'[{GH_IDX}="{idx}"]')
            log(f"  {'✓' if ok else '!'} click [{idx}]")
            if on_step is not None:
                on_step("click", None, {"element_index": idx})
            time.sleep(0.4)
            if _location(ws_url) != before_loc:
                # navigation: remaining indices belong to the old page — stop
                # the batch and re-digest the new page next turn.
                if action is not decision.actions[-1]:
                    log("  ⤵ navigation — dropping stale follow-up clicks")
                break

        history.append({"role": "user", "content": f"GOAL: {goal}"})
        history.append({"role": "assistant",
                        "content": json.dumps(decision.__dict__)})
        history = history[-8:]

        if done_check is not None and done_check():
            log(f"[{brain.name}/web] world goal reached")
            return True
        if decision.done:
            return bool(done_check()) if done_check is not None else True
    log(f"[{brain.name}/web] gave up after {max_turns} turns")
    return False
