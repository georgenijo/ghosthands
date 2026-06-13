#!/usr/bin/env python3
"""Offline + opportunistic-live tests for the read-only monitor.

The monitor is a WATCHER: probe_agents() only reads the process table
(pgrep/ps/lsof) and serve() only reads it back over a localhost HTTP socket —
nothing is clicked, killed, or mutated, so this is safe to run anywhere.

The PROOF here is hermetic. The earlier version of this test was labelled
offline but hard-required a live cua-driver daemon AND ~3 connected Claude
agents, so a green run only meant "this particular machine, right now". That
proves nothing portable. So:

  * test_attribution_hermetic feeds SYNTHETIC pgrep/ps/lsof output (a committed
    fixture, no daemon, no agents) into probe_agents() via its injectable probe
    callables and asserts the full attribution — program match, --session-id,
    --model, project hint — field by field. THIS is the real proof.
  * test_program_match_word_boundary pins the hardening directly: claudette is
    NOT claude, codexterous is NOT codex.
  * test_serve_roundtrip exercises the real HTTP server end to end. It works
    offline because probe_windows() degrades to [] when the driver is down.
  * test_bind_in_use_is_clean proves a port-in-use raises a clean OSError out
    of MonitorServer.__init__ rather than a bare socket error or a half-built
    object whose stop() would AttributeError.
  * test_live_attribution_if_present keeps the original live assertion but
    GATES it: it only asserts ">=1 agent with a session id" when live
    cua-driver processes are actually detected; otherwise it skips cleanly.

Run: python3 tests/test_monitor.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import monitor  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "monitor_proctable.json"


def _load_fixture() -> dict:
    """The committed synthetic process-table snapshot, read relative to this
    test file so the proof is hermetic (no absolute paths, no live host)."""
    with _FIXTURE.open() as fh:
        return json.load(fh)


def _probes_from(fixture: dict):
    """Build the four injectable probe callables from the fixture, mimicking the
    raw return shapes of monitor's live pgrep/ps/lsof helpers. ps/lsof keys are
    JSON strings (object keys) — coerce to int, matching real pids."""
    ppid = {int(k): v for k, v in fixture["ppid"].items()}
    command = {int(k): v for k, v in fixture["command"].items()}
    cwd = {int(k): v for k, v in fixture["cwd"].items()}
    return {
        "pgrep_driver": lambda: list(fixture["pgrep"]),
        "ppid_of": lambda pid: ppid.get(pid),
        "command_of": lambda pid: command.get(pid),
        "cwd_of": lambda pid: cwd.get(pid),
    }


def test_attribution_hermetic() -> None:
    """probe_agents() attributes every field correctly from SYNTHETIC process
    output, with the cua-driver daemon DOWN and no agents connected. This is the
    portable proof of the parse logic, independent of who is connected."""
    fixture = _load_fixture()
    agents = monitor.probe_agents(**_probes_from(fixture))

    assert isinstance(agents, list), "probe_agents must return a list"
    assert len(agents) == len(fixture["expect"]), (
        f"expected {len(fixture['expect'])} entries, got {len(agents)}"
    )

    print(f"hermetic: {len(agents)} synthetic cua-driver process(es):")
    for got, expect in zip(agents, fixture["expect"]):
        assert set(got) >= {
            "pid", "parent_pid", "agent", "session_id", "model", "project",
        }, f"agent dict missing keys: {got}"
        print(
            f"  pid={got['pid']} parent={got['parent_pid']} agent={got['agent']} "
            f"session={got['session_id']} model={got['model']} "
            f"project={got['project']!r}"
        )
        assert got == expect, (
            f"attribution mismatch for pid {got['pid']}:\n"
            f"  got    {got}\n  expect {expect}"
        )

    # Spell out what each row proves, so a regression names itself.
    by_pid = {a["pid"]: a for a in agents}

    # Daemon: parented by launchd (pid 1) -> no brain, every field None.
    daemon = by_pid[101]
    assert daemon["agent"] is None and daemon["session_id"] is None, daemon

    # claude with session + model + cwd-as-project, full attribution.
    claude = by_pid[202]
    assert claude["agent"] == "claude"
    assert claude["session_id"] == "a4059f4e-7744-4f89-b912-5c1dcf310797"
    assert claude["model"] == "claude-opus-4-8[1m]"
    assert claude["project"] == "/Users/dev/Documents/code/ghosthands"

    # codex recognised; model parsed; project from cwd.
    codex = by_pid[303]
    assert codex["agent"] == "codex" and codex["model"] == "gpt-5-codex"

    # HARDENING: 'claudette' / 'codexterous' must NOT be mislabelled. The argv
    # regexes still see their --session-id/--model (program-agnostic), but the
    # AGENT attribution — the field that ties hands to a brain — is None.
    assert by_pid[404]["agent"] is None, "claudette mislabelled as claude"
    assert by_pid[505]["agent"] is None, "codexterous mislabelled as codex"

    # Project hint via the launch-prompt fallback: no cwd, so the trailing
    # `# ...` chunk on the argv is collapsed (the literal \\012 newline escape
    # and runs of spaces become single spaces) into a one-line hint.
    promptish = by_pid[606]
    assert promptish["agent"] == "claude"
    assert promptish["project"] == "research the cua-driver internals and summarize", (
        promptish["project"]
    )
    print("hermetic: attribution + word-boundary + prompt-hint all verified")


def test_hub_routed_attribution() -> None:
    """With the hub in the path the tree is claude -> ghosthands hub ->
    cua-driver, so the brain is TWO hops above the leaf. probe_agents must climb
    past the proxy and attribute to claude (with its session/model), not to the
    python hub process. Also proves the raw 1-hop case and the bare daemon
    (launchd parent) still resolve correctly in the same pass."""
    # pid 900 = cua-driver leaf under the hub; 901 = ghosthands hub; 902 = claude
    # pid 910 = cua-driver leaf wired raw; 911 = claude (direct parent)
    # pid 920 = bare daemon; 1 = launchd
    ppid = {900: 901, 901: 902, 902: 5000, 910: 911, 911: 5000, 920: 1}
    command = {
        901: "/Users/me/.local/bin/ghosthands hub",
        902: "/Users/me/.local/bin/claude --session-id hub-sess --model claude-opus-4-8",
        911: "/Users/me/.local/bin/claude --session-id raw-sess --model claude-opus-4-8",
        920: "/Users/me/.local/bin/cua-driver serve",
    }
    agents = monitor.probe_agents(
        pgrep_driver=lambda: [900, 910, 920],
        ppid_of=lambda pid: ppid.get(pid),
        command_of=lambda pid: command.get(pid),
        cwd_of=lambda pid: None,
    )
    by_pid = {a["pid"]: a for a in agents}

    # Hub-routed leaf attributes to claude two hops up — NOT to the hub.
    hub_leaf = by_pid[900]
    assert hub_leaf["agent"] == "claude", f"hub climb failed: {hub_leaf}"
    assert hub_leaf["parent_pid"] == 902, "should report the brain pid, not the hub"
    assert hub_leaf["session_id"] == "hub-sess", "session must come from claude, not hub"

    # Raw leaf still works (1 hop).
    assert by_pid[910]["agent"] == "claude" and by_pid[910]["session_id"] == "raw-sess"

    # Bare daemon (launchd parent) stays agent=None so daemon_up is unaffected.
    assert by_pid[920]["agent"] is None, by_pid[920]
    print("hub-routed: brain attributed through the proxy (2 hops); raw + daemon intact")


def test_program_match_word_boundary() -> None:
    """_program_of matches on a word boundary, not a raw prefix. Pins the fix
    for the startswith mislabelling directly, including path/suffix variants."""
    cases = [
        ("/usr/local/bin/claude --session-id x", "claude"),
        ("/Users/g/.local/bin/claude --model m", "claude"),
        ("claude-1.2.3 --foo", "claude"),       # version suffix still matches
        ("claude_wrapper run", "claude"),        # underscore separator matches
        ("/opt/homebrew/bin/codex serve", "codex"),
        ("/usr/bin/claudette --foo", None),      # was mislabelled claude
        ("codexterous run", None),               # was mislabelled codex
        ("/sbin/launchd", None),
        ("", None),
        (None, None),
    ]
    for command, expect in cases:
        got = monitor._program_of(command)
        assert got == expect, f"_program_of({command!r}) -> {got!r}, want {expect!r}"
    print(f"word-boundary: {len(cases)} program-match cases pass "
          "(claudette!=claude, codexterous!=codex)")


def test_serve_roundtrip() -> None:
    """The real HTTP server end to end. Offline-safe: probe_agents/probe_calls
    degrade to [] when the driver is down and no hub logs exist, so /api/state
    still serves the agents + calls keys."""
    server = monitor.MonitorServer(port=0)  # ephemeral port
    server.start_background()
    try:
        url = f"http://{server.host}:{server.port}/api/state"
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read())
        assert "agents" in payload, "/api/state missing 'agents'"
        assert "calls" in payload, "/api/state missing 'calls'"
        assert "daemon_up" in payload, "/api/state missing 'daemon_up'"
        assert isinstance(payload["agents"], list)
        assert isinstance(payload["calls"], list)
        print(f"\n/api/state on :{server.port} -> "
              f"{len(payload['agents'])} agents, {len(payload['calls'])} calls, "
              f"daemon_up={payload['daemon_up']}")

        # The dashboard root must be a self-contained HTML page (no external
        # assets) that wires up the poll.
        root = f"http://{server.host}:{server.port}/"
        with urllib.request.urlopen(root, timeout=10) as resp:
            html = resp.read().decode()
        assert "<html" in html.lower() and "/api/state" in html, \
            "dashboard root is not the self-contained polling page"
        assert "http://" not in html.replace(f"http://{server.host}", ""), \
            "dashboard pulls an external asset — must be self-contained"
        print("dashboard root served: self-contained HTML, polls /api/state")
    finally:
        server.stop()


def test_bind_in_use_is_clean() -> None:
    """A second bind to an in-use port raises a clean OSError out of __init__
    (not a half-built object), and the first server's stop() still works."""
    first = monitor.MonitorServer(port=0)
    first.start_background()
    try:
        in_use = first.port
        raised = False
        try:
            monitor.MonitorServer(port=in_use)
        except OSError as e:
            raised = True
            assert str(in_use) in str(e), f"error lacks the address: {e}"
        assert raised, f"expected OSError binding the in-use port {in_use}"
        print(f"\nbind-in-use: second bind to :{in_use} raised cleanly")
    finally:
        first.stop()


