# Decisions Log

Running log of architectural, scope, and process decisions for this project. Newest entries at the top. Each entry is short — for deep rationale on a single locked decision, write an ADR alongside in `docs/decisions/YYYY-MM-DD-*.md` and reference it here.

Maintained via the `/decisions` skill. See `~/.claude/skills/decisions/SKILL.md` for the entry format and invocation rules.

---

## 2026-06-16: Name the MCP broker "usher" and build it in Go

**Decision:** The planned MCP broker ("front desk", EPIC #13) becomes a standalone repo named **`usher`**, written in **Go**, sibling to ghosthands and agent-mesh. A single `usher` binary is both the daemon (`usher serve`) and the control CLI (`usher backend add`, …), talking over a Unix socket. GhostHands stays Python and demotes to one *backend* behind the broker.

**Rationale:** The broker is an always-on daemon — many concurrent agent connections, per-window write-locks, TTL leases, reclaim-on-death, and a stdio/socket proxy on the hot path. Go's goroutines / channels / `context` cancellation fit that shape better than a Python async daemon (GIL, process supervision). A single static binary delivers the locked distribution story (launchd + `brew services` + curl installer, Developer ID sign/notarize) with no interpreter or venv on the user's machine. The only place we pay twice is porting the existing Python `compaction` trimmer (#15) to Go — but it's a stateless pure transform on MCP JSON, mechanical to port and faster in-process afterward. Polyglot tax is bounded: `usher` is the only new Go surface, and the GhostHands⇄usher boundary is MCP-over-stdio, language-blind. Name: an usher seats each arrival → the broker routes each call to the right backend; "House of Usher" themes loosely with ghosthands without being twee; no dominant dev binary collides.

**Status:** active

**References:** EPIC #13, #14 (skeleton), #15 (trim ★), #16 (arbitration), #32 (backend registration), `TOMORROW.md`

---
