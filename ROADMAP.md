# ROADMAP.md — Build order

Work top to bottom. Each milestone is shippable and testable on its own. Full detail in `DESIGN.md`.

## M0 — Environment doctor (`cua-doctor` / `ghosthands doctor`)
- [x] Verify Cua Driver is installed and prints a version; record the pinned version in `README.md`.
- [x] Check + (re)grant macOS permissions: Accessibility (required) and Screen Recording (optional). Use `cua-driver permissions grant` so grants bind to the driver identity. Report status clearly.
- [x] Start/ensure a persistent driver daemon (consider `--autostart`).
- [x] Provide a reusable **action wrapper** that: re-snapshots before acting, retries once or twice on `EAGAIN` / `daemon closed connection`, and verifies via a follow-up snapshot.
- **Done when:** `ghosthands doctor` reports a green environment and the wrapper reliably drives macOS Calculator (`7 × 6 = 42`) cursor-less.

## M1 — Operating-instruction skill (`skills/`)
- [x] `skills/SKILL.md` drafted — adapted from the proven `trycua/cua` `cua-driver` skill (MIT). Encodes no-foreground contract, snapshot→act→verify, element-index `(pid, window_id)` cache rules, AXMenuBar, error→fix table, routing.
- [x] Add `WEB_APPS.md` (browser/Electron: launch_app-urls, windows-over-tabs, sparse-tree retry, the `page` JS escape hatch, minimized-commit no-op).
- [x] Add `TESTS.md` (verification scenarios + "should-NOT-succeed" trust probes; flag "reported success without an after-snapshot" as a hallucination failure).
- **Done when:** dropping the skill into an agent makes it follow snapshot→act→verify, no-foreground, retry, re-snapshot.

## M2 — Wire both brains to the shared hands
- [x] Claude Code: `claude mcp add --transport stdio cua-driver -- ~/.local/bin/cua-driver mcp`.
- [x] Codex CLI: `codex mcp add cua-driver -- ~/.local/bin/cua-driver mcp` (writes `~/.codex/config.toml [mcp_servers.*]`).
- **Done when:** both Claude Code and Codex CLI can drive Calculator through Cua using the M1 skill.

## M3 — Brain selector + launcher (`ghosthands run`)
- [x] `select-brain`: detect authorized brains (Claude Code logged in? Codex signed in? API keys present?), pick best by configurable priority (default: subscription brains before API).
- [x] `ghosthands run "<goal>" [--brain claude|gpt|auto]`: launch the chosen brain wired to Cua + the skill, pass the goal, stream results.
- **Done when:** the same goal runs on either brain by swapping only `--brain`; `auto` picks correctly.

## M4 — Browser task
- [x] Drive one web task via AX, falling back to the `page` DOM tool where AX is insufficient.
- **Done when:** a real browser task completes and is verified.

## M5 — Benchmark harness (`bench/`)
- [x] Task suite with machine-checkable **done-detectors** (file/state/API checks). Mix native + browser; ≥1 task all contenders can run.
- [x] Full-auto / no-prompt mode for each brain; pre-grant OS permissions.
- [x] External wall-clock (start on dispatch, stop on done-detector). `codex exec --json` has **no per-action timestamps** — external timing is required.
- [x] N≥5 runs per task; report median + spread. Metrics: wall-clock, success %, steps, cost.
- **Done when:** harness emits a `brain × task → median s, success %, steps, cost` table comparing Claude+Cua vs GPT/Codex+Cua (and a DOM-only baseline).

## M6 (optional) — Standalone own-loop (Track B)
- [x] `Brain` interface: `decide(goal, state, history) -> Decision` (`src/ghosthands/ownloop.py`).
- [x] Adapters: `AnthropicAPIBrain` (messages API), `OpenAIAPIBrain` (chat completions). Swap via `ghosthands ownloop --brain`.
- [x] Loop machinery validated with `MockBrain` (no key needed): `tests/test_ownloop_mock.py` drives Calculator 8 × 3 = 24, world-checked.
- **Done when:** a no-agent-UI loop drives an app with either brain via API token. *(Loop proven via mock; API adapters built but unexercised — no API keys present on this machine, and Track B is pay-per-call by design.)*

## M7 — Free local brain on the AX tree
- [x] `LocalBrain` (`ownloop.py`): goal + AX-tree digest → JSON `element_index` actions, via an MLX **text** model (default `mlx-community/Qwen2.5-7B-Instruct-4bit`). No vision, no API, **$0**. Lazy `mlx_lm` import keeps the rest stdlib-only.
- [x] **Plan-ahead** prompt (full ordered click sequence per turn, not one action) — the change that made small models reliable. AX-tree digest filters menus, shows value nodes first, dedupes duplicate subtrees.
- [x] Loop hardening for a local brain: settle-until-stable before re-snapshot, lenient JSON parse with regex fallback, bad-action tolerance, optional `done_check` world stop, browser launch (`urls`/`title_contains`).
- [x] Vision fallback `VisionBrain` (`visionloop.py`, MAI-UI-8B): screenshot → normalized coord → pixel click, for no-AX surfaces.
- [x] CLI: `ghosthands run "<goal>" --brain local --app <bundle>` and `ghosthands ownloop --brain local`.
- **Done when:** the local 7B drives Calculator to `7 × 6 = 42` via the AX tree, cursor-less, $0, verified by the display. ✅ (also toggles a Home Assistant entity, world-verified.)

