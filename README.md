# GhostHands

**Invisible hands any AI model can borrow to operate your real Mac — locally, in the background, without stealing your cursor.**

GhostHands is a model-agnostic local computer-use harness. It gives an AI agent the ability to click, type, and read native macOS app UIs, where the **model brain is swappable** — from a **free local model** running on your Mac, to a subscription agent (Claude Code / Codex CLI), to a metered API. The same "hands" work under any of them. It exists so agents can **test their UI work fast and for free** instead of slow, paid browser automation.

- **Free & local.** A small local MLX model (default Qwen2.5-7B-Instruct-4bit) reads the macOS **Accessibility tree** and drives the app — no API, no tokens, **$0**. Vision models are the fallback for canvas/no-AX surfaces.
- **Record → replay with no model.** Run a flow once with a model in the loop; it captures **name-targeted** steps; then **replay deterministically with no model at all**. Testing a UI means running the same flow many times — this makes that free.
- **Model-agnostic.** One set of hands; swap the brain: `local` (free), `claude`/`gpt` (subscription), or API.
- **Fast & capable.** Drives the **Accessibility tree** of native apps (not just browser DOM), in the background, without stealing focus.

## How it works

```
          ┌──── brain slot ────┐
  goal →  │  Claude  OR  GPT   │  →  Cua Driver (hands)  →  macOS apps
          └────────────────────┘
                shared hands, swappable brain
```

