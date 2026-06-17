# Decisions Log

Running log of architectural, scope, and process decisions for this project. Newest entries at the top. Each entry is short — for deep rationale on a single locked decision, write an ADR alongside in `docs/decisions/YYYY-MM-DD-*.md` and reference it here.

Maintained via the `/decisions` skill. See `~/.claude/skills/decisions/SKILL.md` for the entry format and invocation rules.

---

## 2026-06-17: usher gets per-backend pooling modes + a generic declared-lease arbiter

**Decision:** Each usher backend now declares a **pool mode** — `shared` (default, one child multiplexed across all connections), `per-agent` (one child per client connection, reaped on disconnect), or `pool:N` (up to N children, each connection routed to the least-loaded live instance). The supervisor's old "backend name → 1 child" assumption is replaced by a coordinator owning a set of `backendInstance`s (new `internal/broker/instance.go`); `EnsureLive` is connection-aware. Separately, arbitration is **decoupled from cua**: the hardcoded screen write-lock is replaced by a generic declared-lease model — a backend declares `Leases [{Resource, Scope: global|window, ToolClass: write | Tools: [...]}]` and `ArbitrateStage` acquires/releases leases purely from declarations with zero cua knowledge. Backends that declare nothing are never gated. Built via background Workflow (architect → 4 serial implements → race + live verify → repair), opus implement / sonnet verify.

**Rationale:** Sharing one child is only safe for *stateless* backends — stateful ones (cua sessions, filesystem cwd) bleed state between agents, and a single shared child is a single point of failure for all of them. Pool mode lets the backend's nature pick isolation vs. memory (the shared-pool memory thesis still holds for `shared`); `pool:N` doubles as the load-balancer that removes head-of-line blocking on a busy backend. The screen-lock was cua-shaped in a broker that must take *any* MCP; declared leases generalize "take turns on a scarce resource" without the desk knowing what a screen is, and cua reproduces its exact prior per-window write-lock by declaring `{screen, window, write}` — so behavior is unchanged while the coupling is gone. This effectively closes de-cua issue #2 (arbitration tool-classification) and softens #3 (generic gate).

**Status:** active

**References:** usher `feat/pooling-leases` @ `a5d995c` (not pushed); georgenijo/usher issues #1 (hardcoded cua default — still open), #2 (arbitration decouple — addressed), #3 (generic gate — partial); supersedes the cua-specific arbitration in #16

---

## 2026-06-16: Name the MCP broker "usher" and build it in Go

**Decision:** The planned MCP broker ("front desk", EPIC #13) becomes a standalone repo named **`usher`**, written in **Go**, sibling to ghosthands and agent-mesh. A single `usher` binary is both the daemon (`usher serve`) and the control CLI (`usher backend add`, …), talking over a Unix socket. GhostHands stays Python and demotes to one *backend* behind the broker.

**Rationale:** The broker is an always-on daemon — many concurrent agent connections, per-window write-locks, TTL leases, reclaim-on-death, and a stdio/socket proxy on the hot path. Go's goroutines / channels / `context` cancellation fit that shape better than a Python async daemon (GIL, process supervision). A single static binary delivers the locked distribution story (launchd + `brew services` + curl installer, Developer ID sign/notarize) with no interpreter or venv on the user's machine. The only place we pay twice is porting the existing Python `compaction` trimmer (#15) to Go — but it's a stateless pure transform on MCP JSON, mechanical to port and faster in-process afterward. Polyglot tax is bounded: `usher` is the only new Go surface, and the GhostHands⇄usher boundary is MCP-over-stdio, language-blind. Name: an usher seats each arrival → the broker routes each call to the right backend; "House of Usher" themes loosely with ghosthands without being twee; no dominant dev binary collides.

**Status:** active

**References:** EPIC #13, #14 (skeleton), #15 (trim ★), #16 (arbitration), #32 (backend registration), `TOMORROW.md`

---
