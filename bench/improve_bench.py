#!/usr/bin/env python3
"""A/B harness for the #8 / #9 / #10 improvement pass.

Each issue gets a BEFORE measurement (current code) and an AFTER measurement
(the new code path), plus a live WORLD check through the fixture server's
/events log — never the agent's words. The same script runs at baseline (only
BEFORE exists; AFTER shows "pending") and again after implementation (full A/B).

  #10  per-turn digest size on a web target — current digest (browser chrome +
       non-actionable structural nodes leak in) vs an AXWebArea-scoped,
       actionable-only digest. Regression guard: the NATIVE digest is unchanged.
  #8   a control nested in an OPEN shadow root — a plain document.querySelector
       finds nothing; deepQuery + find-by-accessible-name reach it, and verify=
       catches a click that no-ops. World = the fixture logged the action.
  #9   surface routing — today everything goes through AX even on web (where the
       shadow control isn't even in the AX tree); route_surface sends web->DOM,
       native->AX. World = the DOM tier completes what AX cannot see.

Deterministic numbers come off the committed fixture (tests/fixtures/
brave_trimmed.md) so they reproduce without hardware; the live rows need Brave +
cua-driver + the fixture server and are skipped (degraded to "n/a") when absent.

Usage:
  python3 bench/improve_bench.py [--mode baseline|after] [--port 9444]
                                 [--keep-brave]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from ghosthands import driver, ownloop, webtier  # noqa: E402
from ghosthands.actions import App  # noqa: E402

FIXTURE_PORT = 8773
FIXTURE_URL = f"http://127.0.0.1:{FIXTURE_PORT}"
SHADOW_URL = f"{FIXTURE_URL}/shadow.html"
BRAVE_PROFILE = "/tmp/gh-isolated-brave"
COMMITTED_FIXTURE = REPO / "tests" / "fixtures" / "brave_trimmed.md"
RESULTS_DIR = REPO / "bench" / "results"

PENDING = "— pending"   # an AFTER path that does not exist yet (baseline run)


# --------------------------------------------------------------------------- #
# fixture server + isolated Brave                                             #
# --------------------------------------------------------------------------- #

def _fixture_get(path: str, timeout: float = 5.0) -> bytes:
    return urllib.request.urlopen(f"{FIXTURE_URL}{path}", timeout=timeout).read()


def ensure_fixture() -> bool:
    """Fixture server up + event log reset. Returns False if it can't be had."""
    import subprocess
    try:
        _fixture_get("/events")
    except OSError:
        server = REPO / "bench" / "fixture" / "server.py"
        subprocess.Popen([sys.executable, str(server)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        for _ in range(20):
            time.sleep(0.25)
            try:
                _fixture_get("/events")
                break
            except OSError:
                continue
        else:
            return False
    _fixture_get("/reset")
    return True


def events() -> list[dict]:
    try:
        return json.loads(_fixture_get("/events"))
    except (OSError, ValueError):
        return []


def event_hit(path: str, query: dict | None = None) -> bool:
    for e in events():
        if e.get("path") == path and (query is None or e.get("query") == query):
            return True
    return False


def launch_isolated_brave(port: int) -> dict:
    """Launch an isolated Brave (own --user-data-dir, own debug port, new
    instance) on the shadow fixture so the user's working Brave is untouched.
    Returns {pid, window_id, ws_url, target}."""
    # Kill any isolated instance left over from a prior run — targeted by the
    # unique profile path, so the user's working Brave is never matched.
    import subprocess
    subprocess.run(["pkill", "-f", f"user-data-dir={BRAVE_PROFILE}"],
                   capture_output=True)
    time.sleep(1.0)
    res = driver.call("launch_app", {
        "bundle_id": webtier.DEFAULT_BUNDLE_ID,
        "electron_debugging_port": port,
        "creates_new_application_instance": True,
        "additional_arguments": [f"--user-data-dir={BRAVE_PROFILE}",
                                 "--no-first-run", "--no-default-browser-check"],
        "urls": [SHADOW_URL],
    })
    # The launch RESPONSE carries the isolated instance's own pid — use it
    # directly rather than list_apps (which is ambiguous when the user's
    # working Brave is also running under the same bundle id).
    pid = res.get("pid") if isinstance(res, dict) else None
    target = None
    for _ in range(60):
        try:
            target = webtier.target_for_url(port, "shadow.html")
            break
        except webtier.WebTierError:
            time.sleep(0.5)
    if target is None:
        raise RuntimeError("shadow.html tab never appeared on the debug port")
    ws_url = target["webSocketDebuggerUrl"]
    # wait for the shadow root + its controls to exist
    for _ in range(60):
        ready = webtier.cdp_eval(
            ws_url,
            "(() => { const p = document.querySelector('gh-panel');"
            "  return !!(p && p.shadowRoot && p.shadowRoot.getElementById('save')); })()")
        if ready:
            break
        time.sleep(0.5)
    window_id = _brave_window(pid)
    return {"pid": pid, "window_id": window_id, "ws_url": ws_url, "target": target}


def _brave_window(pid: int | None) -> int | None:
    """The on-screen Shadow window of the isolated Brave pid, for AX snapshots."""
    if pid is None:
        return None
    for _ in range(10):
        try:
            listed = driver.call("list_windows", {"pid": pid})
        except driver.DriverError:
            return None
        wins = listed["windows"] if isinstance(listed, dict) else listed
        titled = [w for w in wins or [] if w.get("is_on_screen") and w.get("title")]
        for w in titled:
            if "Shadow" in (w.get("title") or ""):
                return w["window_id"]
        if titled:
            return titled[0]["window_id"]
        time.sleep(0.5)
    return None


def navigate_fresh(ws_url: str) -> None:
    """Reset the world log and reload a clean shadow.html in the same tab."""
    _fixture_get("/reset")
    webtier.cdp_navigate(ws_url, SHADOW_URL)
    for _ in range(40):
        ready = webtier.cdp_eval(
            ws_url,
            "(() => { const p = document.querySelector('gh-panel');"
            "  return !!(p && p.shadowRoot && p.shadowRoot.getElementById('save')); })()")
        if ready:
            return
        time.sleep(0.25)


# --------------------------------------------------------------------------- #
# helpers for AFTER paths that may not exist yet (baseline run)              #
# --------------------------------------------------------------------------- #

def _opt(fn_name: str, *args, **kwargs):
    """Call webtier.<fn_name>(*args) if it exists with this signature, else
    PENDING. A missing function (AttributeError) or a not-yet-added keyword
    (TypeError) both mean "this AFTER path isn't built yet" at baseline."""
    fn = getattr(webtier, fn_name, None)
    if fn is None:
        return PENDING
    try:
        return fn(*args, **kwargs)
    except TypeError as e:
        # only swallow the "unexpected keyword / signature" shape, not a real
        # in-body TypeError from a built function
        if "argument" in str(e):
            return PENDING
        raise


# --------------------------------------------------------------------------- #
# #10 — web digest scoping                                                    #
# --------------------------------------------------------------------------- #

# A real browser AXButton/AXLink is not a menu role, so actionable_digest keeps
# it; an AXWebArea-scoped digest drops the chrome and the non-actionable
# structural nodes the brain never clicks.
_ACTIONABLE_ROLES = {"AXButton", "AXLink", "AXTextField", "AXCheckBox",
                     "AXRadioButton", "AXPopUpButton", "AXMenuButton",
                     "AXTextArea", "AXComboBox", "AXSlider", "AXTab"}


def _digest_rows(label: str, markdown: str) -> list[dict]:
    before, _ = ownloop.actionable_digest(markdown)
    after = _opt_digest(markdown)                 # digest str, or PENDING
    pending = after is PENDING
    b_lines = [l for l in before.splitlines() if l.strip()]
    a_lines = [] if pending else [l for l in after.splitlines() if l.strip()]
    return [{
        "issue": "#10", "metric": f"{label}: digest chars",
        "before": len(before), "after": PENDING if pending else len(after),
    }, {
        "issue": "#10", "metric": f"{label}: digest lines",
        "before": len(b_lines), "after": PENDING if pending else len(a_lines),
    }, {
        "issue": "#10", "metric": f"{label}: non-actionable lines (chrome/structural)",
        "before": sum(1 for l in b_lines if not _is_actionable(l)),
        "after": PENDING if pending else sum(1 for l in a_lines if not _is_actionable(l)),
    }]


def _opt_digest(markdown: str):
    """ownloop.actionable_digest(markdown, web_scope=True) once it grows the
    parameter; PENDING until then."""
    try:
        buttons, _ = ownloop.actionable_digest(markdown, web_scope=True)
        return buttons
    except TypeError:
        return PENDING


def _is_actionable(line: str) -> bool:
    # digest line shape: "[N] AXRole 'name'..."
    parts = line.strip().split(maxsplit=2)
    return len(parts) >= 2 and parts[1] in _ACTIONABLE_ROLES


def measure_issue10(live: dict | None) -> list[dict]:
    rows: list[dict] = []
    # reproducible number off the committed fixture (no hardware)
    rows += _digest_rows("web(committed)", COMMITTED_FIXTURE.read_text())
    # native regression guard: the digest a calculator yields must be unchanged
    # by web scoping. Use the committed fixture's *non-web* shape as a stand-in
    # check that web_scope leaves a menu-free native tree alone is covered by
    # tests; here we record the real live web number when Brave is up.
    if live and live.get("window_id") is not None:
        try:
            app = App(live["pid"], live["window_id"])
            md = app.snapshot().markdown
            (RESULTS_DIR / "improve_live_brave_snapshot.md").write_text(md)
            rows += _digest_rows("web(live Brave)", md)
        except Exception as e:  # noqa: BLE001
            rows.append({"issue": "#10", "metric": "web(live Brave): digest",
                         "before": f"n/a ({type(e).__name__})", "after": "n/a"})
    return rows


# --------------------------------------------------------------------------- #
# #8 — shadow piercing / find-by-name / verify                               #
# --------------------------------------------------------------------------- #

def measure_issue8(live: dict | None) -> list[dict]:
    if not live:
        return [{"issue": "#8", "metric": "shadow DOM (needs Brave)",
                 "before": "n/a", "after": "n/a", "world": "skipped"}]
    ws = live["ws_url"]
    rows: list[dict] = []

    # (1) shadow-nested button click ----------------------------------------
    navigate_fresh(ws)
    # BEFORE: a plain document.querySelector cannot pierce the open shadow root.
    before_found = bool(webtier.cdp_eval(
        ws, "(() => { const el = document.querySelector('#save');"
            "  if (!el) return false; el.click(); return true; })()"))
    time.sleep(0.8)
    before_world = event_hit("/set/shadowclick")
    # AFTER: deep cdp_click pierces.
    navigate_fresh(ws)
    after_found = _opt("cdp_click", ws, "#save")
    time.sleep(0.8)
    after_world = event_hit("/set/shadowclick") if after_found is not PENDING else PENDING
    rows.append({"issue": "#8", "metric": "shadow button click reaches it",
                 "before": before_found, "after": after_found,
                 "world": f"before={before_world} after={after_world}"})

    # (2) find-by-accessible-name into a shadow aria-labelled field ----------
    navigate_fresh(ws)
    # BEFORE: a plain selector by aria-label can't pierce either.
    before_set = bool(webtier.cdp_eval(
        ws, "(() => { const el = document.querySelector("
            "'input[aria-label=\"Project name\"]');"
            "  if (!el) return false; el.value='x'; return true; })()"))
    before_name_world = event_hit("/set/name", {"value": "Acme"})
    # AFTER: set_value_by_name resolves by accessible name + pierces.
    navigate_fresh(ws)
    after_set = _opt("set_value_by_name", ws, "Project name", "Acme")
    time.sleep(0.8)
    after_name_world = (event_hit("/set/name", {"value": "Acme"})
                        if after_set is not PENDING else PENDING)
    rows.append({"issue": "#8", "metric": "find-by-name sets shadow field",
                 "before": before_set, "after": after_set,
                 "world": f"before={before_name_world} after={after_name_world}"})

    # (3) verify catches a no-op (disabled button) --------------------------
    navigate_fresh(ws)
    # BEFORE: the old click returns True on a found element even when disabled
    # -> it no-ops, but the caller is told it "worked".
    before_click = bool(webtier.cdp_eval(
        ws, "(() => { const p=document.querySelector('gh-panel');"
            "  const el=p.shadowRoot.getElementById('confirm');"
            "  if(!el) return false; el.click(); return true; })()"))
    # AFTER: verify= predicate sees the world didn't change -> returns False.
    after_click = _opt("cdp_click", ws, "#confirm",
                       verify="!document.querySelector('gh-panel')"
                              ".shadowRoot.getElementById('confirmed').hidden")
    noop_world = event_hit("/set/confirm")  # must stay False either way
    rows.append({"issue": "#8", "metric": "verify catches no-op click "
                 "(True=lies / False=honest)",
                 "before": before_click, "after": after_click,
                 "world": f"/set/confirm fired={noop_world} (want False)"})

    # (4) light DOM must STILL work through the deep path --------------------
    navigate_fresh(ws)
    after_light = _opt("cdp_click", ws, "#light-link")
    time.sleep(0.8)
    light_world = (event_hit("/set/lightclick")
                   if after_light is not PENDING else PENDING)
    rows.append({"issue": "#8", "metric": "deep path still clicks light DOM",
                 "before": "(n/a)", "after": after_light,
                 "world": f"after={light_world}"})
    return rows


# --------------------------------------------------------------------------- #
# #9 — surface routing                                                        #
# --------------------------------------------------------------------------- #

def measure_issue9(live: dict | None) -> list[dict]:
    rows: list[dict] = []
    # router decision (deterministic, no hardware) --------------------------
    rows.append({"issue": "#9", "metric": "route(Brave bundle) -> tier",
                 "before": "AX (always)",
                 "after": _opt("route_surface",
                               bundle_id=webtier.DEFAULT_BUNDLE_ID)})
    rows.append({"issue": "#9", "metric": "route(calculator bundle) -> tier",
                 "before": "AX (always)",
                 "after": _opt("route_surface",
                               bundle_id="com.apple.calculator")})
    rows.append({"issue": "#9", "metric": "route(AXWebArea snapshot) -> tier",
                 "before": "AX (always)",
                 "after": _opt("route_surface",
                               markdown="- [0] AXWindow\n  - [1] AXWebArea")})
    # the live "why AX loses": a backgrounded web window's AX capture is chrome
    # only — no AXWebArea, so the page's controls are absent from the AX tree.
    # (Focus-dependent, hence corroboration; the router decision above is the
    # robust headline.) The DOM tier reads every control regardless.
    if live and live.get("window_id") is not None:
        try:
            navigate_fresh(live["ws_url"])
            app = App(live["pid"], live["window_id"])
            md = app.snapshot().markdown
            ax_has_webarea = "AXWebArea" in md
            ax_sees_ctrls = "Save project" in md
            dom_sees_ctrls = bool(webtier.cdp_eval(
                live["ws_url"],
                "!!document.querySelector('gh-panel').shadowRoot"
                ".getElementById('save')"))
            rows.append({
                "issue": "#9",
                "metric": "page controls visible to AX vs DOM tier",
                "before": f"AX: webarea={ax_has_webarea} ctrls={ax_sees_ctrls}",
                "after": f"DOM: ctrls={dom_sees_ctrls}",
                "world": "AX visibility focus-dependent; DOM always reads it"})
        except Exception as e:  # noqa: BLE001
            rows.append({"issue": "#9", "metric": "AX reachability (needs Brave)",
                         "before": f"n/a ({type(e).__name__})", "after": "n/a"})

        # The headline #9 proof: the router sends a web target to the DOM tier,
        # and that tier COMPLETES a task end-to-end (world-checked). Robust —
        # no model dependency, so it can't be gamed by 4B luck.
        try:
            navigate_fresh(live["ws_url"])
            tier = _opt("route_surface", bundle_id=webtier.DEFAULT_BUNDLE_ID)
            completed = _opt("click_by_name", live["ws_url"], "Save project")
            time.sleep(0.9)
            world = (event_hit("/set/shadowclick")
                     if completed is not PENDING else PENDING)
            rows.append({
                "issue": "#9", "metric": "routed web->DOM completes a task",
                "before": "AX always (no route)",
                "after": f"tier={tier}, click={completed}",
                "world": f"/set/shadowclick={world}"})
        except Exception as e:  # noqa: BLE001
            rows.append({"issue": "#9", "metric": "routed completion (needs Brave)",
                         "before": "n/a", "after": f"n/a ({type(e).__name__})"})
    return rows


# --------------------------------------------------------------------------- #
# reporting                                                                   #
# --------------------------------------------------------------------------- #

def render(rows: list[dict]) -> str:
    w_metric = max(len(r["metric"]) for r in rows)
    w_b = max(len(str(r.get("before", ""))) for r in rows)
    w_a = max(len(str(r.get("after", ""))) for r in rows)
    head = (f"| {'#':<3} | {'metric':<{w_metric}} | {'before':<{w_b}} "
            f"| {'after':<{w_a}} | world / note |")
    out = [head, "|" + "-" * (len(head) - 2) + "|"]
    for r in rows:
        out.append(
            f"| {r['issue']:<3} | {r['metric']:<{w_metric}} "
            f"| {str(r.get('before','')):<{w_b}} | {str(r.get('after','')):<{w_a}} "
            f"| {r.get('world','')} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "after"], default="baseline")
    ap.add_argument("--port", type=int, default=9444)
    ap.add_argument("--keep-brave", action="store_true",
                    help="don't kill the isolated Brave on exit")
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    have_fixture = ensure_fixture()
    live: dict | None = None
    if have_fixture and driver.driver_available():
        try:
            print("launching isolated Brave on the shadow fixture…", flush=True)
            live = launch_isolated_brave(args.port)
            print(f"  pid={live['pid']} window={live['window_id']} "
                  f"target={live['target']['id'][:8]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  live setup failed ({e}); web rows degrade to n/a", flush=True)
    else:
        print("fixture/driver unavailable — deterministic rows only", flush=True)

    rows: list[dict] = []
    rows += measure_issue10(live)
    rows += measure_issue8(live)
    rows += measure_issue9(live)

    table = render(rows)
    print()
    print(table)

    out = RESULTS_DIR / f"improve_{args.mode}.json"
    out.write_text(json.dumps({"mode": args.mode, "rows": rows}, indent=2))
    print(f"\nsaved: {out}")

    if live and not args.keep_brave and live.get("pid"):
        try:
            # pid-only kill: terminates ONLY the isolated instance we spawned,
            # never the user's working Brave (different pid + user-data-dir).
            driver.call("kill_app", {"pid": live["pid"]})
        except driver.DriverError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
