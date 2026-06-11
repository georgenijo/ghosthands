# GhostHands — Model-Agnostic Local Computer-Use Harness

> **Status:** Design doc for implementation. Project name **GhostHands** (`ghosthands`).
> **Tagline:** Invisible hands any AI model can borrow to operate your real Mac — locally, in the background, without stealing your cursor.
> **Audience:** A fresh engineering agent with no prior context on this project. This doc is self-contained — everything you need is here or linked.
> **Platform:** macOS (Apple Silicon, macOS 14+). Single-user local machine.

---

## 1. What we are building (one paragraph)

A local tool that lets an AI model **operate the real macOS desktop** — click, type, read native app UIs — where the **model brain is swappable**. The same "hands" work whether the brain is Anthropic's Claude or OpenAI's GPT/Codex. The tool detects which brains the user has authorized on the machine and routes to the best available one. Goal: native-app + browser automation that is faster and more capable than DOM-only browser automation, runs locally, and costs nothing beyond an existing AI subscription when possible.

## 2. The core idea: brain slot + shared hands

```
          ┌──── brain slot ────┐
  goal →  │  Claude  OR  GPT   │  →  Cua Driver (hands)  →  macOS apps
          └────────────────────┘
                       shared hands, swappable brain
```

- **Hands = Cua Driver** (open-source, MIT). It does screenshot capture, reads the macOS Accessibility (AX) tree, and performs clicks/typing/keys. It is **brain-agnostic by design** — it exposes its capabilities as an **MCP server** and takes commands like "click element 240"; it does not care which model decided that.
- **Brain = whichever model.** The brain reads the current screen state (AX tree and/or screenshot), decides the next action, and tells the hands to do it.

Because the hands are already model-agnostic, model-agnosticism is the *natural* design, not extra work.

## 3. The cost rule (critical — drives the architecture)

There are two ways to attach a brain, with different billing:

| Mode | Brain runs as | Billing |
|------|---------------|---------|
| **A — agent-as-brain** | The vendor's own agent app (Claude Code CLI, or Codex CLI) calls Cua over MCP | Covered by that app's **subscription** (Claude plan / ChatGPT plan). ~Free. |
| **B — own loop** | Our own program calls the model's HTTP API directly | **Pay-per-call API token.** Not covered by subscriptions. |

**Rule:** a subscription only pays when the brain is the vendor's own agent app. A custom standalone program must use a metered API key.

**Build Track A first** (proven, subscription-covered). Track B is an optional extension for a standalone, no-agent-UI product.

## 4. Background facts you need (verified)

- **Cua Driver** is the `trycua/cua` project's host-control component (`cua-driver-rs`, MIT). It drives the **real local desktop** (not a VM) via Accessibility + ScreenCaptureKit, **without stealing focus** (acts on backgrounded windows). It is explicitly "for any agents, any MCP client."
- It exposes **~33 MCP tools** (screenshot, AX snapshot, click, type, key, scroll, launch_app, browser-DOM fallback, etc.). Prefer **`element_index`** (AX) actions over pixel `x,y` — they work on hidden/background windows and need no Screen Recording permission.
- **OpenAI Codex CLI has NO native computer use** — confirmed at source level (only a feature flag, no GUI primitives). Codex gets computer use the *same way we will*: by calling Cua as an MCP server. So "GPT brain + Cua" is just Codex CLI consuming the Cua MCP server. There is **no headless GPT-native desktop control** to depend on.
- **Proven already:** on the target machine, Claude (via Claude Code) drove macOS Calculator end-to-end through Cua — read the AX tree (262 elements, no screenshot), clicked `7 × 6 =`, verified `42`. The model-agnostic premise is validated for the Claude side; GPT side is the same MCP wiring.

## 5. Environment already set up on the target machine

Do **not** re-do these; verify and build on them:

- Cua Driver installed: binary at `~/.local/bin/cua-driver` → `/Applications/CuaDriver.app/Contents/MacOS/cua-driver` (version 0.5.x, **prerelease**).
- Registered as an MCP server in Claude Code under name `cua-computer-use` (compat mode):
  `cua-driver mcp --claude-code-computer-use-compat`
- macOS **Accessibility** permission: **granted** to the CuaDriver daemon identity (`com.trycua.driver`).
- macOS **Screen Recording** permission: **NOT yet granted** (needed only for screenshots / vision fallback / the visual agent-cursor overlay; the AX action path does not need it).

Install command (if reinstalling): `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"`

## 6. Components to build

### Track A (MVP, recommended) — agent-as-brain over shared Cua

