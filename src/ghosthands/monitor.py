"""Read-only observability of who is currently driving the hands.

GhostHands is a single shared resource: one `cua-driver` daemon, one screen,
many possible agents (Claude, Codex, raw CLI). When several runs touch the
same machine it gets hard to tell *who* has their hands on the keyboard. This
module answers that — and only that. It is a WATCHER, not a warden: no gate,
no kill switch, no mutation of any process or window. It reads the process
table and asks the driver which windows exist, and serves both as JSON plus a
tiny self-contained HTML dashboard.

`probe_agents()` walks every live `cua-driver`-matching process, reads its
PARENT's argv (the agent that spawned the bridge — `claude`/`codex`), and
pulls out the identity that matters: which agent program, its --session-id and
--model, and a short project hint (the parent's cwd, else the first slice of
its launch prompt). The daemon process itself (parented by launchd) is reported
with no agent — it is the hands, not a brain.

Everything here is defensive: processes vanish between the pgrep and the ps,
`ps`/`lsof` can be slow or blocked, and the driver may be down. Any of those
degrades a field to None rather than raising.
"""

from __future__ import annotations

import http.server
import json
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import driver

# Where the hub (src/ghosthands/hub.py) tees every MCP call, one JSONL file per
# agent. The dashboard tails these to show WHAT each agent is calling, not just
# who is connected. Empty/absent until an agent is registered through the hub.
HUB_LOG_DIR = Path.home() / ".ghosthands" / "hub"

# Matches both the daemon (`.../cua-driver serve`) and the per-agent MCP
# bridges (`.../cua-driver mcp ...`) that each connected agent spawns.
_PGREP_PATTERN = "cua-driver"

# Agent launcher basenames we recognise as a "brain" driving the hands.
_AGENT_PROGRAMS = ("claude", "codex")

_SESSION_RE = re.compile(r"--session-id[=\s]+(\S+)")
_MODEL_RE = re.compile(r"--model[=\s]+(\S+)")
# The launch prompt, when present, is appended as a trailing `# ...` chunk on
# the agent's argv. `ps` renders embedded newlines as the literal escape
# "\012"; collapse those so the hint is one readable line.
_PROMPT_RE = re.compile(r"#\s+(.*)$", re.DOTALL)

_PS_TIMEOUT = 4.0
_LSOF_TIMEOUT = 4.0


