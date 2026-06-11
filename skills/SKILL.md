# GhostHands — Computer-Use Operating Instructions

How any AI brain should drive the macOS desktop through the Cua Driver MCP server. Read this before acting. These rules are adapted from the proven `trycua/cua` `cua-driver` skill (MIT) and from failures observed in practice — follow them exactly; they are the difference between reliable control and silently-dropped actions.

Companion docs: `WEB_APPS.md` (browser/Electron specifics), `TESTS.md` (verification scenarios).

---

## 1. The no-foreground contract (the core rule)

**The user's frontmost app MUST NOT change.** GhostHands acts on background/hidden windows without stealing the cursor, focus, or Space. Everything else follows from this.

Before any command, self-check three questions:
1. Does this **raise / activate / foreground** an app? → don't.
2. Does this **move the real cursor**? → don't.
3. Does this **bypass the computer-use MCP** (shell GUI automation)? → don't.

If any answer is yes — or you can't tell — **stop and pick an AX path instead.**

**Banned operations** (all foreground or leak input):
- Every `open` CLI form — `open -a`, `open -b`, `open <file>`, `open <url>`, `open .../MyApp.app` (all route through LaunchServices and foreground). Exception: `open`/`/usr/bin/open` is allowed *only* to reveal a finished file/URL for the user to view — never to launch/navigate/automate.
- `osascript … activate`/`launch`/`open`, `cliclick`, raw `CGEventPost` at the HID tap over another app, `NSRunningApplication.activate`, Dock clicks, Cmd-Tab.
- Focus-stealing shortcuts even to a backgrounded pid: browser `⌘L` (omnibox), Finder `⌘⇧G`. Rule of thumb: a shortcut that says *"put my cursor here"* steals focus; one that says *"do this"* (copy, save, quit) is fine.
- Browser tab-switching (`⌘1..9`, `⌘]`, `⌘[`) — visibly flips tabs even backgrounded.

**Stop before irreversible actions** (purchase, send, delete, payment, account change, form submit) unless the user explicitly approved that exact step.

## 2. Launching: `launch_app` is the only launch primitive

