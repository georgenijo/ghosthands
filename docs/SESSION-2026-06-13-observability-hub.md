# Session record — observability + hub + web-DOM tier (2026-06-13)

A single advisory session that designed, built, proved, and merged a new
observability/coordination layer for GhostHands, then split the remaining work
into a tracked backlog.

## What shipped (on `main` @ `59ed102`)

Six stdlib-only modules, each with a test, built from six GitHub issues
(#1–#6, all now closed):

| # | Module | What it does |
|---|--------|--------------|
| #1 | `webtier.py` | DOM tier over Brave's CDP debug port — per-tab addressing (no fronting), real DOM clicks that fire React, `<select>` enumeration, type-without-focus |
| #2 | `hub.py` | transparent stdio MCP proxy ("shim"); tees every call to `~/.ghosthands/hub/<agent>.jsonl` tagged by `GHOSTHANDS_AGENT` |
| #3 | `compaction.py` | compact fat AX snapshots at the funnel (~96% on a real 90 KB capture) |
| #4 | `isolation.py` | per-agent windowed / exclusive / stateless lanes keyed by the who-sent-it tag |
| #5 | `monitor.py` | read-only localhost dashboard: **who** (agents) + **what** (live MCP call feed) |
| #6 | `hygiene.py` | `strip_menubar` / `filter_web` (AXWebArea-safe) / `scale_point` |

Plus PR #7 (the hub hook-up layer): `ghosthands hub install/uninstall/status`,
`ghclaude` session tagging, and a `monitor.probe_agents` climb that attributes a
hub-routed leaf to the brain two hops up.

CLI added: `ghosthands monitor | compact | hub | web`.

## The core idea (from George's whiteboard)

Agents share one set of hands (`cua-driver`). Instead of each agent spawning its
own bridge, route them through **one shared tools hub** — a transparent proxy
that tags every call by who sent it and tees it to a log. Because everything
funnels through one place, you get four things for free, all visibility-only
(**no gate, no confirm, no kill switch** — decided explicitly):

- **WHO** — who is driving the hands (process-tree probe, works even off-hub)
- **WHAT** — which app / live MCP calls (the hub tee)
- **isolation** — agents don't collide on the same window
- **compaction / hygiene** — slim the fat tool output at the funnel

See `docs/architecture/shared-tools-hub.html` (the design, your version → improved)
and `docs/architecture/mcp-proxy-architecture.html` (how the stdio proxy works).

## How it was built

- **Two build workflows + one fix workflow** (multi-agent, ~22 agents total):
  fan out one agent per module → adversarial review → fix the blocking findings
  (the reviewers caught real bugs: a wrong Retina premise, an unenforced
  exclusive lock, an ownership leak, a CDP infinite-loop) → re-verify ship-clean.
- **Live proofs**, serially (shared daemon/desktop can't parallelize): monitor
  showed the real connected agents; compaction hit 96.2% on a real snapshot; the
  hub passed 35 real tools verbatim with a tagged log; isolation spawned two
  distinct TextEdit instances; webtier drove a backgrounded Brave tab over CDP,
  world-verified via the fixture event log; hygiene cut 57% off a fresh snapshot.

## Field validation (App Store Connect)

A real agent drove the ASC New-App form with the web tier — backgrounded, no
focus theft, **~6 CDP calls vs ~30 AX/pixel/screenshot round-trips**. It found 4
gaps (shadow-DOM piercing, find-by-accessible-name, post-action verify, closed
shadow/iframe limit) — now tracked as **#8 (webtier v2)**.

## Multi-agent coordination

This session ran alongside other agents in the same cmux window. Lessons applied:

- **Isolate concurrent writers with a git worktree**, not a shared checkout. This
  session committed its modules to `feat/observability-hub` in a separate worktree
  (`../ghosthands-hub`) so a sibling agent's `main` work was never disturbed.
- **Coordinate by messaging the sibling pane** via `cmux send --surface <ref>`
  + `cmux send-key <ref> Enter`. Used to warn the hub-hook-up agent not to
  double-commit the modules and to agree the merge sequence.
- Result: clean layered merge — this session's modules (`5ed6a6c`) → the hub
  hook-up (PR #7) → `main` `59ed102`.

## Tracked backlog (open issues)

- **#8** webtier v2: shadow-DOM piercing + find-by-accessible-name + post-action
  verify (active branch `webtier-v2`)
- **#9** surface routing: auto native→AX / web→DOM in the run loop (split from #1)
- **#10** wire hygiene + compaction into the local-brain loop

## Process going forward

issue → branch → PR (`Closes #N`) → merge → close. `webtier-v2` is wired to #8.
