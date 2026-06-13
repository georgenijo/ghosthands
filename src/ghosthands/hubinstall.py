"""Put the hub *in the path*: re-register an agent's ``cua-driver`` MCP server
so it runs through :mod:`ghosthands.hub` instead of talking to the hands raw.

The hub (``ghosthands hub``) is a transparent stdio proxy — to the agent it is
indistinguishable from ``cua-driver mcp`` — but it tees every JSON-RPC frame to
``~/.ghosthands/hub/<agent>.jsonl`` tagged by ``GHOSTHANDS_AGENT``. Until an
agent is registered through the hub it talks to the hands directly and the
monitor's call feed stays empty (it can see *that* the agent is connected via
the process table, but not *what* it is calling). This module flips that:

    ghosthands hub install              # claude (this project) -> hub, + ghclaude
    ghclaude my-session claude ...       # launch tagged; calls stream to monitor
    ghosthands hub status                # show routing + recent tagged agents
    ghosthands hub uninstall             # restore raw cua-driver mcp

Safety contract:
- **New sessions only.** Re-registering an MCP server changes what the *next*
  agent session spawns; a live session already holds its stdio pipe and is
  untouched. So installing never clobbers an agent mid-flight.
- **Reversible.** The prior registration is captured to an install-state file
  and ``uninstall`` restores it (falling back to the canonical
  ``<DRIVER_BIN> mcp`` if no backup exists).
- **Idempotent.** Installing when already hub-routed is a no-op.

Why a separate module from :mod:`ghosthands.hub`: ``hub.serve`` is the hot path
spawned on *every* MCP session — it stays lean (no install machinery imported).
The install/uninstall/status verbs live here and are lazy-imported by the CLI
only when invoked.

Testability: the command-*building* and registration-*parsing* are pure
functions (``claude_add_cmd`` / ``_parse_claude_get`` / ``is_hub_routed`` …) and
the :class:`Installer` takes an injected command runner + getter, so the whole
sequencing is provable hermetically with no client CLI present. The live proof
exercises the real ``claude``/``codex`` binaries.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .driver import DRIVER_BIN

HUB_DIR = Path.home() / ".ghosthands" / "hub"
INSTALL_STATE = HUB_DIR / "install-state.json"
GHCLAUDE_PATH = Path.home() / ".local" / "bin" / "ghclaude"
DEFAULT_NAME = "cua-driver"

# `claude mcp get <name>` renders one field per line; we pull command/args/scope.
_CMD_RE = re.compile(r"^\s*Command:\s*(.+)$", re.M)
_ARGS_RE = re.compile(r"^\s*Args:\s*(.*)$", re.M)
_SCOPE_RE = re.compile(r"^\s*Scope:\s*(.+)$", re.M)
_KNOWN_SCOPES = ("local", "project", "user")

GHCLAUDE_SCRIPT = """\
#!/bin/sh
# ghclaude <tag> [claude args...]
# Launch Claude Code tagged for the GhostHands hub + monitor. The tag flows as
# GHOSTHANDS_AGENT through Claude Code into the hub MCP subprocess, so this
# session's calls show up named (not "unknown") in `ghosthands monitor`.
# Proven: Claude Code passes the launching shell's env to its stdio MCP servers.
if [ "$#" -lt 1 ]; then
  echo "usage: ghclaude <tag> [claude args...]" >&2
  exit 2
fi
tag="$1"
shift
exec env GHOSTHANDS_AGENT="$tag" claude "$@"
"""


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O) — hermetically testable                               #
# --------------------------------------------------------------------------- #

def hub_invocation() -> list[str]:
    """The command a client should run to reach the hands *through* the hub.

    Prefer the absolute path to the installed ``ghosthands`` console script
    (MCP servers spawn with a login-ish env whose PATH may not include
    ``~/.local/bin``); fall back to ``<python> -m ghosthands`` when the script
    isn't found. The trailing ``hub`` selects the proxy verb."""
    exe = shutil.which("ghosthands")
    if exe:
        return [exe, "hub"]
    return [sys.executable, "-m", "ghosthands", "hub"]


