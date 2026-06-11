# AGENTS.md — Instructions for the implementing agent

You are implementing **GhostHands**, a model-agnostic local macOS computer-use harness. This file is your entry point. Read it fully, then read `DESIGN.md` (the complete spec) and `ROADMAP.md` (the task order) before writing code.

## What this project is (1 paragraph)

A local tool that lets an AI model operate the real macOS desktop (click, type, read native app UIs) where the **model brain is swappable** (Claude ⇄ GPT/Codex) over a single shared "hands" layer (Cua Driver, an MCP server). It detects which brains the user has authorized and routes to the best available one. See `DESIGN.md` for the architecture, the cost rule, and acceptance criteria.

## Before you write any code

1. Read `DESIGN.md` end to end. Pay special attention to:
   - **§3 The cost rule** — subscription (agent-as-brain) vs API token (own loop). Build the subscription track (Track A) first.
   - **§8 Known gotchas** — these are real failures already hit on this machine. Handle them from day one; do not rediscover them.
2. Read `ROADMAP.md` and work the milestones in order.
3. Verify the environment (see below) before assuming anything is broken.

## Environment already set up (verify, don't redo)

- Cua Driver installed: `~/.local/bin/cua-driver` → `/Applications/CuaDriver.app/Contents/MacOS/cua-driver` (v0.5.x, **prerelease — pin and expect churn**).
- macOS **Accessibility** permission: granted to the driver daemon identity (`com.trycua.driver`).
- macOS **Screen Recording** permission: **NOT granted yet**. Needed only for screenshots / vision fallback / the visual cursor overlay. The AX action path (what you'll use most) does **not** need it.
- Cua may already be registered in Claude Code as MCP server `cua-computer-use`. Re-registering for a clean `cua-driver` name is fine.

Quick verification commands:
```bash
~/.local/bin/cua-driver --version
~/.local/bin/cua-driver permissions status   # Accessibility should be true; source = driver-daemon
~/.local/bin/cua-driver list-tools
```

## The operating contract is already written

`skills/SKILL.md` is the canonical computer-use operating contract — adapted from the proven `trycua/cua` `cua-driver` skill (MIT, see `ATTRIBUTION.md`) and from failures observed in practice. **Read it.** M1 is largely drafted there; refine and add `WEB_APPS.md` / `TESTS.md`, don't write the rules from scratch. The summary below is the essentials; SKILL.md is authoritative.

## The golden workflow (encode this everywhere)

Every interaction with an app follows this loop. It is the single most important thing to get right:

1. `launch_app({bundle_id})` → returns pid + windows. Launches in the **background** (no focus steal).
2. `list_windows({pid})` → pick the window with `is_on_screen: true`. That `window_id` is your target.
3. `get_window_state({pid, window_id, capture_mode: "ax"})` → Markdown AX tree with `[element_index N]` tags. **No screenshot needed.**
4. Act by `element_index`: `click` / `type_text` / `press_key` / `hotkey`.
5. `get_window_state(...)` again → verify the action landed (read the changed value).

**Re-snapshot before every action** — the element-index cache expires after a few seconds (see gotchas).

## Hard rules (from DESIGN §8 — non-negotiable)

- **Prefer `element_index` (AX) over pixel `x,y`.** AX works on background/hidden windows and needs no Screen Recording. Use pixels only for canvas/WebGL surfaces absent from the AX tree.
- **Re-snapshot before each action.** Stale index → `Element index N not found in cache`. Recover by re-snapshotting; never give up after one miss.
- **Run cursor-less until Screen Recording is granted.** Clicking *with* a `session` draws a cursor overlay that needs Screen Recording; without it you get `EAGAIN` transport errors. Omit `session`.
- **Handle multiple AXWindow subtrees.** A snapshot can render two windows with different index ranges; target the on-screen `window_id` and use the index set that resolves for it.
- **Retry on daemon flakiness.** `daemon closed connection without response` / `EAGAIN` happen on this prerelease build; the action often still landed. Wrap action calls: re-snapshot + retry once or twice, then verify state.
- **No-foreground contract.** Don't steal focus. Navigate URLs via `launch_app({bundle_id, urls})`, not address-bar hotkeys.
- **APIs before clicking.** If other MCP tool servers exist for a task (email, calendar, etc.), use them; click only when no API exists.
- **Guardrails.** Draft before send; stop before destructive/irreversible actions.

## Conventions

- Keep it **local-only**. No cloud backend, no telemetry, no key proxy.
- Language: pick per component, but a thin CLI (`ghosthands ...`) is the user-facing surface. Document any runtime deps in `README.md`.
- Source in `src/`, operating-instruction skills in `skills/`, benchmark suite in `bench/`, extra docs in `docs/`.
- Pin the Cua Driver version you test against; record it in `README.md`.
- Intended license: MIT.

## Definition of done (MVP)

See `DESIGN.md` §10. In short: drive a native app via Cua with brain=Claude (subscription), swap to brain=GPT/Codex with no change to hands/instructions, a selector auto-picks the authorized brain, hardening is in place, one browser task works, and the benchmark harness emits a comparison table.

## Out of scope

Training/fine-tuning models; billing/paywall; voice or HUD UI; cloud backend; reimplementing the hands. See `DESIGN.md` §11.