## M8 — Record-assert-replay (free reruns)
- [x] `flows.py`: `record(...)` runs a flow once with the local brain and captures **name-targeted** steps (AX id / title / role+text), saved to `flows/<name>.json`.
- [x] `replay(flow)` re-runs with **no model**, re-resolving each target against a fresh snapshot through the hardened wrapper; `--heal` spends one model call when a target won't resolve and rewrites the flow.
- [x] CLI: `ghosthands record …` / `ghosthands replay <flow> [--heal]`.
- **Done when:** the HA-toggle flow records once, then replays with no model and the world done-detector passes — repeatably. ✅

---

### Definition of MVP done
M0–M5 complete and passing on this machine. See `DESIGN.md` §10 for the acceptance checklist.

### This session's goal (free local computer-use)
M7 + M8 complete: a free, local model drives macOS via the AX tree, and flows
replay with no model. Benchmark comparing `local` (7B/14B AX) vs `mai-ui-pixel`
(vision) vs `claude` (subscription ceiling) below.

| contender    | task      | n  | success | median s | steps | cost |
|--------------|-----------|----|---------|----------|-------|------|
| scripted-ax  | calc-7×6  | 5  | 100%    | 7.2      | —     | $0   |
| local 7B     | calc-7×6  | 5  | 100%    | 20.4     | 5     | $0   |
| mai-ui-pixel | calc-7×6  | 5  | 0%      | —        | 10    | $0   |
| claude       | calc-7×6  | 5  | 100%    | 37.8     | 9     | sub  |
| scripted-ax  | ha-toggle | 5  | 100%    | 4.8      | —     | $0   |
| local 7B     | ha-toggle | 5  | 100%    | 14.0     | 1     | $0   |
| mai-ui-pixel | ha-toggle | 5  | 0%      | —        | 10    | $0   |
| claude       | ha-toggle | 1\*| 0%\*    | —        | 52    | sub  |

