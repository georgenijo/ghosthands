# CANVAS.md — canvas apps: never mouse-draw geometry

Canvas/WebGL surfaces (Excalidraw, tldraw, Figma, games, charts) have **no AX
tree for their content**. Click-drawing shapes through a model loop is the
single worst path in this stack: a measured real-world failure drew **one
rectangle in six minutes**. The rule:

> **Generate the scene as data and inject it. Never drag-draw geometry.**

## Recipe 1 — Excalidraw via localStorage (preferred, zero clicks)

Excalidraw persists its scene in `localStorage` under the key `excalidraw`
(a **raw JSON array** of element objects — not wrapped in `{elements: …}`).
Write the scene, reload, done:

```js
localStorage.setItem("excalidraw", JSON.stringify(elements));
location.reload();
```

Execute that with whatever JS escape hatch the current hands offer:
- cua-driver: the `page` tool (`page` → evaluate JS in the tab).
- agent-browser: `agent-browser eval '<js>'`.
- chrome-devtools-mcp: `evaluate_script`.

Element objects need at minimum: `id`, `type` (`rectangle`/`ellipse`/
`diamond`/`arrow`/`text`/`line`), `x`, `y`, `width`, `height`, `strokeColor`,
`backgroundColor`, `seed` (any int), `version` (1), `versionNonce` (any int).
Excalidraw's `restoreElements` repairs missing cosmetic fields, so a minimal
LLM-generated scene loads fine. For text elements include `text`,
`fontSize` (20), `fontFamily` (1).

Labels: prefer free-floating `text` elements over bound container labels —
bindings (`boundElements`/`containerId`) must be consistent both ways and are
the most common way a generated scene fails to render.

## Recipe 2 — Excalidraw via clipboard paste (when reload is unacceptable)

Excalidraw accepts a **plain-text** clipboard payload of the form:

```json
{"type": "excalidraw/clipboard", "elements": [ … ]}
```

Put it on the pasteboard (`pbcopy`), focus the canvas, send one `Cmd+V`.
One paste replaces minutes of drawing. Caveat: paste requires the page
focused — this is the one step that may need a foreground window, so prefer
Recipe 1 when running background-only.

## Recipe 3 — Mermaid text-to-diagram dialog (AX-only fallback)

Excalidraw's "More tools → Mermaid to Excalidraw" dialog IS in the AX tree.
Type Mermaid source (`graph TD; A-->B; …`), click Insert. Slower than 1/2 but
needs no JS escape hatch.

## Other canvas apps

- **tldraw**: same pattern — scene JSON in `localStorage` (`TLDRAW_DOCUMENT…`
  keys) or its `editor.createShapes(...)` API via JS eval.
- **Figma**: no DOM/localStorage path — use a plugin-bridge MCP server if one
  is connected; otherwise report the task as needing one. Never click-draw.
- **Charts/maps/games**: read state via any exposed JS API first; pixel
  clicks are the last resort and must target a *foreground* window.

## Routing rule (add to your task triage)

When the goal is "draw / diagram / sketch X" in a browser app:
1. Generate the scene as JSON/Mermaid **first** (pure model work, no tools).
2. Inject via Recipe 1 (or 2). Verify by re-reading the scene store
   (`localStorage.getItem('excalidraw')`) or a screenshot — not by your
   own memory of having drawn it.
3. Only fall back to UI interaction for the chrome *around* the canvas
   (export buttons, dialogs) — those are real AX elements.
