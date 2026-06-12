<div align="center">

# 👻 GhostHands

### Invisible hands any AI can borrow to drive your Mac — **free, local, and without stealing your cursor.**

[![License: MIT](https://img.shields.io/badge/License-MIT-black.svg)](./LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS%2014%2B%20·%20Apple%20Silicon-black.svg)](#requirements)
[![Cost](https://img.shields.io/badge/cost-%240-brightgreen.svg)](#the-benchmark)
[![Hands: Cua Driver](https://img.shields.io/badge/hands-Cua%20Driver-blue.svg)](https://github.com/trycua/cua)

*A local computer-use harness that lets an AI agent click, type, and read native macOS apps — driven by a **free local model**, in the **background**, then **replayed with no model at all.***

</div>

---

## Why

Agents that build UIs need to **test** them. Today that means slow, paid, DOM-only browser automation. GhostHands drives the **real macOS Accessibility tree** instead — so a tiny **local** model (or Claude, or Codex — swappable) can operate any native app, in the background, for **$0**. Run a flow once, and GhostHands records it so every rerun afterward needs **no model**.

```
          ┌──── brain slot ────┐
  goal →  │ local · Claude · GPT│ →  Cua Driver (hands) →  macOS apps
          └─────────────────────┘
                  swap the brain · shared hands · no cursor stolen
```

## The three ideas

🧠 **Free local brain.** A small MLX text model (default **Qwen2.5-7B-Instruct-4bit**) reads the structured **Accessibility tree** and picks the element to click — *no vision, no API, no tokens*. It drove macOS Calculator to `7 × 6 = 42` and toggled a Home Assistant entity at **100% / $0**, faster than the paid ceiling.

👁️ **Vision only when there's no other way.** Canvas/WebGL/game surfaces with no AX tree fall back to a local vision model (MAI-UI-8B) + pixel clicks. It's the *last* resort — the benchmark shows exactly why (below).

🔁 **Record → replay with no model.** Run a flow once with a model in the loop; GhostHands captures **name-targeted** steps (AX id / title). Then `replay` re-runs it **deterministically, $0, no model** — self-healing a moved target with a single model call only when needed. Testing a UI = running the same flow many times. Now those reruns are free.

## 60-second quickstart

```sh
# Hands: install Cua Driver (MIT) and grant Accessibility once
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"
cua-driver permissions grant

# Brain: a local MLX model (Apple Silicon). Pull the default 7B:
python -m venv .venv && .venv/bin/pip install mlx-lm mlx-vlm
.venv/bin/hf download mlx-community/Qwen2.5-7B-Instruct-4bit

# Drive a native app with the free local brain, $0:
.venv/bin/python bin/ghosthands run "Compute 7 times 6" \
    --brain local --app com.apple.calculator

# Record a flow once, then replay forever with NO model:
.venv/bin/python bin/ghosthands record myflow "<goal>" --app <bundle> --url <page>
bin/ghosthands replay myflow            # deterministic · $0
```

Other commands: `ghosthands doctor` (verify the environment), `ghosthands smoke` (Calculator 7×6=42), `ghosthands brains` (detect Claude/Codex), `ghosthands run --brain claude|gpt` (subscription agents), `ghosthands bench`.

## The benchmark

Every brain, identical task, identical hands. Wall-clock starts at dispatch and stops the instant a **world** check passes — a calculator display value read over AX, or the destination page title in any browser — never the agent's own words. Full per-device results and how to add your own Mac: **[bench/RESULTS.md](./bench/RESULTS.md)**.

Measured on an **Apple M4 Mac mini · 24 GB · macOS 27** *(calc n=5, web n=3)*:

| contender | hands / path | task | success | median | steps | cost\* |
|-----------|--------------|------|---------|--------|-------|------|
| `scripted-ax` | no model (floor) | calc 7×6 | 100% | 7.2s | — | $0 |
| **`local` 7B** | free MLX · **AX** | calc 7×6 | **100%** | 20.4s | 5 | **$0** |
| `mai-ui-pixel` | free vision · **pixel** | calc 7×6 | **0%** | — | 10 | $0 |
| `claude` | Claude · cua **AX** | calc 7×6 | 100% | 37.8s | 9 | $0.24 |
| **`local` 7B** | free MLX · **AX** | web | **100%** | **16.3s** | 1 | **$0** |
| `claude-browser` | Claude · agent-browser **DOM** | web | 100% | 19.2s | 5 | $0.07 |
| `claude-chrome` | Claude · chrome-devtools-mcp **DOM** | web | 100% | 22.1s | 5 | $0.08 |
| `claude` | Claude · cua **AX** | web | 100% | 23.1s | 6 | $0.11 |
| `claude-pixel` | Claude · cua **pixel** | web | **0%** | — | 19 | — |

\* Local/scripted contenders burn zero tokens; the Claude `cost` is the metered `total_cost_usd` Claude Code reports (subscription, not out-of-pocket).

Two results jump out:

1. **The free local 7B on AX is the fastest *model-driven* path on both tasks, at $0** — beating every Claude path (one AX click vs 5–9 tool round-trips).
2. **Same brain, different hands:** hand *Claude* the pixel path and it still scores **0%** (3× timeout, ~19 flailing clicks). The brain was never the bottleneck — synthetic pixel clicks don't land on backgrounded windows. DOM tools (agent-browser, chrome-devtools-mcp) and AX all work; pixels don't.

### Why AX wins and pixels lose (the whole thesis)

- **AX path** → Cua reads the accessibility tree (`[5] AXButton "7"`), a local *text* model picks the element, Cua fires `AXPress`. This is an accessibility *action* aimed at the element itself — no cursor, no focus — so it lands on a **background** window. ✅
- **Pixel path** → screenshot → vision model *guesses* coordinates → synthetic mouse click. macOS routes synthetic clicks to the **frontmost** window, so they never reach a backgrounded target. → **0%**. The only fix would be to foreground the window and move your cursor — which violates the whole point.

That's why GhostHands is AX-first and pixels are fallback-only.

## How the hands work

The hands are [**Cua Driver**](https://github.com/trycua/cua) (MIT) exposed over MCP. GhostHands adds a hardened wrapper that absorbs the prerelease driver's sharp edges:

- **Re-snapshot before every action** — the `element_index` cache expires in seconds.
- **Match by name, not index** — targets resolve against a fresh snapshot; duplicate-window subtrees are all tried.
- **Settle until the tree stops changing** before re-reading (a digit press flips a button label a beat before the display updates).
- **Cursor-less, no focus steal** — apps launch in the background; actions are `AXPress`.

## Requirements

- **macOS 14+ on Apple Silicon.** This is not cross-platform: the local brain uses **MLX** (Metal), and the hands use macOS Accessibility + ScreenCaptureKit. The *architecture* (swappable brain over MCP hands) is portable, but this build is Mac-only.
- **Cua Driver** pinned to `0.5.1` (prerelease) at `~/.local/bin/cua-driver`; Accessibility granted to `com.trycua.driver`. Screen Recording is optional (only the pixel fallback needs it).
- **Python 3.10+.** The CLI and AX path are stdlib-only; the local brains add `mlx-lm` / `mlx-vlm` in a venv (lazy-imported).

## Layout

```
src/ghosthands/  driver · ax · actions (hardened wrapper) · ownloop (LocalBrain + loop)
                 visionloop (pixel fallback) · flows (record/replay) · tasks · cli
skills/          model-agnostic operating instructions for any agent
bench/           brain-vs-brain harness + results
flows/           recorded flows (replay with no model)
```

See **[DESIGN.md](./DESIGN.md)** for the full spec, **[ROADMAP.md](./ROADMAP.md)** for milestones + the benchmark, and **[AGENTS.md](./AGENTS.md)** if you're an agent picking this up.

## License & credits

MIT. The hands are [Cua Driver](https://github.com/trycua/cua) (MIT) — the operating-instruction skill is adapted from its `cua-driver` skill; see [ATTRIBUTION.md](./ATTRIBUTION.md).