1. **Brain selector** (`select-brain`): detects which brains are authorized and prints/launches the best one.
   - Check Claude Code present + logged in (`claude` on PATH, has a usable session).
   - Check Codex CLI present + logged in (`codex` on PATH, signed in to a ChatGPT plan).
   - Optional fallback: presence of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (→ Track B).
   - Output: a chosen brain + the command to launch it wired to Cua. Priority order configurable (default: subscription brains before API brains).
2. **Cua wiring per brain.** Ensure Cua is registered as an MCP server in each target agent:
   - Claude Code: `claude mcp add --transport stdio cua-driver -- ~/.local/bin/cua-driver mcp` (or the compat variant already present).
   - Codex CLI: `codex mcp add cua-driver -- ~/.local/bin/cua-driver mcp` (Codex consumes MCP servers via `~/.codex/config.toml` `[mcp_servers.*]`).
3. **Operating instructions / skill** (`SKILL.md` + `MACOS.md`): a model-agnostic prompt that teaches *any* agent the reliable Cua workflow. Must encode:
   - The per-turn loop: `get_window_state(pid, window_id)` → act by `element_index` → `get_window_state` again to verify.
   - **No-foreground contract:** prefer AX element actions; do not steal focus; navigate URLs via `launch_app({bundle_id, urls})` not address-bar hotkeys.
   - "Use APIs before clicking" (if other MCP tool servers like email/calendar are connected, prefer them; click only when no API exists — clicking is slower and more fragile).
   - Draft-before-send / stop-before-destructive guardrails.
4. **Hardening wrapper / "doctor"** (`cua-doctor`): handles the known prerelease gotchas (see §8) — a persistent daemon, a retry shim, and a permission checker.
5. **Selector + launcher CLI** that ties 1–4 together: `cua-pilot run "<goal>" [--brain claude|gpt|auto]`.

### Track B (extension) — standalone own-loop, brain-swappable via API

A small program implementing the loop directly, for a no-agent-UI product:

```
loop:
  state   = cua.get_window_state(pid, window_id)        # AX tree (+ optional screenshot)
  actions = brain.decide(goal, state, history)          # one model call
  for a in actions: cua.<click|type|key>(a)             # execute via Cua
  if done(state): break
```

- Define a single **`Brain` interface**: `decide(goal, state, history) -> list[Action]`.
- Provide two adapters implementing it: `AnthropicBrain` (Claude messages API, supports its computer-use tool shape) and `OpenAIBrain` (GPT/`computer-use-preview` or function-calling). Swap via config `brain = claude | gpt`.
- Talk to Cua over its MCP/stdio or its raw socket. **Uses API tokens (pay-per-call).**

Both tracks share: the Cua hands, the operating instructions, the benchmark harness (§9), and the selector.

## 7. Cua Driver tool reference (the ones you will use)

Call `cua-driver list-tools` for the full set. Key tools:

- `start_session(session)` / `end_session(session)` — declare a named run; drives the per-run colored agent cursor (cursor overlay needs Screen Recording — see gotchas). Optional; omit `session` to run cursor-less.
- `launch_app({bundle_id|name, urls?})` — launches **in background**; returns pid + windows.
- `list_windows({pid})` — find the on-screen `window_id` (pick `is_on_screen: true`).
- `get_window_state({pid, window_id, capture_mode})` — AX tree as Markdown with `[element_index N]` tags. `capture_mode`: `ax` (no screenshot, no Screen Recording needed), `som` (AX + screenshot), `vision` (screenshot only). **Optional `query` filters the rendered text but still refreshes the full index cache.**
- `click({pid, window_id, element_index})` — AX press, no focus steal. Use `x,y` only for canvas/WebGL surfaces not in the AX tree.
- `type_text`, `press_key`, `hotkey`, `scroll`, `drag`, `set_value`.
- `page(...)` — browser DOM access (Chromium/Safari/Electron) when AX is insufficient.
- `check_permissions({prompt})` — report/raise Accessibility + Screen Recording grants.

## 8. Known gotchas you MUST handle (learned the hard way)

