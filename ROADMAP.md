# ROADMAP.md — Build order

Work top to bottom. Each milestone is shippable and testable on its own. Full detail in `DESIGN.md`.

## M0 — Environment doctor (`cua-doctor` / `ghosthands doctor`)
- [ ] Verify Cua Driver is installed and prints a version; record the pinned version in `README.md`.
- [ ] Check + (re)grant macOS permissions: Accessibility (required) and Screen Recording (optional). Use `cua-driver permissions grant` so grants bind to the driver identity. Report status clearly.
- [ ] Start/ensure a persistent driver daemon (consider `--autostart`).
- [ ] Provide a reusable **action wrapper** that: re-snapshots before acting, retries once or twice on `EAGAIN` / `daemon closed connection`, and verifies via a follow-up snapshot.
- **Done when:** `ghosthands doctor` reports a green environment and the wrapper reliably drives macOS Calculator (`7 × 6 = 42`) cursor-less.

## M1 — Operating-instruction skill (`skills/`)
- [ ] Author `SKILL.md` + `MACOS.md` encoding the golden workflow + hard rules (AGENTS.md). Model-agnostic prose any MCP-capable agent can follow.
- **Done when:** dropping the skill into an agent makes it follow snapshot→act→verify, no-foreground, retry, re-snapshot.

## M2 — Wire both brains to the shared hands
- [ ] Claude Code: `claude mcp add --transport stdio cua-driver -- ~/.local/bin/cua-driver mcp`.
- [ ] Codex CLI: `codex mcp add cua-driver -- ~/.local/bin/cua-driver mcp` (writes `~/.codex/config.toml [mcp_servers.*]`).
- **Done when:** both Claude Code and Codex CLI can drive Calculator through Cua using the M1 skill.

## M3 — Brain selector + launcher (`ghosthands run`)
- [ ] `select-brain`: detect authorized brains (Claude Code logged in? Codex signed in? API keys present?), pick best by configurable priority (default: subscription brains before API).
- [ ] `ghosthands run "<goal>" [--brain claude|gpt|auto]`: launch the chosen brain wired to Cua + the skill, pass the goal, stream results.
- **Done when:** the same goal runs on either brain by swapping only `--brain`; `auto` picks correctly.

## M4 — Browser task
- [ ] Drive one web task via AX, falling back to the `page` DOM tool where AX is insufficient.
- **Done when:** a real browser task completes and is verified.

## M5 — Benchmark harness (`bench/`)
- [ ] Task suite with machine-checkable **done-detectors** (file/state/API checks). Mix native + browser; ≥1 task all contenders can run.
- [ ] Full-auto / no-prompt mode for each brain; pre-grant OS permissions.
- [ ] External wall-clock (start on dispatch, stop on done-detector). `codex exec --json` has **no per-action timestamps** — external timing is required.
- [ ] N≥5 runs per task; report median + spread. Metrics: wall-clock, success %, steps, cost.
- **Done when:** harness emits a `brain × task → median s, success %, steps, cost` table comparing Claude+Cua vs GPT/Codex+Cua (and a DOM-only baseline).

## M6 (optional) — Standalone own-loop (Track B)
- [ ] `Brain` interface: `decide(goal, state, history) -> [Action]`.
- [ ] Adapters: `AnthropicBrain`, `OpenAIBrain` (pay-per-call API). Swap via config.
- **Done when:** a no-agent-UI loop drives an app with either brain via API token.

---

### Definition of MVP done
M0–M5 complete and passing on this machine. See `DESIGN.md` §10 for the acceptance checklist.
