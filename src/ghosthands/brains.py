"""Brain detection, selection, and launching (Track A — agent-as-brain).

A "brain" is a vendor agent app that consumes the cua-driver MCP server:
- claude — Claude Code CLI (Anthropic subscription)
- gpt    — Codex CLI (ChatGPT subscription)

API keys (ANTHROPIC_API_KEY / OPENAI_API_KEY) are detected and reported as
Track B candidates but are not launchable here (own-loop, pay-per-call —
DESIGN.md §3/M6).

Launch contract (identical task framing for every brain — the hands and the
instructions are the constants; only the brain varies):
- The operating skill (skills/SKILL.md + WEB_APPS.md) rides along: Claude via
  --append-system-prompt, Codex prepended to the prompt (no system slot).
- cua-driver is wired ephemerally for Claude (--mcp-config --strict-mcp-config)
  and via the global `codex mcp add cua-driver` registration for Codex.
- Full-auto, no approval prompts; runs happen in a NEUTRAL cwd so neither
  agent ingests this repo's CLAUDE.md/AGENTS.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .driver import DRIVER_BIN

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_FILES = ("SKILL.md", "WEB_APPS.md")

TASK_PREAMBLE = (
    "You control macOS through the cua-driver MCP tools (computer use). "
    "Use ONLY the cua-driver MCP tools for desktop control — no other "
    "computer-use or browser tools. Work cursor-less: never pass a 'session' "
    "argument. Verify every state-changing action with an after-snapshot.\n"
    "TASK: "
)

DEFAULT_PRIORITY = ["claude", "gpt"]  # subscription brains; order configurable


@dataclass
class Brain:
    name: str          # selector key: claude | gpt
    label: str         # human name
    kind: str          # subscription | api
    available: bool
    detail: str
    launchable: bool = True

    def command(self, goal: str, skill: str) -> tuple[list[str], str | None]:
        """Return (argv, stdin) for a full-auto run of `goal`."""
        raise NotImplementedError


def skill_text() -> str:
    parts = []
    for name in SKILL_FILES:
        path = REPO_ROOT / "skills" / name
        if path.exists():
            parts.append(path.read_text())
    return "\n\n".join(parts)


@dataclass
class ClaudeBrain(Brain):
    def command(self, goal: str, skill: str) -> tuple[list[str], str | None]:
        mcp = json.dumps({
            "mcpServers": {
                "cua-driver": {"command": DRIVER_BIN, "args": ["mcp"]}
            }
        })
        return ([
            "claude", "-p", TASK_PREAMBLE + goal,
            "--append-system-prompt", skill,
            "--mcp-config", mcp,
            "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
        ], None)


@dataclass
class CodexBrain(Brain):
    def command(self, goal: str, skill: str) -> tuple[list[str], str | None]:
        # No system-prompt slot: skill rides at the top of the prompt.
        prompt = skill + "\n\n" + TASK_PREAMBLE + goal
        return ([
            "codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ], None)


def _detect_claude() -> Brain:
    path = shutil.which("claude")
    if not path:
        return ClaudeBrain("claude", "Claude Code", "subscription", False, "claude not on PATH")
    try:
        ver = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as e:  # noqa: BLE001 - report, don't crash detection
        return ClaudeBrain("claude", "Claude Code", "subscription", False, f"--version failed: {e}")
    session = Path.home() / ".claude.json"
    if not session.exists():
        return ClaudeBrain("claude", "Claude Code", "subscription", False, f"{ver}; no ~/.claude.json (never logged in?)")
    return ClaudeBrain("claude", "Claude Code", "subscription", True, ver)


def _detect_codex() -> Brain:
    path = shutil.which("codex")
    if not path:
        return CodexBrain("gpt", "Codex CLI", "subscription", False, "codex not on PATH")
    try:
        status = subprocess.run([path, "login", "status"], capture_output=True, text=True, timeout=15)
        out = (status.stdout + status.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return CodexBrain("gpt", "Codex CLI", "subscription", False, f"login status failed: {e}")
    if "logged in" not in out.lower():
        return CodexBrain("gpt", "Codex CLI", "subscription", False, f"not signed in ({out.splitlines()[0] if out else 'no output'})")
    return CodexBrain("gpt", "Codex CLI", "subscription", True, out.splitlines()[0])


def _detect_api_keys() -> list[Brain]:
    found = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        found.append(Brain("claude-api", "Anthropic API", "api", True,
                           "ANTHROPIC_API_KEY set (Track B own-loop — not implemented, M6)", launchable=False))
    if os.environ.get("OPENAI_API_KEY"):
        found.append(Brain("gpt-api", "OpenAI API", "api", True,
                           "OPENAI_API_KEY set (Track B own-loop — not implemented, M6)", launchable=False))
    return found


def detect_all() -> list[Brain]:
    return [_detect_claude(), _detect_codex(), *_detect_api_keys()]


def select(brain: str = "auto", priority: list[str] | None = None) -> Brain:
    brains = {b.name: b for b in detect_all()}
    if brain != "auto":
        chosen = brains.get(brain)
        if chosen is None:
            raise SystemExit(f"unknown brain {brain!r} (known: {', '.join(brains)})")
        if not chosen.available:
            raise SystemExit(f"brain {brain!r} not available: {chosen.detail}")
        if not chosen.launchable:
            raise SystemExit(f"brain {brain!r} detected but not launchable: {chosen.detail}")
        return chosen
    for name in (priority or DEFAULT_PRIORITY):
        b = brains.get(name)
        if b and b.available and b.launchable:
            return b
    raise SystemExit("no authorized brain found (need Claude Code logged in or Codex CLI signed in)")


@dataclass
class RunResult:
    brain: str
    returncode: int
    output: str
    seconds: float
    workdir: str = ""


def run_goal(brain: Brain, goal: str, *, timeout: float = 600.0,
             stream=None, workdir: str | None = None) -> RunResult:
    """Dispatch `goal` to `brain`, full-auto, from a neutral cwd. `stream`
    is an optional callable that receives output lines as they arrive."""
    import time as _time

    argv, stdin = brain.command(goal, skill_text())
    wd = workdir or tempfile.mkdtemp(prefix="ghosthands-run-")
    started = _time.monotonic()
    proc = subprocess.Popen(
        argv, cwd=wd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if stream:
                stream(line.rstrip("\n"))
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        lines.append(f"[ghosthands] killed after {timeout}s timeout\n")
    return RunResult(
        brain=brain.name,
        returncode=proc.returncode if proc.returncode is not None else -9,
        output="".join(lines),
        seconds=_time.monotonic() - started,
        workdir=wd,
    )