1. **AX index cache expires (~seconds).** The `element_index` map from `get_window_state` is short-lived and is replaced by the next snapshot. **Re-snapshot immediately before each action** (or batch snapshot→act with minimal gap). Acting on a stale index returns `Element index N not found in cache` — recover by re-snapshotting.
2. **Multiple AXWindow nodes per app.** A snapshot may render two `AXWindow` subtrees with different index ranges (e.g. buttons at `5–20` *and* `240–258`). The `(pid, window_id)` cache only holds the indices for *that* window. Always target the **on-screen** `window_id` from `list_windows` and use the index set that resolves for it; if a click says "not found," re-snapshot and use the other set.
3. **Agent-cursor clicks fail without Screen Recording.** Clicking *with* a `session` draws a cursor overlay that needs Screen Recording to composite; without it, clicks throw `daemon transport error: Resource temporarily unavailable (EAGAIN)`. **Run cursor-less (omit `session`) until Screen Recording is granted.**
4. **Prerelease daemon flakiness.** Occasional `daemon closed connection without response` / EAGAIN on action calls — the action often still lands. **Wrap action calls in a retry** (re-snapshot + retry once or twice). Consider running a persistent daemon with `--autostart` for stability.
5. **TCC permission attribution.** macOS binds Accessibility/Screen Recording grants to the *responsible process*. Grant via `cua-driver permissions grant` (launches CuaDriver so the grant sticks to the driver's own identity, not the launching terminal). Verify with `cua-driver permissions status` / the `check_permissions` tool (`source` field must read `driver-daemon`).
6. **Retina coordinates.** If you ever fall back to pixel `x,y`, account for the display `scale_factor` from `get_screen_size`. (AX `element_index` actions avoid this entirely — prefer them.)

## 9. Benchmark harness (compare brains fairly)

Purpose: measure **Claude-brain vs GPT/Codex-brain on the same Cua hands** — and against a DOM-only baseline — for speed, success, and steps.

Design:
- **Task suite.** Each task ships a machine-checkable **done-detector** (a function returning true when the task actually succeeded — file exists, a setting value flipped, a web entity state changed via API). Include a mix of native-app and browser tasks; at least one task all contenders can attempt.
- **Fair timing.** Run every brain in **full-auto / no-prompts** mode so human-approval pauses don't pollute the clock (Codex: `--dangerously-bypass-approvals-and-sandbox` / `--yolo`; Claude Code: pre-approved tools / permission mode). Pre-grant all OS permissions.
- **Clock.** Start when the goal is dispatched; stop when the done-detector returns true (external wall-clock — **note:** `codex exec --json` carries no per-action timestamps, so external timing is required, not optional).
- **Robustness.** Run each task **N≥5 times**, report the **median** plus spread (model output varies run-to-run).
- **Metrics per (brain × task):** median wall-clock, success rate, action/step count, and (if available) token/usage cost.
- **Output:** a comparison table `brain × task → median seconds, success %, steps, cost`.

## 10. Acceptance criteria (definition of done for MVP)

- [ ] `cua-pilot run "<goal>"` drives a **native macOS app** end-to-end via Cua, brain = Claude (subscription), and verifies the result.
- [ ] Same goal runs with brain = **GPT/Codex** (ChatGPT subscription) by swapping only the brain — no change to the hands or instructions.
- [ ] **Selector** auto-detects authorized brains and picks one; manual override works (`--brain`).
- [ ] **Hardening** in place: cursor-less default, action retry on EAGAIN, re-snapshot-on-stale-cache, permission doctor.
- [ ] **One browser task** works (AX or the `page` DOM tool).
- [ ] **Benchmark harness** runs the task suite across both brains and emits the comparison table.

## 11. Out of scope (do not build)

- Training or fine-tuning any model. (Brains are off-the-shelf; this tool only orchestrates.)
- Billing/paywall/metering, voice I/O, a notch/HUD UI. (This is a developer tool, not a consumer app.)
- A cloud backend or key-vending proxy. Everything runs locally; subscriptions/keys stay on the user's machine.
- Reimplementing the hands. Cua Driver is the hands; if it regresses, the fallback is another host-control MCP server (e.g. `mediar-ai/mcp-server-macos-use`), not a rewrite.

## 12. References

- Cua project: <https://github.com/trycua/cua> (MIT). Cua Driver docs: <https://cua.ai/docs/cua-driver>, tool reference: <https://cua.ai/docs/cua-driver/reference/mcp-tools>.
- Fallback host-control MCP server: <https://github.com/mediar-ai/mcp-server-macos-use>.
- Codex CLI computer-use status: feature request <https://github.com/openai/codex/issues/20851> (open — no first-class CLI computer use); GUI-tools fork <https://github.com/openai/codex/issues/16666> (closed). Codex consumes MCP servers via `~/.codex/config.toml` `[mcp_servers.*]`.
- MCP (Model Context Protocol): the standard mechanism for exposing tools to an agent; Cua and any brain communicate over it.

---

### Implementation order (suggested)

1. `cua-doctor` (verify install, grant/verify permissions, start persistent daemon, retry shim).
2. Operating instructions skill (`SKILL.md` + `MACOS.md`) encoding the §8 workflow.
3. Wire Cua into Claude Code and Codex CLI; confirm both can drive Calculator (the proven smoke test).
4. `select-brain` selector + `cua-pilot run` launcher.
5. Benchmark harness + task suite.
6. (Optional) Track B standalone loop with `Brain` adapters.
