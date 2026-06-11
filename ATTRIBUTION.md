# Attribution

GhostHands is MIT-licensed.

## Cua Driver (the "hands")

The computer-use hands are [`trycua/cua`](https://github.com/trycua/cua) (`cua-driver`, **MIT**). GhostHands drives it as an MCP server; it does not vendor or modify it.

## Operating-instruction skill

`skills/SKILL.md` (and the planned `WEB_APPS.md` / `TESTS.md`) is **adapted from the `trycua/cua` `cua-driver` skill (MIT)** — the no-foreground contract, the snapshot→act→verify loop, AXMenuBar navigation, web-app handling, and the error/troubleshooting tables. Rules were rewritten in our own words and merged with behavior observed in practice. MIT requires retaining the copyright + permission notice on substantial copies; this file serves as that notice.

```
The cua-driver skill and trycua/cua are Copyright (c) trycua, MIT License.
https://github.com/trycua/cua
```

## Prior-art reference (not vendored)

The architecture was informed by a third-party static-analysis teardown of the HeyClicky app (Codex brain + Cua hands + Composio APIs). No HeyClicky/Clicky source or assets are included in GhostHands. The Hermes integration-skill catalog referenced in that teardown is MIT (Nous Research) **except** its `powerpoint.md` (proprietary) — none of it is copied here.
