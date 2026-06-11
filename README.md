# GhostHands

**Invisible hands any AI model can borrow to operate your real Mac — locally, in the background, without stealing your cursor.**

GhostHands is a model-agnostic local computer-use harness. It gives an AI agent the ability to click, type, and read native macOS app UIs, where the **model brain is swappable**: the same "hands" work whether the brain is Anthropic's Claude or OpenAI's GPT/Codex. It detects which brains you've authorized on the machine and routes to the best available one.

- **Local & private.** Everything runs on your Mac. No cloud backend, no key-vending proxy.
- **Model-agnostic.** One set of hands; swap the brain (Claude ⇄ GPT/Codex).
- **Cheap.** When the brain is a vendor agent app (Claude Code / Codex CLI), your existing **subscription** covers it. A standalone loop uses pay-per-call API tokens.
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

🚧 Early implementation. See **[DESIGN.md](./DESIGN.md)** for the full spec and **[ROADMAP.md](./ROADMAP.md)** for the build plan.

## Start here (for contributors / agents)

Read **[AGENTS.md](./AGENTS.md)** — it tells an implementing agent exactly what to do and in what order.

## License

MIT (intended). The hands (Cua Driver) are MIT.