def raw_target() -> list[str]:
    """The canonical *un-proxied* registration: the hands' own MCP command.
    Used to restore on uninstall when no backup of the prior command exists."""
    return [DRIVER_BIN, "mcp"]


def _parse_claude_get(text: str) -> dict | None:
    """Parse ``claude mcp get <name>`` human output into
    ``{command, args, scope}``, or ``None`` if it describes no server (the
    ``Command:`` line is absent — e.g. "No MCP server found")."""
    m = _CMD_RE.search(text)
    if not m:
        return None
    am = _ARGS_RE.search(text)
    sm = _SCOPE_RE.search(text)
    scope = "local"
    if sm:
        # Match the leading word ("User config…" -> user), not a substring:
        # "User config (available in all your projects)" contains "project".
        head = sm.group(1).strip().split()[0].lower() if sm.group(1).strip() else ""
        scope = head if head in _KNOWN_SCOPES else "local"
    return {
        "command": m.group(1).strip(),
        "args": (am.group(1).strip() if am else ""),
        "scope": scope,
    }


def is_hub_routed(server: dict | None) -> bool:
    """True when ``server`` already runs through the hub — i.e. ``hub`` is the
    verb in its argv. Both ``<ghosthands> hub`` and ``<python> -m ghosthands
    hub`` shapes match. Used to make install idempotent."""
    if not server:
        return False
    return "hub" in (server.get("args", "") or "").split()


def claude_add_cmd(name: str, scope: str, target: list[str]) -> list[str]:
    return ["claude", "mcp", "add", name, "-s", scope, "--", *target]


def claude_remove_cmd(name: str, scope: str) -> list[str]:
    return ["claude", "mcp", "remove", name, "-s", scope]


def codex_add_cmd(name: str, target: list[str]) -> list[str]:
    return ["codex", "mcp", "add", name, "--", *target]


def codex_remove_cmd(name: str) -> list[str]:
    return ["codex", "mcp", "remove", name]


# --------------------------------------------------------------------------- #
# Live command runner / registration reader                                   #
# --------------------------------------------------------------------------- #