The hands are [Cua Driver](https://github.com/trycua/cua) (MIT) — a host-native macOS control surface exposed over MCP. GhostHands adds: a **brain selector**, **operating-instruction skills**, **hardening** for the prerelease driver, and a **benchmark harness** to compare brains.

## Status

- **M0 — environment doctor + reliable action wrapper: done.**
  `ghosthands smoke` drives macOS Calculator to `7 × 6 = 42` cursor-less,
  through AX element actions only (no screenshots, no Screen Recording).
- **M1 — operating-instruction skill: done.** `skills/SKILL.md` (core
  contract) + `WEB_APPS.md` (browser quirks) + `TESTS.md` (trust probes).
- **M2 — both brains wired: done.** Claude Code and Codex CLI each drove
  Calculator to 42 through the same cua-driver MCP + skill, world-verified.
- **M3 — selector + launcher: done.** `ghosthands brains` detects authorized
  brains; `ghosthands run "<goal>" [--brain claude|gpt|auto]` dispatches
  full-auto with the skill riding along.
- **M4 — browser task: done.** Safari example.com → IANA via AX, repeatable,
  with a machine done-detector.
- **M5 — benchmark harness: done.** `ghosthands bench` measures contender ×
  task with external wall-clock and a world done-detector. See
  `bench/results/`.
- **M6 (Track B own-loop): built.** `ghosthands ownloop "<goal>" --brain
  claude-api|gpt-api` — swappable `Brain` over the same hands. Loop machinery
  validated with a scripted `MockBrain` (Calculator 8 × 3 = 24,
  world-checked); the API adapters are unexercised because no API keys live
  on this machine (Track B is pay-per-call by design).
- **M7 — free local brain on the AX tree: done.** `LocalBrain` (an MLX text
  model, default **Qwen2.5-7B-Instruct-4bit**) reads the AX tree and returns
  the element to click — no vision, no API, **$0**. It drives Calculator to
  `7 × 6 = 42` and toggles a Home Assistant entity, both cursor-less and
  world-verified: `ghosthands run "<goal>" --brain local --app <bundle>`.
  A vision fallback (`VisionBrain`, MAI-UI-8B, screenshot + pixel click) ships
  for no-AX surfaces — measured but slower/less reliable (see the benchmark).
- **M8 — record-assert-replay: done.** `ghosthands record` runs a flow once
  with the local brain and saves **name-targeted** steps; `ghosthands replay
  <flow>` re-runs it with **no model**, self-healing a missing target with a
  single model call. Proven on the HA-toggle flow (replays $0, world-verified,
  repeatable).

## Requirements

- macOS 14+ (Apple Silicon), single-user local machine.
- **Cua Driver — pinned: `0.5.1`** (prerelease; tested against this version
  only). Binary expected at `~/.local/bin/cua-driver` (override with
  `GHOSTHANDS_DRIVER_BIN`).
- macOS **Accessibility** permission granted to the driver daemon identity
  (`com.trycua.driver`) — grant with `cua-driver permissions grant`, never
  from a terminal (TCC attributes the grant to whoever raises the prompt).
- macOS **Screen Recording**: optional for the AX path (the default), required
  only for the vision/pixel fallback (`mai-ui-pixel`), which screenshots.
- Python 3.10+. The CLI and AX path are **standard-library only**. The free
  local brains additionally need a venv with `mlx-lm` (text) / `mlx-vlm`
  (vision) and a cached MLX model — the `mlx_*` imports are lazy, so everything
  except `--brain local` / `ownloop` / `record` / `replay --heal` runs without
  them. On this machine that venv is `./.venv` (`mlx-lm 0.31.3`,
  `mlx-vlm 0.6.3`); models are pulled with `.venv/bin/hf download`.

## Usage

```sh
bin/ghosthands doctor                  # verify binary, daemon, permissions, round-trip
bin/ghosthands smoke                   # prove the wrapper: Calculator 7 × 6 = 42

# Free local brain (MLX, $0) — run a goal on the AX tree:
.venv/bin/python bin/ghosthands run "Compute 7 times 6" --brain local --app com.apple.calculator
# (--url / --title for browser apps; --model to pick a different MLX model)

# Record once, then replay forever with NO model:
.venv/bin/python bin/ghosthands record ha-toggle "<goal>" --app com.brave.Browser --url <page> --title <win>
bin/ghosthands replay ha-toggle        # deterministic, $0 (add --heal to self-heal a moved target)

# Subscription / API brains:
bin/ghosthands brains                  # detect authorized brains + auto pick
bin/ghosthands run "<goal>" [--brain claude|gpt|auto]   # subscription agent
bin/ghosthands ownloop "<goal>" --brain local|claude-api|gpt-api

bin/ghosthands bench [--runs 5] [--tasks ...] [--contenders ...]
```

Run any command that loads a local model with the venv interpreter
(`.venv/bin/python bin/ghosthands …`) so `mlx` is importable; the pure-AX
commands (`doctor`, `smoke`, `replay` without `--heal`, subscription `run`)
work with system Python. `doctor` exits 0 on a green environment (add `--json`)
and starts the CuaDriver daemon if needed. `bench` writes raw results to
`bench/results/latest.json` and prints the contender × task table.

## Layout

```
src/ghosthands/
  driver.py     # raw `cua-driver call` layer + error classification
  ax.py         # parser for the get_window_state Markdown AX tree
  actions.py    # the hardened action wrapper (snapshot → act → verify)
  ownloop.py    # the own-loop + brains: LocalBrain (free MLX text, AX path),
                #   API adapters, MockBrain; the plan-ahead loop
  visionloop.py # VisionBrain (MLX MAI-UI) screenshot + pixel-click fallback
  flows.py      # record-assert-replay: capture name-targeted steps, replay $0
  tasks.py      # benchmark/acceptance tasks + world done-detectors
  doctor.py     # environment checks
  smoke.py      # Calculator 7 × 6 = 42 acceptance test
  cli.py        # `ghosthands` CLI
skills/         # model-agnostic operating instructions
bench/          # brain-vs-brain benchmark harness + results
flows/          # recorded flows (replay with no model)
```

## The hardening contract (why the wrapper exists)

Cua Driver 0.5.1 is a prerelease; the wrapper absorbs its sharp edges so
callers (and brains) never see them:

- **Re-snapshot before every action** — the `element_index` cache expires in
  seconds; acting on a stale index errors.
- **Match by name, not index** — targets are matchers (AX id / title / label)
  resolved against a fresh snapshot; when a snapshot renders the same window
  as two AXWindow subtrees with different index ranges, every candidate is
  tried.
- **Transient-error recovery** — on `EAGAIN` / `daemon closed connection` the
  action often landed anyway; with a `verify` predicate the wrapper checks
  state before re-issuing.
- **Cursor-less always** — no `session` is ever sent, so nothing needs Screen
  Recording.
- **No focus steal** — apps launch in the background; actions are AX presses.

## The free local brain (how it stays $0)

`LocalBrain` (`ownloop.py`) is a small MLX **text** model on the **AX path** —
the reliable one. Each turn it gets the goal plus a compact digest of the
current window: the actionable elements (`[N] role "name"`, menus filtered out)
and the on-screen value nodes (a calculator display, a field). It returns JSON
actions naming `element_index` values; the loop executes them through the
hardened wrapper, settles until the AX tree stops changing, and re-asks.

Two design choices made a 7B model reliable here:

- **AX, not pixels.** The model picks a structured `element_index`, so it needs
  no vision and the click lands on a background window. (Pixel/CGEvent clicks do
  *not* reliably register on background windows — that's what the
  `mai-ui-pixel` vision contender measures.)
- **Plan-ahead, not step-by-step.** Asked for one action per turn, small models
  loop ("press 6 again to complete 7 × 6"). Asked for the **full ordered click
  sequence** from the current screen, Qwen2.5-7B-4bit / 7B-8bit / 14B-4bit all
  drive Calculator to 42. 7B-4bit is the default; `--model` selects another.

Vision (`VisionBrain`, MAI-UI-8B) is the **fallback** for canvas/no-AX surfaces:
screenshot → normalized 0–1000 coordinate → pixel click. It needs Screen
Recording and is slower and less reliable on background windows by design.

## Record → replay (free reruns)

```sh
# 1. record once, model in the loop, capturing name-targeted steps
.venv/bin/python bin/ghosthands record ha-toggle "<goal>" \
    --app com.brave.Browser --url http://localhost:8123/ghosthands-test --title GhostHands
# 2. replay any number of times with NO model — deterministic, $0
bin/ghosthands replay ha-toggle
```

A flow (`flows/<name>.json`) stores each step by **name** (AX id / title /
role+text), never a volatile index. Replay re-resolves each target against a
fresh snapshot through the hardened wrapper, so it survives index churn and
duplicate-window subtrees. If a target no longer resolves, `--heal` spends a
**single** model call to re-pick it and rewrites the flow — the happy path never
touches a model. This is the point: testing a UI runs the same flow many times,
and here those reruns are free.

## Benchmark

`bench/run_bench.py` compares brains on identical tasks with identical hands.
External wall-clock starts at dispatch and stops the first time a **world**
done-detector reads true (a calculator display value, a Home Assistant entity
state via REST — never the agent's own words). N≥5 runs per cell; local models
load once per contender and are evicted before the next.

Latest run (N=5, this machine, `bench/results/latest.json`):

| contender    | task      | n  | success | median s | steps | cost |
|--------------|-----------|----|---------|----------|-------|------|
| scripted-ax  | calc-7×6  | 5  | 100%    | 7.2      | —     | $0   |
| **local 7B** | calc-7×6  | 5  | **100%**| 20.4     | 5     | **$0** |
| mai-ui-pixel | calc-7×6  | 5  | 0%      | —        | 10    | $0   |
| claude       | calc-7×6  | 5  | 100%    | 37.8     | 9     | sub  |
| scripted-ax  | ha-toggle | 5  | 100%    | 4.8      | —     | $0   |
| **local 7B** | ha-toggle | 5  | **100%**| 14.0     | 1     | **$0** |
| mai-ui-pixel | ha-toggle | 5  | 0%      | —        | 10    | $0   |
| claude       | ha-toggle | 1\*| 0%\*    | —        | 52    | sub  |

Reading it: the **free local 7B on the AX path is 100% on both tasks at $0**, and
faster than the subscription ceiling (20s vs 38s on calc). The **vision+pixel**
path is **0%** — its synthetic clicks don't register on a background window, which
is exactly why the AX path is the default and pixels are fallback-only.

\* `claude × ha-toggle` is **not a clean Claude score** — a harness fairness gap,
not a Claude limitation. The own-loop contenders are handed the app (Brave); the
subscription brain gets only the prose goal ("the web browser"), chose Safari —
which isn't logged into HA and blocks JS — flailed, switched to Brave late, and
hit the 300 s cap mid-toggle (it *had* found the right card). Labeled honestly
rather than rerun; Claude's calc row (100%) is the clean ceiling.

The Home-Assistant task toggles a dedicated, side-effect-free helper
(`input_boolean.ghosthands_test`) on a hidden dashboard (`/ghosthands-test`),
never a real device; its done-detector reads the entity state over HA's REST API
(token from `GHOSTHANDS_HA_TOKEN` or `~/homelab/.env`).

## Start here (for contributors / agents)

Read **[AGENTS.md](./AGENTS.md)** — it tells an implementing agent exactly what to do and in what order.

## License

MIT (intended). The hands (Cua Driver) are MIT.
