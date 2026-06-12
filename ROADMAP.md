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