def _run(argv: list[str]) -> tuple[int, str]:
    """Run a client CLI command; return ``(exit_code, combined_output)``.
    Never raises: a missing binary or a timeout degrades to a non-zero code
    with the error text, so the caller can report it instead of crashing."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found on PATH"
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def _claude_get(name: str) -> dict | None:
    code, out = _run(["claude", "mcp", "get", name])
    if code != 0:
        return None
    return _parse_claude_get(out)


def _codex_get(name: str) -> dict | None:
    """Best-effort: codex has no per-server ``get``; scan ``codex mcp list``
    for a row starting with ``name``. Returns ``{command, args}`` or None."""
    code, out = _run(["codex", "mcp", "list"])
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == name:
            return {"command": parts[1] if len(parts) > 1 else "",
                    "args": parts[2] if len(parts) > 2 else ""}
    return None


# --------------------------------------------------------------------------- #
# Install state (backup for reversible uninstall)                             #
# --------------------------------------------------------------------------- #

def _load_state() -> dict:
    try:
        return json.loads(INSTALL_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    INSTALL_STATE.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_STATE.write_text(json.dumps(state, indent=2))


def _backup(client: str, name: str, server: dict | None) -> None:
    state = _load_state()
    state.setdefault(client, {})[name] = server
    _save_state(state)


def _pop_backup(client: str, name: str) -> dict | None:
    state = _load_state()
    server = state.get(client, {}).pop(name, None)
    _save_state(state)
    return server


# --------------------------------------------------------------------------- #
# Installer                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class StepResult:
    cmd: list[str]
    code: int
    out: str


@dataclass
class InstallResult:
    client: str
    changed: bool
    ok: bool
    message: str
    steps: list[StepResult] = field(default_factory=list)


class Installer:
    """Drives the client CLIs to (re-)register ``cua-driver`` through the hub.

    The runner and the registration readers are injectable so the sequencing is
    provable without a real ``claude``/``codex`` present."""

    def __init__(self, run=_run, claude_get=_claude_get, codex_get=_codex_get,
                 backup=_backup, pop_backup=_pop_backup):
        self._run = run
        self._claude_get = claude_get
        self._codex_get = codex_get
        self._backup = backup
        self._pop_backup = pop_backup

    # -- claude --------------------------------------------------------------

    def install_claude(self, name=DEFAULT_NAME, scope=None,
                       dry_run=False) -> InstallResult:
        cur = self._claude_get(name)
        if is_hub_routed(cur):
            return InstallResult("claude", changed=False, ok=True,
                                 message="already routed through the hub")
        scope = scope or (cur["scope"] if cur else "local")
        target = hub_invocation()
        plan: list[list[str]] = []
        if cur:
            plan.append(claude_remove_cmd(name, cur["scope"]))
        plan.append(claude_add_cmd(name, scope, target))
        if dry_run:
            return InstallResult("claude", changed=False, ok=True,
                                 message="dry-run",
                                 steps=[StepResult(c, 0, "(not run)") for c in plan])
        self._backup("claude", name, cur)
        steps = self._exec(plan)
        after = self._claude_get(name)
        ok = is_hub_routed(after)
        msg = ("routed cua-driver through the hub"
               if ok else "registration did not take — see step output")
        return InstallResult("claude", changed=True, ok=ok, message=msg, steps=steps)

    def uninstall_claude(self, name=DEFAULT_NAME, scope=None) -> InstallResult:
        cur = self._claude_get(name)
        backup = self._pop_backup("claude", name)
        scope = scope or (backup or {}).get("scope") or (cur["scope"] if cur else "local")
        if backup:
            target = [backup["command"], *backup["args"].split()]
        else:
            target = raw_target()
        plan: list[list[str]] = []
        if cur:
            plan.append(claude_remove_cmd(name, cur["scope"]))
        plan.append(claude_add_cmd(name, scope, target))
        steps = self._exec(plan)
        after = self._claude_get(name)
        ok = after is not None and not is_hub_routed(after)
        return InstallResult("claude", changed=True, ok=ok,
                             message=("restored raw cua-driver mcp" if ok
                                      else "restore did not take — see step output"),
                             steps=steps)

    # -- codex ---------------------------------------------------------------

    def install_codex(self, name=DEFAULT_NAME, dry_run=False) -> InstallResult:
        cur = self._codex_get(name)
        if cur and "hub" in (cur.get("args", "") or "").split():
            return InstallResult("codex", changed=False, ok=True,
                                 message="already routed through the hub")
        target = hub_invocation()
        plan = [codex_remove_cmd(name), codex_add_cmd(name, target)]
        if dry_run:
            return InstallResult("codex", changed=False, ok=True, message="dry-run",
                                 steps=[StepResult(c, 0, "(not run)") for c in plan])
        self._backup("codex", name, cur)
        steps = self._exec(plan, ignore_first_failure=True)
        after = self._codex_get(name)
        ok = bool(after) and "hub" in (after.get("args", "") or "").split()
        return InstallResult("codex", changed=True, ok=ok,
                             message=("routed cua-driver through the hub" if ok
                                      else "registration did not take — see step output"),
                             steps=steps)

    def uninstall_codex(self, name=DEFAULT_NAME) -> InstallResult:
        backup = self._pop_backup("codex", name)
        target = ([backup["command"], *backup["args"].split()]
                  if backup and backup.get("command") else raw_target())
        plan = [codex_remove_cmd(name), codex_add_cmd(name, target)]
        steps = self._exec(plan, ignore_first_failure=True)
        after = self._codex_get(name)
        ok = bool(after) and "hub" not in (after.get("args", "") or "").split()
        return InstallResult("codex", changed=True, ok=ok,
                             message=("restored raw cua-driver mcp" if ok
                                      else "restore did not take — see step output"),
                             steps=steps)

    def _exec(self, plan: list[list[str]],
              ignore_first_failure: bool = False) -> list[StepResult]:
        steps: list[StepResult] = []
        for i, cmd in enumerate(plan):
            code, out = self._run(cmd)
            steps.append(StepResult(cmd, code, out))
            # A `remove` of a not-yet-registered server is expected to fail;
            # don't abort the add that follows.
            if code != 0 and not (i == 0 and ignore_first_failure):
                if cmd[:3] != ["claude", "mcp", "remove"] and cmd[:3] != ["codex", "mcp", "remove"]:
                    break
        return steps


def write_ghclaude(path: Path | None = None) -> Path:
    """Write the ``ghclaude`` launcher (env-tag wrapper) and mark it
    executable. Returns the path written."""
    path = path or GHCLAUDE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GHCLAUDE_SCRIPT)
    path.chmod(0o755)
    return path


# --------------------------------------------------------------------------- #
# CLI entry points                                                            #
# --------------------------------------------------------------------------- #

def _print_result(r: InstallResult) -> None:
    mark = "✅" if r.ok else "❌"
    verb = "would run" if r.message == "dry-run" else ""
    print(f"  {mark} {r.client}: {r.message}")
    for s in r.steps:
        if verb:
            print(f"      {verb}: {' '.join(s.cmd)}")
        elif s.code != 0:
            print(f"      ! ({s.code}) {' '.join(s.cmd)}\n        {s.out.splitlines()[0] if s.out else ''}")


def cli_install(args) -> int:
    inst = Installer()
    results: list[InstallResult] = []
    if args.client in ("claude", "both"):
        results.append(inst.install_claude(args.name, scope=args.scope,
                                            dry_run=args.dry_run))
    if args.client in ("codex", "both"):
        results.append(inst.install_codex(args.name, dry_run=args.dry_run))
    print("hub install:")
    for r in results:
        _print_result(r)
    if not args.dry_run and not getattr(args, "no_ghclaude", False):
        p = write_ghclaude()
        print(f"  ✅ wrote launcher {p}")
        print(f"\nNext: start a tagged session and watch it in the monitor:")
        print(f"  ghosthands monitor &")
        print(f"  ghclaude my-session   # then drive any native app; calls stream in")
    return 0 if all(r.ok for r in results) else 1


def cli_uninstall(args) -> int:
    inst = Installer()
    results: list[InstallResult] = []
    if args.client in ("claude", "both"):
        results.append(inst.uninstall_claude(args.name, scope=args.scope))
    if args.client in ("codex", "both"):
        results.append(inst.uninstall_codex(args.name))
    print("hub uninstall:")
    for r in results:
        _print_result(r)
    return 0 if all(r.ok for r in results) else 1


def cli_status(args) -> int:
    name = getattr(args, "name", DEFAULT_NAME)
    claude = _claude_get(name)
    codex = _codex_get(name)
    print("hub status:")
    for client, server in (("claude", claude), ("codex", codex)):
        if server is None:
            print(f"  {client}: cua-driver not registered")
        elif is_hub_routed(server) or "hub" in (server.get("args", "") or "").split():
            print(f"  {client}: ✅ routed through the hub  "
                  f"({server.get('command')} {server.get('args')})")
        else:
            print(f"  {client}: ⛔ raw (not through hub)  "
                  f"({server.get('command')} {server.get('args')})")
    print(f"  ghclaude: {'present' if GHCLAUDE_PATH.exists() else 'absent'} ({GHCLAUDE_PATH})")
    # Recent tagged agents from the tee logs.
    logs = sorted(HUB_DIR.glob("*.jsonl")) if HUB_DIR.is_dir() else []
    if logs:
        print("  tagged logs:")
        for fp in logs:
            try:
                n = sum(1 for _ in fp.open())
            except OSError:
                n = 0
            print(f"    {fp.stem:<20} {n} frames")
    else:
        print("  tagged logs: none yet")
    return 0
