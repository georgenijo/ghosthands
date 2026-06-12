"""Benchmark/acceptance task suite (DESIGN.md §9).

Every task ships:
- `goal`        — the natural-language goal handed to a brain, verbatim.
- `setup()`     — put the world in a known starting state (and make sure the
                  done-detector is NOT already satisfied).
- `done_check()`— a machine check of the WORLD (never the agent's words).
                  Must be passive: it may not launch apps or mutate state,
                  because it is polled while a brain is mid-run.

Both tasks are safe to repeat and require nothing beyond apps already on the
machine (Calculator, Brave).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import ax, driver

BRAVE_BUNDLE = "com.brave.Browser"


@dataclass
class Task:
    name: str
    nature: str  # native | browser
    goal: str
    setup: Callable[[], None]
    done_check: Callable[[], bool]
    bundle_id: str = ""          # app the own-loop contenders launch
    url: str = ""                # page to open (browser tasks)
    window_title: str = ""       # substring identifying the target window/tab
    simple_goal: str = ""        # goal for own-loop brains (page pre-opened by
                                 # the loop); defaults to `goal` when empty
    dom_goal: str = ""           # browser-neutral goal for DOM contenders
                                 # (chrome-devtools-mcp / agent-browser): names
                                 # no specific browser, since those tools bring
                                 # their own. Defaults to `goal` when empty.

    def loop_goal(self) -> str:
        return self.simple_goal or self.goal

    def browser_goal(self) -> str:
        return self.dom_goal or self.goal


def _running_pid(bundle_id: str) -> int | None:
    apps = driver.call("list_apps", {})
    for a in (apps.get("apps", apps) if isinstance(apps, dict) else apps):
        if a.get("bundle_id") == bundle_id and a.get("running"):
            return a.get("pid")
    return None


def _on_screen_windows(pid: int) -> list[dict]:
    listed = driver.call("list_windows", {"pid": pid})
    windows = listed["windows"] if isinstance(listed, dict) else listed
    return [w for w in windows or [] if w.get("is_on_screen")]


# --- calculator: 7 × 6 = 42 (native) ---------------------------------------

def _calc_setup() -> None:
    # macOS Calculator PERSISTS its expression across quit/relaunch (state
    # restoration), so quitting is not enough — a stale "7 × 99,…" would poison
    # every run. Relaunch and AX-clear to "0" so every contender starts clean.
    from .actions import App  # local import: avoids an import cycle
    subprocess.run(
        ["osascript", "-e", 'tell application "Calculator" to quit'],
        capture_output=True, timeout=15,
    )
    time.sleep(1.2)
    app = App.launch("com.apple.calculator")
    for _ in range(15):
        if app.read("Edit field") == "0":
            return
        app.click(lambda el: el.ax_id in ("AllClear", "Clear"))


def _calc_done() -> bool:
    pid = _running_pid("com.apple.calculator")
    if pid is None:
        return False
    titled = [w for w in _on_screen_windows(pid) if w.get("title")]
    if not titled:
        return False
    try:
        state = driver.call("get_window_state", {
            "pid": pid, "window_id": titled[0]["window_id"], "capture_mode": "ax",
            "query": "Edit field",
        }, timeout=20)
    except driver.DriverError:
        return False
    for el in ax.parse_tree(state.get("tree_markdown", "")):
        if (el.label or "") == "Edit field":
            return el.value == "42"
    return False


CALC = Task(
    name="calc-7x6",
    nature="native",
    goal=(
        "Compute 7 × 6 on the macOS Calculator app (bundle id "
        "com.apple.calculator) and finish with the result showing on its "
        "display. Clear the calculator first if it shows a previous value."
    ),
    setup=_calc_setup,
    done_check=_calc_done,
    bundle_id="com.apple.calculator",
)


# --- browser: example.com → IANA (browser) ----------------------------------

# Safari, not the user's daily browser (Brave): a shared live browser races
# the user's own tab switches — window titles only show the ACTIVE tab, so
# both the done-detector and the agent's window targeting get invalidated
# mid-run. Safari sits idle on this machine; setup quits it for a fresh,
# deterministic state every run.

SAFARI_BUNDLE = "com.apple.Safari"
_IANA_TITLE = "Example Domains"  # destination (iana.org, plural);
                                 # example.com itself titles "Example Domain"


def _safari_title_active(fragment: str) -> bool:
    pid = _running_pid(SAFARI_BUNDLE)
    if pid is None:
        return False
    return any(fragment in (w.get("title") or "") for w in _on_screen_windows(pid))


# Browser-agnostic world check: the WEB task is run by contenders that each
# bring a DIFFERENT browser — Safari (cua AX / pixel, local 7B, scripted),
# Chrome (chrome-devtools-mcp), Chromium (agent-browser/Playwright). So the
# done-detector can't key on one browser; it scans every running browser-ish
# app's on-screen window titles for the destination page title.
_BROWSER_HINTS = ("safari", "chrome", "chromium", "brave", "edge", "firefox",
                  "webkit", "playwright")


def _any_browser_title(fragment: str) -> bool:
    apps = driver.call("list_apps", {})
    items = apps.get("apps", apps) if isinstance(apps, dict) else apps
    for a in items or []:
        if not a.get("running"):
            continue
        name = (a.get("name") or a.get("bundle_id") or "").lower()
        if not any(h in name for h in _BROWSER_HINTS):
            continue
        pid = a.get("pid")
        if pid is None:
            continue
        try:
            for w in _on_screen_windows(pid):
                if fragment in (w.get("title") or ""):
                    return True
        except (driver.DriverError, KeyError, TypeError):
            continue
    return False


def _web_setup() -> None:
    # Clear any prior "Example Domains" window across the browsers contenders
    # use, so the done-detector isn't pre-satisfied. Touches only idle Safari
    # and the contenders' OWN ephemeral browsers — never the user's daily Brave.
    subprocess.run(
        ["osascript", "-e", 'tell application "Safari" to quit'],
        capture_output=True, timeout=15,
    )
    subprocess.run(["agent-browser", "close", "--all"],
                   capture_output=True, timeout=20)
    # chrome-devtools-mcp Chrome dies with the claude subprocess (--isolated).
    time.sleep(2)


def _web_done() -> bool:
    return _any_browser_title(_IANA_TITLE)


WEB = Task(
    name="web-example-iana",
    nature="browser",
    goal=(
        "In Safari (bundle id com.apple.Safari), open https://example.com, "
        "then click the 'Learn more' link on that page. Done when the IANA "
        "'Example Domains' page has loaded in that tab."
    ),
    setup=_web_setup,
    done_check=_web_done,
    # own-loop (local 7B / scripted) drive Safari via cua AX:
    bundle_id=SAFARI_BUNDLE,
    url="https://example.com",
    window_title="Example",
    simple_goal=(
        "The page https://example.com is open. Click the 'Learn more' link on "
        "it. Done when the IANA 'Example Domains' page has loaded."
    ),
    # DOM contenders (chrome-devtools-mcp / agent-browser) bring their own
    # browser — name none:
    dom_goal=(
        "Open https://example.com, then click the 'Learn more' link on that "
        "page. You are done when the IANA page titled 'Example Domains' has "
        "loaded."
    ),
)


# --- Home Assistant: toggle an isolated test entity (browser) ---------------

# Drives the HA web UI to flip a DEDICATED, side-effect-free helper
# (input_boolean.ghosthands_test) on an isolated, hidden dashboard
# (/ghosthands-test) — never a real device. The done-detector reads the WORLD
# through HA's REST API (the entity state), not the agent's words or the DOM.
#
# One-time HA setup (already applied on this machine; reproduce with
# scripts via the homelab repo's ha_ws.py if recreating):
#   input_boolean/create name="GhostHands Test"  -> input_boolean.ghosthands_test
#   lovelace/dashboards/create url_path="ghosthands-test" show_in_sidebar=false
#   lovelace/config/save  -> one button card, entity above, tap_action: toggle

HA_URL = os.environ.get("GHOSTHANDS_HA_URL", "http://localhost:8123")
HA_ENTITY = "input_boolean.ghosthands_test"
HA_DASHBOARD = f"{HA_URL}/ghosthands-test"


def _ha_token() -> str:
    """Long-lived HA token: GHOSTHANDS_HA_TOKEN, else the homelab repo's .env."""
    tok = os.environ.get("GHOSTHANDS_HA_TOKEN")
    if tok:
        return tok
    env = Path.home() / "homelab" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("HA_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError(
        "no HA token: set GHOSTHANDS_HA_TOKEN or add HA_TOKEN to ~/homelab/.env"
    )


