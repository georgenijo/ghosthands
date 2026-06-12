# Web apps & Electron — browser specifics

Supplement to `SKILL.md` (read that first). Browsers (Brave/Chrome/Chromium,
Safari) and Electron apps (VS Code, Slack, …) follow the same snapshot→act→
verify loop, with the quirks below. Every one of these was observed live on
this machine.

## 1. Navigation

- **URL navigation = `launch_app({bundle_id, urls})`.** Never `⌘L`, never
  typing into the omnibox — a backgrounded pid can't supply the "user-typed"
  commit signal, so the URL lands but Return no-ops.
- **The new tab opens in whichever window of that browser was last focused —
  not necessarily the one you were watching.** After any `urls` navigation,
  re-run `list_windows({pid})` and pick the window whose TITLE matches the
  destination page. Do not assume the previous `window_id` is the one that
  navigated.
- Window titles are `(<badge counts>) <page title> - <browser>` — match on a
  substring of the expected page title, not equality.

## 2. Windows over tabs

You cannot enumerate tabs over AX, and tab-switch hotkeys (`⌘1..9`, `⌘]`) are
banned (they visibly flip tabs). Treat each browser WINDOW as the unit: find
the window whose title shows the page you need. If the page is in a
background tab of some window, navigate to the URL again with `launch_app` —
the browser will surface/reuse it — rather than hunting tabs.

## 3. The web AX tree

- **Sparse-tree retry.** Chromium builds its accessibility tree lazily; the
  first snapshot after navigation can be near-empty (chrome UI only, zero
  links). Wait ~2s and re-snapshot once before concluding anything about the
  page.
- A full page tree is large (500–1000 elements). `query` filters the rendered
  markdown without losing the index cache — filter, but remember the indices
  you get are still valid for the whole window.
- Duplicate AXWindow subtrees (SKILL.md §4) happen on browser windows too —
  the same button can appear at two indices; use whichever resolves.

## 4. Stale control labels — verify by state that must MOVE

Rich web players (YouTube, etc.) update control labels lazily in the AX tree.
Observed: video autoplaying while the button still read `Play (k)`; clicking
"Play" actually paused it.

**Never trust a toggle's label as evidence of state.** Verify with a value
that must change over time or as a result of the action:
- video playing → elapsed-time text advances between two snapshots a few
  seconds apart;
- form submitted → page/window title or a result element changed;
- toggle flipped → re-snapshot and read the dependent state, not the button.

## 5. Autoplay and media

Background-tab autoplay is unreliable (user-gesture policies). An AX press on
the player's play control counts as a gesture and works on a backgrounded,
unfocused window. Verify per §4 (clock advance), then leave focus alone — the
no-foreground contract holds; audio plays from background tabs fine.

## 6. The `page` escape hatch

When AX is insufficient (canvas-rendered content, virtualized lists that
don't materialize, missing roles), the `page` tool gives browser-DOM access
(Chromium/Safari/Electron): query the DOM, run JS, click DOM nodes. Order of
preference: AX `element_index` → `page` DOM → tell the user it needs a
foreground/pixel mode. Never pixel-guess silently.

## 7. Minimized windows

Key events to a minimized browser window no-op with a system beep (the commit
never lands). Use `set_value` on the field or AX-press the button instead, or
operate on a non-minimized window.