def _run(argv: list[str], timeout: float) -> str | None:
    """Run a read-only probe command, returning stripped stdout or None if it
    fails, times out, or the target has already vanished."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _pgrep_driver() -> list[int]:
    """PIDs of every process whose argv matches `cua-driver`. Empty when none
    run (pgrep exits non-zero) or pgrep is unavailable."""
    out = _run(["pgrep", "-f", _PGREP_PATTERN], _PS_TIMEOUT)
    if not out:
        return []
    pids: list[int] = []
    for tok in out.split():
        try:
            pids.append(int(tok))
        except ValueError:
            continue
    return pids


def _ppid_of(pid: int) -> int | None:
    out = _run(["ps", "-o", "ppid=", "-p", str(pid)], _PS_TIMEOUT)
    if not out:
        return None
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return None


def _command_of(pid: int) -> str | None:
    """Full argv string of a process (`ps -ww` to avoid width truncation)."""
    return _run(["ps", "-ww", "-o", "command=", "-p", str(pid)], _PS_TIMEOUT)


def _cwd_of(pid: int) -> str | None:
    """Working directory of a process via `lsof`, best-effort. Blocked or
    unavailable lsof simply yields None."""
    out = _run(
        ["lsof", "-a", "-d", "cwd", "-Fn", "-p", str(pid)], _LSOF_TIMEOUT
    )
    if not out:
        return None
    # -Fn field output: one `n<path>` line is the cwd name.
    for line in out.splitlines():
        if line.startswith("n") and len(line) > 1:
            return line[1:]
    return None


def _program_of(command: str | None) -> str | None:
    """The agent program name (claude/codex) from a parent's argv, taken from
    the executable's basename so a full path still matches.

    Matching is on a WORD boundary, not a raw prefix: `claude`, `claude-1.2`
    and `claude_wrapper` all map to "claude", but `claudette` and
    `codexterous` do NOT — a bare `startswith` would mislabel those. We split
    the basename on non-alphanumeric separators and compare the leading token."""
    if not command:
        return None
    parts = command.split()
    exe = parts[0] if parts else ""
    base = exe.rsplit("/", 1)[-1]
    # Leading token up to the first separator (-, _, ., space already gone).
    head = re.split(r"[^A-Za-z0-9]", base, maxsplit=1)[0]
    for prog in _AGENT_PROGRAMS:
        if head == prog:
            return prog
    return None


def _project_hint(command: str | None, cwd: str | None) -> str | None:
    """A short, human-legible "which project is this" string: the parent's cwd
    if known, otherwise the first ~80 chars of its launch prompt (the trailing
    `# ...` chunk on the argv)."""
    if cwd:
        return cwd
    if not command:
        return None
    m = _PROMPT_RE.search(command)
    if not m:
        return None
    prompt = m.group(1).replace("\\012", " ").replace("\n", " ")
    prompt = " ".join(prompt.split())
    if not prompt:
        return None
    return prompt[:80]


def _agent_record(
    pid: int,
    parent_pid: int | None,
    parent_cmd: str | None,
    cwd: str | None,
) -> dict:
    """Build one agent dict from already-gathered raw process facts.

    Pure: no subprocess, no I/O. This is the whole attribution logic — program
    match, session-id / model regex, project hint — factored out so it can be
    proven against synthetic `ps`/`lsof` output with the daemon DOWN."""
    return {
        "pid": pid,
        "parent_pid": parent_pid,
        "agent": _program_of(parent_cmd),
        "session_id": _first(_SESSION_RE, parent_cmd),
        "model": _first(_MODEL_RE, parent_cmd),
        "project": _project_hint(parent_cmd, cwd),
    }


def probe_agents(
    *,
    pgrep_driver: Callable[[], list[int]] | None = None,
    ppid_of: Callable[[int], int | None] | None = None,
    command_of: Callable[[int], str | None] | None = None,
    cwd_of: Callable[[int], str | None] | None = None,
) -> list[dict]:
    """Discover every agent currently attached to the hands.

    One entry per live `cua-driver`-matching process. For each, we read its
    PARENT's argv and surface the driving agent's identity:
        pid          the cua-driver process id
        parent_pid   the spawning process (the agent, or launchd for the daemon)
        agent        "claude"/"codex"/None — None means the daemon itself
        session_id   --session-id value, if the agent passed one
        model        --model value, if present
        project      cwd of the agent, else a prompt snippet, else None

    Fully defensive: a process that exits mid-probe drops out silently and any
    unreadable field degrades to None.

    The four probe callables default to the live `pgrep`/`ps`/`lsof` helpers.
    Tests inject synthetic ones to prove the attribution logic hermetically,
    with no cua-driver daemon and no connected agents required."""
    pgrep_driver = pgrep_driver or _pgrep_driver
    ppid_of = ppid_of or _ppid_of
    command_of = command_of or _command_of
    cwd_of = cwd_of or _cwd_of

    agents: list[dict] = []
    for pid in pgrep_driver():
        parent_pid = ppid_of(pid)
        parent_cmd = command_of(parent_pid) if parent_pid is not None else None
        agent = _program_of(parent_cmd)
        cwd = cwd_of(parent_pid) if (agent and parent_pid is not None) else None
        agents.append(_agent_record(pid, parent_pid, parent_cmd, cwd))
    return agents


def _first(pattern: re.Pattern[str], text: str | None) -> str | None:
    if not text:
        return None
    m = pattern.search(text)
    return m.group(1) if m else None


def probe_windows() -> list[dict]:
    """Windows the driver currently sees, summarised. Empty list if the driver
    is down or returns nothing — this never raises so the dashboard keeps
    polling through a daemon restart."""
    try:
        state = driver.call("list_windows")
    except driver.DriverError:
        return []
    if not isinstance(state, dict):
        return []
    out: list[dict] = []
    for w in state.get("windows", []) or []:
        if not isinstance(w, dict):
            continue
        out.append(
            {
                "window_id": w.get("window_id"),
                "pid": w.get("pid"),
                "app_name": w.get("app_name"),
                "title": w.get("title"),
            }
        )
    return out


def probe_calls(*, per_file: int = 200, total: int = 80,
                log_dir: Path | None = None) -> list[dict]:
    """Recent MCP calls the hub has teed, newest first. Tails every
    `<agent>.jsonl` under HUB_LOG_DIR and returns a flat, time-sorted list of
    {agent, ts, dir, method, summary}. Empty when no agent has run through the
    hub yet (the dir is absent) — this is the WHAT-are-they-doing feed that
    pairs with the WHO list. Never raises: an unreadable/garbage line is
    skipped."""
    d = log_dir or HUB_LOG_DIR
    if not d.is_dir():
        return []
    records: list[dict] = []
    for fp in sorted(d.glob("*.jsonl")):
        try:
            lines = fp.read_text(errors="replace").splitlines()[-per_file:]
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            records.append({
                "agent": r.get("agent"),
                "ts": r.get("ts"),
                "dir": r.get("dir"),
                "method": r.get("method"),
                "summary": r.get("params_summary"),
            })
    records.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return records[:total]


def state() -> dict[str, Any]:
    """The observability snapshot served at /api/state. `agents` is filtered to
    real brains (the bare cua-driver daemon is surfaced as `daemon_up`, not as a
    row); `calls` is the hub's live MCP-call feed."""
    all_agents = probe_agents()
    brains = [a for a in all_agents if a.get("agent")]
    daemon_up = any(a.get("agent") is None for a in all_agents)
    return {"agents": brains, "daemon_up": daemon_up, "calls": probe_calls()}