def _ha_request(path: str, *, method: str = "GET", body: dict | None = None) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{HA_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {_ha_token()}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def _ha_state(entity: str) -> str:
    return json.loads(_ha_request(f"/api/states/{entity}"))["state"]


def _ha_set(entity: str, on: bool) -> None:
    service = "turn_on" if on else "turn_off"
    _ha_request(f"/api/services/input_boolean/{service}", method="POST",
                body={"entity_id": entity})


def _ha_setup() -> None:
    _ha_set(HA_ENTITY, on=False)
    time.sleep(0.5)


def _ha_done() -> bool:
    try:
        return _ha_state(HA_ENTITY) == "on"
    except (urllib.error.URLError, KeyError, ValueError, RuntimeError):
        return False


HA_TOGGLE = Task(
    name="ha-toggle",
    nature="browser",
    goal=(
        "In the web browser, open the Home Assistant page at "
        f"{HA_DASHBOARD} and turn the 'GhostHands Test' button ON (click it "
        "once so its state changes to On). Done when it reads On."
    ),
    setup=_ha_setup,
    done_check=_ha_done,
    bundle_id=BRAVE_BUNDLE,
    url=HA_DASHBOARD,
    window_title="GhostHands",
    # The own-loop opens the page itself; the brain only has to flip the toggle.
    simple_goal=(
        "The 'GhostHands Test' button is on screen and currently Off. Click it "
        "once to turn it On. Done when it reads On."
    ),
)


ALL_TASKS = {t.name: t for t in (CALC, WEB, HA_TOGGLE)}