def _live_pids() -> list[int]:
    """Live cua-driver pids, via the module's own pgrep helper. Empty list when
    the daemon/agents are not running — used purely to gate the live assertion."""
    try:
        return monitor._pgrep_driver()
    except Exception:
        return []


def test_live_attribution_if_present() -> None:
    """Original live assertion, now GATED. Only when real cua-driver processes
    are detected do we demand at least one agent surface a session id or a
    recognised agent parent; otherwise skip cleanly so the test is portable."""
    pids = _live_pids()
    if not pids:
        print("\nlive: no cua-driver processes detected — skipped")
        return

    agents = monitor.probe_agents()
    assert isinstance(agents, list)
    print(f"\nlive: discovered {len(agents)} cua-driver process(es):")
    for a in agents:
        print(f"  pid={a['pid']} parent={a['parent_pid']} agent={a['agent']} "
              f"session={a['session_id']} model={a['model']} project={a['project']!r}")
    assert agents, "pgrep found cua-driver pids but probe_agents returned none"

    have_signal = any(a["session_id"] or a["agent"] for a in agents)
    assert have_signal, (
        "live cua-driver processes present but none tied to a brain "
        "(no session id, no recognised agent parent)"
    )
    print("live: at least one process attributed to a brain")


def main() -> int:
    test_attribution_hermetic()
    test_hub_routed_attribution()
    test_program_match_word_boundary()
    test_serve_roundtrip()
    test_bind_in_use_is_clean()
    test_live_attribution_if_present()
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
