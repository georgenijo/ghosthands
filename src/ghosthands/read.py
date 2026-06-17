"""The no-brain read tier — dump the AX tree, grab a screenshot, find a name.

Issue #35. The symmetric half of record/replay: just as flows act with NO
model, these verbs *read* with no model. Everything here is deterministic,
instant, and $0 — it goes straight to `driver.call` / `actions.App` and never
imports the model stack.

ACCEPTANCE CONSTRAINT: this module imports NEITHER `ownloop` NOR `mlx`
(enforced by tests/test_read_no_model.py). Keep the read path stdlib-only:
`actions` (App/Snapshot), `ax`, and `driver` are all model-free; do not pull
in anything that drags `mlx` along.
"""

from __future__ import annotations

import json
import sys
import time

from . import ax, driver
from .actions import App, GhostHandsError, Snapshot

WATCH_POLL_SECONDS = 0.5
# A cold or just-launched/name-bound window can hand back a SPARSE AX tree a
# beat before it fills in — a present element then looks absent. Settle and
# re-read once on an empty/missed snapshot so a read verb never false-negatives
# on a window it only just resolved. (The act path already re-snapshots on a
# miss; the read verbs inherit the same guard here.)
SETTLE_SECONDS = 0.4


def _snapshot_settled(app: App, *, query: str | None = None) -> Snapshot:
    """One snapshot; if the tree comes back empty (a sparse cold read), settle
    and re-snapshot once before returning it."""
    snap = app.snapshot(query=query)
    if not snap.elements:
        time.sleep(SETTLE_SECONDS)
        snap = app.snapshot(query=query)
    return snap


def resolve_target(spec: str, *, title_contains: str | None = None,
                   urls: list[str] | None = None) -> App:
    """The SHARED resolver for every read verb. Accepts, in order:

    - a bare PID (all digits) -> bind to its on-screen window via list_windows;
    - a bundle id (contains a dot) -> App.launch (background, idempotent);
    - otherwise a process NAME -> launch_app({name}) (cua resolves it), then
      bind the on-screen window; falls back to a list_apps/list_windows lookup
      for an already-running app that launch didn't surface a window for.

    Returns a bound `App` (pid, window_id) with the on-screen window picked by
    App._pick_window. Raises GhostHandsError when nothing on-screen is found.
    """
    spec = spec.strip()
    try:
        if spec.isdigit():
            return _bind_pid(int(spec), title_contains=title_contains)
        if "." in spec:
            return App.launch(spec, urls=urls, title_contains=title_contains)
        return _resolve_name(spec, title_contains=title_contains, urls=urls)
    except driver.DriverError as e:
        # A bad bundle id / unknown name surfaces as a driver error; translate
        # it to the GhostHandsError every read verb already handles, so the
        # caller prints one clean line instead of a traceback.
        raise GhostHandsError(f"could not resolve {spec!r}: {e}") from e


def _bind_pid(pid: int, *, title_contains: str | None = None) -> App:
    listed = driver.call("list_windows", {"pid": pid})
    windows = listed["windows"] if isinstance(listed, dict) else listed
    window = App._pick_window(windows or [], title_contains)
    if window is None:
        raise GhostHandsError(
            f"pid {pid}: no on-screen window"
            + (f" titled ~{title_contains!r}" if title_contains else "")
            + " (is the app running with a visible window?)")
    name = window.get("app_name") or ""
    return App(pid, window["window_id"], name=name)


def _resolve_name(name: str, *, title_contains: str | None = None,
                  urls: list[str] | None = None) -> App:
    args: dict = {"name": name}
    if urls:
        args["urls"] = urls
    result = driver.call("launch_app", args)
    pid = result.get("pid")
    app_name = result.get("name", name)

    window = App._pick_window(result.get("windows", []), title_contains)
    for _ in range(10):
        if window is not None:
            break
        time.sleep(0.4)
        listed = driver.call("list_windows", {"pid": pid})
        windows = listed["windows"] if isinstance(listed, dict) else listed
        window = App._pick_window(windows or [], title_contains)
    if window is None:
        raise GhostHandsError(
            f"{name!r} (pid {pid}): no on-screen window"
            + (f" titled ~{title_contains!r}" if title_contains else "")
            + " appeared")
    return App(pid, window["window_id"], name=app_name)


# -- snapshot ----------------------------------------------------------------

def snapshot(spec: str, *, fmt: str = "ax", query: str | None = None,
             title_contains: str | None = None,
             out=sys.stdout) -> int:
    """Dump the AX tree of `spec`'s on-screen window. NO model.

    fmt: "ax" -> markdown tree (default); "json" -> parsed elements as JSON.
    Returns a process exit code (0 ok).
    """
    app = resolve_target(spec, title_contains=title_contains)
    snap = _snapshot_settled(app, query=query)
    print(_render(snap, fmt), file=out)
    return 0