_DASHBOARD_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>GhostHands — who has the hands</title>
<style>
  :root { color-scheme: dark; }
  body { font: 14px/1.5 -apple-system, system-ui, sans-serif;
         margin: 0; padding: 1.5rem; background: #14161a; color: #e6e6e6; }
  h1 { font-size: 1.1rem; margin: 0 0 .25rem; }
  .sub { color: #8a8f98; margin: 0 0 1.25rem; }
  h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em;
       color: #8a8f98; margin: 1.5rem 0 .5rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #262a30;
           vertical-align: top; }
  th { color: #8a8f98; font-weight: 600; font-size: .75rem; text-transform: uppercase; }
  td.mono, td .mono { font-family: ui-monospace, Menlo, monospace; font-size: .82rem; }
  .pill { display: inline-block; padding: .05rem .45rem; border-radius: 999px;
          font-size: .72rem; font-weight: 600; }
  .agent { background: #1d3b2a; color: #6fe39c; }
  .daemon { background: #2a2230; color: #c79cff; }
  .none { color: #5b6068; }
  .empty { color: #5b6068; padding: .6rem; }
  .dot { display:inline-block; width:.5rem; height:.5rem; border-radius:50%;
         background:#6fe39c; margin-right:.4rem; }
  .up { color:#6fe39c; } .down { color:#e3766f; }
  .hint { color:#5b6068; font-weight:400; text-transform:none; letter-spacing:0; }
  .dir-req { color:#6fb4e3; } .dir-res { color:#8a8f98; }
  td.detail { color:#b6bcc6; max-width:46ch; overflow:hidden; text-overflow:ellipsis;
              white-space:nowrap; }
</style>
</head>
<body>
  <h1><span class="dot"></span>GhostHands monitor</h1>
  <p class="sub">Read-only — who has the hands and what they're calling. Polls every 2s.
     <span id="hands" class="mono"></span> · <span id="ts" class="mono"></span></p>

  <h2>Agents</h2>
  <table>
    <thead><tr><th>PID</th><th>Parent</th><th>Agent</th><th>Session</th>
      <th>Model</th><th>Project</th></tr></thead>
    <tbody id="agents"></tbody>
  </table>

  <h2>Live MCP calls <span class="hint" id="callsrc"></span></h2>
  <table>
    <thead><tr><th>Time</th><th>Agent</th><th>Dir</th><th>Method</th><th>Detail</th></tr></thead>
    <tbody id="calls"></tbody>
  </table>

<script>
function esc(v) {
  if (v === null || v === undefined) return '<span class="none">—</span>';
  return String(v).replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function clock(ts) {
  if (!ts) return '<span class="none">—</span>';
  return new Date(ts * 1000).toLocaleTimeString();
}
function render(d) {
  const ab = document.getElementById('agents');
  if (!d.agents.length) {
    ab.innerHTML = '<tr><td colspan="6" class="empty">no agents driving the hands</td></tr>';
  } else {
    ab.innerHTML = d.agents.map(a =>
      '<tr>' +
        '<td class="mono">' + esc(a.pid) + '</td>' +
        '<td class="mono">' + esc(a.parent_pid) + '</td>' +
        '<td><span class="pill agent">' + esc(a.agent) + '</span></td>' +
        '<td class="mono">' + esc(a.session_id) + '</td>' +
        '<td class="mono">' + esc(a.model) + '</td>' +
        '<td>' + esc(a.project) + '</td>' +
      '</tr>').join('');
  }
  const cb = document.getElementById('calls');
  if (!d.calls || !d.calls.length) {
    cb.innerHTML = '<tr><td colspan="5" class="empty">no hub-routed calls yet — ' +
      'register an agent through <span class="mono">ghosthands hub</span> to see its calls here</td></tr>';
    document.getElementById('callsrc').textContent = '';
  } else {
    cb.innerHTML = d.calls.map(c =>
      '<tr>' +
        '<td class="mono">' + clock(c.ts) + '</td>' +
        '<td>' + esc(c.agent) + '</td>' +
        '<td class="mono dir-' + esc(c.dir) + '">' + esc(c.dir) + '</td>' +
        '<td class="mono">' + esc(c.method) + '</td>' +
        '<td class="mono detail">' + esc(c.summary) + '</td>' +
      '</tr>').join('');
    document.getElementById('callsrc').textContent = '(' + d.calls.length + ')';
  }
  document.getElementById('hands').textContent =
    d.daemon_up ? 'hands: up' : 'hands: down';
  document.getElementById('hands').className = 'mono ' + (d.daemon_up ? 'up' : 'down');
  document.getElementById('ts').textContent =
    'updated ' + new Date().toLocaleTimeString();
}
async function tick() {
  try {
    const r = await fetch('/api/state', {cache: 'no-store'});
    render(await r.json());
  } catch (e) { /* daemon hiccup — keep polling */ }
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


class _MonitorHandler(http.server.BaseHTTPRequestHandler):
    """Serves the dashboard and the JSON state. Read-only: only GET, only two
    routes, no body parsing."""

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send(200, _DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            body = json.dumps(state()).encode()
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass


class MonitorServer:
    """A running dashboard server. `serve()` constructs one and blocks; tests
    construct it directly to start/stop on an ephemeral port."""

    def __init__(self, port: int = 7878, host: str = "127.0.0.1"):
        # Binding can fail (port already in use, no permission). Surface that as
        # a clean OSError out of __init__ with the address attached, rather than
        # a bare socket error with no context — and guarantee self.httpd is None
        # on failure so a caller's `finally: stop()` does not hit AttributeError.
        self.httpd: http.server.ThreadingHTTPServer | None = None
        try:
            self.httpd = http.server.ThreadingHTTPServer((host, port), _MonitorHandler)
        except OSError as e:
            raise OSError(f"cannot bind monitor to {host}:{port} — {e}") from e
        self.host, self.port = self.httpd.server_address[0], self.httpd.server_address[1]

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()


def serve(port: int = 7878) -> None:
    """Launch the dashboard and block. Open http://localhost:<port>/ in a
    browser; it polls /api/state every 2s. Ctrl-C to stop. No external assets,
    no auth — bind to localhost only (default), this exposes the local process
    table."""
    server = MonitorServer(port)
    print(f"GhostHands monitor on http://{server.host}:{server.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
