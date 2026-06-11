# CLAUDE.md

Project instructions live in **[AGENTS.md](./AGENTS.md)** — read it first, then `DESIGN.md` (full spec) and `ROADMAP.md` (task order).

This is GhostHands: a model-agnostic local macOS computer-use harness. The "hands" are Cua Driver (an MCP server, already installed); the brain is swappable (Claude ⇄ GPT/Codex). Build the subscription-covered track first.

Key reminders (full list in AGENTS.md §"Hard rules"):
- Prefer AX `element_index` over pixel `x,y`; re-snapshot before every action.
- Run cursor-less (omit `session`) until Screen Recording is granted, or clicks throw `EAGAIN`.
- Retry on `daemon closed connection` / `EAGAIN` — the action often still landed.
- Local-only. No cloud, no telemetry.