- `launch_app({bundle_id})` is **idempotent** (no-op if running), launches in the **background**, and returns **pid + `windows` array in one call**. It has an internal focus-restore guard, so it's safe even for apps that foreground on load (browsers, players).
- "Open <app>" means **launch, not activate**. For a just-built `.app`, resolve its `CFBundleIdentifier` from `Info.plist` and pass that — never `open` the path.
- URL navigation = `launch_app({bundle_id: <default_browser>, urls: [...]})`. Never `⌘L`+type, never `set_value`/`type_text` a URL into the omnibox (a backgrounded pid can't supply the "user-typed" commit signal — the URL lands but Return no-ops).

## 3. The core loop: snapshot → act → snapshot (both are mandatory)

```
launch_app(target)            → pick window_id from returned `windows` (is_on_screen: true)
get_window_state(pid, win_id) → BEFORE: resolves element_index for this turn
[act]  (click / type_text / press_key / hotkey)   — every action takes (pid, window_id)
get_window_state(pid, win_id) → AFTER: verify the action landed (read the changed value)
```

- **The AFTER snapshot is not optional.** The AX-tree diff (new value, new window, disabled button) is your *only* evidence the action fired. Skipping it → reporting success on a silently-dropped action — the single most common failure mode. If nothing changed, say so; do not paper over with "done."
- **Window selection is your job.** Pass the on-screen `window_id` from `list_windows`; don't trust a largest-area heuristic (off-screen utility panels can out-area the visible window).

## 4. Element indices — how the cache actually works (read this; it bites)

- `get_window_state` returns an AX tree where each actionable element has an `[element_index N]` tag. Act by `element_index`, not pixels.
- **The index map is keyed on `(pid, window_id)` and is REPLACED on every snapshot.** Consequences, all observed in practice:
  - Indices from turn N do **not** resolve in turn N+1 → **re-snapshot before every action.**
  - An app may render **two `AXWindow` subtrees** with different index ranges (e.g. buttons at `5–20` *and* `240–258`). Only the indices belonging to the `window_id` you snapshotted will resolve. If a click says `element index N not found`, you used the wrong window's set — re-snapshot the on-screen `window_id` and use the indices that resolve for it.
  - The cache is short-lived; a few seconds of idle can expire it. Recover by re-snapshotting — never give up after one miss.
- **`element_index` is primary** because it works on hidden/occluded/minimized/off-Space windows, steals no focus, and survives rebuilds. Use pixel `x,y` only for canvas/WebGL surfaces absent from the AX tree (see §7).
- The `actions=[...]` list per element is **advisory** — try the action; pivot on the returned AX error, not on what was advertised.

## 5. Capture modes

`get_window_state` `capture_mode`: `som` (AX tree + screenshot, default), `ax` (tree only — **no Screen Recording needed**), `vision` (screenshot only). Reason over both when you have them: **AX tree = what's clickable; screenshot = which one.** Prefer `ax` for speed and to stay light on usage when you don't need pixels. A `query` arg filters the rendered text but still refreshes the full index cache.

## 6. Menu bar navigation

- **Only drive the menu bar when the target app is frontmost.** Backgrounded apps' document/editor/playback menu items go *disabled* (AXPick/AXPress returns success at the AX layer but no-ops), and the on-screen menu always belongs to the frontmost app (renders over the wrong app). For backgrounded targets, use in-window controls / `element_index` / keyboard shortcuts instead.
- Two-snapshot menu flow (frontmost only): find `[N] AXMenuBarItem` → `click({pid, element_index: N, action: "pick"})` (menu items implement **AXPick**, not AXPress) → re-snapshot (items now appear as `AXMenuItem`) → click target (AXPress) → re-snapshot/verify. `press_key({key: "escape"})` to back out; never leave a menu open between turns (it poisons later snapshots for that pid).

## 7. Surfaces that have no safe background path

Canvas/viewport apps (Blender, Unity, games, WebGL, Qt/wxWidgets) expose the whole surface as one opaque `AXGroup`; per-pid synthetic events get filtered out. **Do not silently fall back to pixel-guessing.** Stop and tell the user it needs a foreground/pixel mode. (GhostHands is local-only and *may* offer an opt-in pixel/foreground mode later — but background AX is always the default.)

## 8. Error → fix quick table

| Symptom | Meaning | Fix |
|---|---|---|
| `Invalid element_index` / `No cached AX state` | skipped snapshot this turn, or wrong `window_id` | re-snapshot the same `window_id`, then act |
| `element index N not found` | indices belong to the other AXWindow subtree | re-snapshot on-screen `window_id`; use the set that resolves |
| `EAGAIN` / `daemon transport error` on a click | usually the per-session cursor overlay (needs Screen Recording) or prerelease flakiness | run **cursor-less** (omit `session`); retry once or twice — the action often still landed |
| `daemon closed connection without response` | prerelease daemon hiccup | re-verify via snapshot (action often landed); retry |
| tiny screenshot / empty `tree_markdown` | `capture_mode` is `vision` | switch to `som`/`ax` |
| sparse Chromium/Electron tree | web a11y tree still warming up | retry `get_window_state` once (see WEB_APPS.md) |
| system beep on `press_key` | minimized window — key commit no-ops | use `set_value`, or AX-click a button |
| `AXPress failed` | element wants another action | try `show_menu` / `confirm` / `cancel` / `pick` |

## 9. Permissions

- **Accessibility** (required) — element actions and reads.
- **Screen Recording** (optional) — only for screenshots (`som`/`vision`) and the visual agent-cursor overlay. The `ax` path needs neither. Grant via `cua-driver permissions grant` so the grant binds to the driver identity (`com.trycua.driver`), not the launching terminal.

## 10. Routing: prefer structured tools over clicking

Computer use is the **last-mile** path. For account-backed apps (email, calendar, notion, github, etc.), prefer a connected MCP/API tool — clicking is slower and more fragile. List the available MCP tools first; click only when no connector exists, or the user explicitly says to operate the visible UI. A screenshot showing an app is *context*, not a request to click it.

---

*Adapted from the `trycua/cua` `cua-driver` skill (MIT) and observed practice. See `../ATTRIBUTION.md`.*