Free local 7B on AX = 100% on both tasks at $0, faster than the subscription
ceiling. Vision+pixel = 0% (synthetic clicks don't land on background windows).

\* `claude × ha-toggle` n=1, timeout — harness fairness artifact, not a Claude
limitation: own-loop contenders are handed the app (Brave); Claude got only the
prose goal, chose Safari (not logged into HA, JS blocked), switched to Brave too
late, hit the 300 s cap mid-toggle. Claude's calc row (100%) is the clean ceiling.
`local-14b` (`mlx-community/Qwen2.5-14B-Instruct-4bit`) is available via
`--contenders`/`--model` but was dropped from the default set: with the verbose
calc goal it over-plans (50 steps), whereas 7B-4bit is 100% — a nice "smaller is
enough on the AX path" result.

## M9 — Multi-path comparison + contributable per-device results
- [x] Same brain (Claude), swapped **hands**: `claude` (cua AX) vs `claude-pixel`
  (cua pixel) vs `claude-chrome` (chrome-devtools-mcp DOM) vs `claude-browser`
  (agent-browser CLI DOM) — `brains.py`.
- [x] Browser-agnostic world done-detector (any browser's window title) so the
  Safari / Chrome / Chromium paths compare on one task (`tasks._any_browser_title`).
- [x] Per-device results: `bench/run_bench.py --device` auto-stamps hardware
  (`bench/devices.py`) and writes `bench/results/<slug>.json`; `bench/render.py`
  regenerates the leaderboard `bench/RESULTS.md`. Adding a machine = run → render → PR.
- **Done when:** the web cross-path table is in `bench/RESULTS.md` and a second
  machine can drop in its numbers without touching anyone else's data. ✅

Web cross-path (Apple M4 mini, n=3): **pixel = 0% even with Claude as the brain**
(the brain was never the bottleneck); the free local 7B on AX is the fastest
model-driven path at $0. DOM paths (agent-browser, chrome-devtools-mcp) ≈ cua AX,
all ~20s. Full per-device data: `bench/RESULTS.md`.

## M10 — Speed pass (profile → fix the real bottlenecks)
- [x] **Profiled the stack**: subprocess-per-call only ~23ms; AX snapshot ~75ms;
  but every *action* call blocked ~1.13s — the daemon executes in ≤250ms and
  pads the success response (errors return in <50ms). Model decide() was 13.4s
  (prefill 2.8s of a 657-token prompt + slow 7B decode).
- [x] **Fire-and-go actions** (`driver.fire`/`PendingCall`, `App._fire_and_judge`,
  batched dispatch in `run_loop`): dispatch, wait 0.35s, exited→classify error,
  running→landed; truth read from the next snapshot. Scripted calc 7.2s → 4.5s.
- [x] **Model swap + compact protocol**: Qwen2.5-7B → **Qwen3-4B-Instruct-2507**
  (researched + head-to-head benched; Qwen3.5-4B rejected — non-deterministic
  plans on identical state). Brain now emits `{"plan","done","clicks":[N…]}` —
  ~30 generated tokens instead of ~120.
- [x] **KV prompt cache across turns** (static-first prompt ordering, trim on
  divergence) + **JSON early-stop** (stop decoding at the closing brace):
  warm decide 13.4s → **1.8s**.
- [x] **Canvas contract** (`skills/CANVAS.md`): never mouse-draw geometry —
  inject Excalidraw scene JSON via `localStorage` (verified live: 3-box diagram
  in seconds vs 6 min/rectangle), clipboard `excalidraw/clipboard` payload, or
  the Mermaid dialog as AX-only fallback. Shipped to subscription brains via
  `SKILL_FILES`.
- **Result (n=5, world-checked):** local calc **20.4s → 11.3s**, local web
  **16.3s → 14.5s**, floor **7.2s → 4.5s**, all 100% success, $0.
- Backlog from research (not yet adopted): persistent Unix-socket client
  (~20ms/call), AXObserver-gated settle, snapshot diffing, speculative actions
  from `flows/`, tiered local→Claude escalation, Electron `AXManualAccessibility`
  auto-enable. The stale `local × ha-toggle` rows moved to `local-7b` (HA test
  entity currently 404 — task needs re-setup before it can run again).

## M11 — Reliability pass (discriminative benchmark → protocol + routing)
- [x] **Model gate v2** (`bench/model_gate.py`): seeded parametric benchmark on
  the REAL decide() pipeline — long ordered plans, disambiguation among
  identical labels, teacher-forced wizard steps, honesty traps stamped from
  the same templates (clicking = fail, false done = critical), determinism
  (N×M identical-and-correct), one-shot scene-JSON (value-checked). Same
  `--seed` ⇒ byte-identical suite. The old calc-7×6 saturated at 100% for
  every model and hid all of this.
- [x] **Six-model bake-off** (both research fleets' picks, measured): every
  model failed long plans on the index protocol; only Qwen3-8B declined
  impossible goals and produced a valid labeled scene. Gemma-3-4B fastest but
  guesses on impossible goals; Granite-4.0-Micro (the fleet's "honesty pick")
  hallucinated worst. Verdict: keep Qwen3-4B-2507 as planner, route canvas/
  honesty-critical work to Qwen3-8B.
- [x] **Clicks-by-NAME protocol**: the brain answers with button names, the
  harness resolves them against the live digest (`resolve_clicks_guarded`).
  Gate, same seed, same model: long plans 0%→83%, disambig 67%→100%,
  determinism 0%→100%, p50 1.55→1.35s. Symbol aliases ("×"→"Multiply") bridge
  fixture symbols vs real AX names.
- [x] **Honesty guard**: a plan naming a button that isn't on screen is
  refused ("cannot: X is not on screen") instead of acted on — trap pass rate
  0%→33% at system level; semantic traps (existing-but-wrong element) remain
  a 4B model ceiling.
- [x] **Navigation cut + load-wait**: a link/Continue click drops the rest of
  the batch (those indices belong to a dead page) and the loop waits for the
  tree to change before settling. Window-twin subtrees trimmed at the second
  AXWindow root (the old text-dedup hid 4 of 5 'Learn more' links — found
  only because the fixture bench failed while the paper gate passed).
- [x] **Two-model routing**: `ghosthands scene "<description>"`
  (`src/ghosthands/scene.py`, Qwen3-8B) emits a validated Excalidraw scene /
  clipboard payload for the CANVAS.md recipes. Verified live: 5 boxes, 4
  arrows, labels exact.
- [x] **Fixture benchmark (tier 1)**: `bench/fixture/` local site + event-log
  done-detectors; tasks `v2-wizard` / `v2-disambig` / `v2-deepnav` wired into
  `run_bench.py`. Local fixtures = reproducible scores (real sites redesign,
  A/B-test, geo-vary).
- **E2E results (n=3, world-checked, $0):** calc **11.3→8.1s**, web 12.1s,
  v2-disambig **0%→100% @ 8.2s** — all 100%. Still open (deliberately
  unsaturated): `v2-wizard` 0% and `v2-deepnav` 33% — multi-page state is the
  4B's real frontier; the failure modes (done claimed after first click,
  page-load races) are logged for the next pass. Honesty beyond the guard and
  scene-JSON for the 4B remain model ceilings — candidates for the tiered
  local→8B escalation backlog item.