def watch(spec: str, *, fmt: str = "ax", query: str | None = None,
          title_contains: str | None = None, out=sys.stdout) -> int:
    """Re-dump the AX tree whenever it changes (Ctrl-C to stop). NO model."""
    app = resolve_target(spec, title_contains=title_contains)
    last: str | None = None
    print(f"# watching {app.name or spec} (pid {app.pid}, window {app.window_id}) "
          f"— Ctrl-C to stop", file=sys.stderr)
    try:
        while True:
            try:
                snap = app.snapshot(query=query)
            except driver.TransientDriverError:
                time.sleep(WATCH_POLL_SECONDS)  # daemon flaked; it often recovers
                continue
            if snap.markdown != last:
                last = snap.markdown
                print(f"# --- {time.strftime('%H:%M:%S')} ---", file=out)
                print(_render(snap, fmt), file=out)
                out.flush()
            time.sleep(WATCH_POLL_SECONDS)
    except KeyboardInterrupt:
        return 0


def _render(snap: Snapshot, fmt: str) -> str:
    if fmt == "json":
        return json.dumps([_element_dict(el) for el in snap.elements], indent=2)
    return snap.markdown


def _element_dict(el: ax.Element) -> dict:
    return {
        "index": el.index,
        "role": el.role,
        "text": el.text,
        "title": el.title,
        "label": el.label,
        "value": el.value,
        "ax_id": el.ax_id,
        "actions": el.actions,
        "depth": el.depth,
        "subtree": el.subtree,
    }


# -- shot --------------------------------------------------------------------

def shot(spec: str, path: str, *, title_contains: str | None = None,
         out=sys.stdout) -> int:
    """Screenshot `spec`'s window to `path` via cua's OWN ScreenCaptureKit
    grant — works even when the calling shell lacks Screen Recording, since the
    CuaDriver daemon holds the grant. Fails GRACEFULLY (one clear line,
    non-zero exit, no traceback) when no process can capture the screen.
    """
    if not driver.driver_available():
        print("shot failed: cua-driver not found "
              f"(expected {driver.DRIVER_BIN})", file=sys.stderr)
        return 1

    # Probe the live ScreenCaptureKit capability of the daemon (the process
    # that does the work), not the shell. Skip the prompt — a read verb must
    # never raise a system dialog.
    try:
        perms = driver.call("check_permissions", {"prompt": False})
    except driver.DriverError as e:
        print(f"shot failed: could not query permissions ({e})", file=sys.stderr)
        return 1
    if isinstance(perms, dict) and perms.get("screen_recording_capturable") is False:
        print("shot failed: Screen Recording is not granted to CuaDriver — "
              "grant it in System Settings ▸ Privacy & Security ▸ Screen Recording "
              "(the AX tree still works: try `ghosthands snapshot`)",
              file=sys.stderr)
        return 1

    try:
        app = resolve_target(spec, title_contains=title_contains)
    except GhostHandsError as e:
        print(f"shot failed: {e}", file=sys.stderr)
        return 1

    try:
        result = driver.call("get_window_state", {
            "pid": app.pid,
            "window_id": app.window_id,
            "capture_mode": "vision",          # screenshot only, no AX walk
            "screenshot_out_file": path,
        })
    except driver.DriverError as e:
        print(f"shot failed: {e}", file=sys.stderr)
        return 1

    written = (result or {}).get("screenshot_file_path") if isinstance(result, dict) else None
    written = written or path
    print(f"wrote {written}", file=out)
    return 0


# -- find --------------------------------------------------------------------

def find(name: str, spec: str, *, title_contains: str | None = None,
         out=sys.stdout) -> int:
    """Resolve a NAME to an element in `spec`'s window and print its role /
    on-screen flag / index + coords. Exit 0 if found, non-zero if not. NO
    model — reuses Snapshot.find_all.
    """
    try:
        app = resolve_target(spec, title_contains=title_contains)
    except GhostHandsError as e:
        print(f"find failed: {e}", file=sys.stderr)
        return 1  # match snapshot's exit code for a GhostHandsError
    # actionable=False so a static-text / non-clickable match still reports.
    snap = app.snapshot()
    matches = snap.find_all(name, actionable=False)
    if not matches:
        # A cold / just-bound window can yield a sparse tree where a present
        # element looks absent — settle and re-check once before declaring miss.
        time.sleep(SETTLE_SECONDS)
        snap = app.snapshot()
        matches = snap.find_all(name, actionable=False)
    if not matches:
        print(f"not found: {name!r} in {app.name or spec} "
              f"(pid {app.pid}, window {app.window_id})", file=sys.stderr)
        return 1
    el = matches[0]
    on_screen = el.index is not None  # actionable nodes carry an element_index
    print(f"{name!r}: role={el.role} on_screen={on_screen} "
          f"index={el.index} ax_id={el.ax_id!r} text={el.text!r}"
          + (f"  (+{len(matches) - 1} more)" if len(matches) > 1 else ""),
          file=out)
    return 0
