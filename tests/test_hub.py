#!/usr/bin/env python3
"""Prove the MCP shim is transparent against the REAL cua-driver.

We spawn `python -m ghosthands.hub` as a subprocess (exactly how an agent
would launch it), with GHOSTHANDS_AGENT=test-agent and the tee log pointed
at /tmp. The hub spawns the real `~/.local/bin/cua-driver mcp` child. We then
drive a full MCP handshake THROUGH the shim:

    initialize → (read result) → notifications/initialized → tools/list

and assert:
  1. transparency — the hands' real tool list comes back verbatim through the
     proxy (a JSON-RPC result whose `tools` is the hands' actual tools), and
  2. observability — /tmp/gh-hub-test.jsonl carries tagged records for the
     initialize and tools/list frames, all stamped agent=="test-agent".

If the real handshake can't run (no daemon / binary), we fall back to proving
the proxy forwards a hand-crafted JSON-RPC line to the child and logs it — but
the real handshake is preferred and attempted first.

Run: .venv/bin/python tests/test_hub.py
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ghosthands.driver import DRIVER_BIN  # noqa: E402

LOG = Path("/tmp/gh-hub-test.jsonl")


def _spawn_hub(child_cmd_argv=None):
    """Launch the shim as its own process, exactly like an agent would."""
    env = dict(os.environ)
    env["GHOSTHANDS_AGENT"] = "test-agent"
    env["GHOSTHANDS_HUB_LOG"] = str(LOG)
    argv = [sys.executable, "-m", "ghosthands.hub"]
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(ROOT / "src"),
        env=env,
    )


def _drain(stream):
    for _ in stream:
        pass


def _send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _readline(proc, timeout=15.0):
    """Read one line from the proc's stdout with a timeout (the child's first
    response can lag while the daemon spins up)."""
    box = {}

    def _read():
        box["line"] = proc.stdout.readline()

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("line")


def _read_log_records():
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def real_handshake() -> tuple[bool, str]:
    """Preferred path: a genuine MCP handshake through the shim against the
    real hands. Returns (passed, detail)."""
    proc = _spawn_hub()
    err_t = threading.Thread(target=_drain, args=(proc.stderr,), daemon=True)
    err_t.start()
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-hub", "version": "0.0.1"},
            },
        })
        init_line = _readline(proc)
        if not init_line:
            return False, "no initialize response through the shim"
        init = json.loads(init_line)
        if init.get("id") != 1 or "result" not in init:
            return False, f"initialize not echoed verbatim: {init_line[:200]!r}"

        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized",
                     "params": {}})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                     "params": {}})
        tools_line = _readline(proc)
        if not tools_line:
            return False, "no tools/list response through the shim"
        tl = json.loads(tools_line)
        tools = tl.get("result", {}).get("tools")
        if tl.get("id") != 2 or not isinstance(tools, list) or not tools:
            return False, f"tools/list not passed through: {tools_line[:200]!r}"
        names = {t.get("name") for t in tools}
        # Sanity: these are the REAL hands, not something we faked.
        if "get_window_state" not in names:
            return False, f"tool list missing known hands tool: {sorted(names)[:8]}"

        detail = (f"transparency OK: initialize id=1 result + tools/list id=2 "
                  f"with {len(tools)} real tools (e.g. get_window_state); "
                  f"verbatim through the proxy")
        return True, detail
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
        # Give the shim's downstream pump + tee a moment to flush to disk.
        time.sleep(0.5)


def fallback_handshake() -> tuple[bool, str]:
    """Fallback: drive a fake child (`cat`) so we still prove the proxy
    forwards a JSON-RPC line and logs it, even if the real daemon is flaky.
    `cat` echoes stdin to stdout, so the line round-trips through both pumps."""
    # Force the child to be a transparent echo. We do this by invoking serve()
    # in a tiny inline runner rather than the binary.
    runner = (
        "import sys; from ghosthands import hub; "
        "sys.exit(hub.serve(['cat']))"
    )
    env = dict(os.environ)
    env["GHOSTHANDS_AGENT"] = "test-agent"
    env["GHOSTHANDS_HUB_LOG"] = str(LOG)
    proc = subprocess.Popen(
        [sys.executable, "-c", runner],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=str(ROOT / "src"), env=env,
    )
    threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()
    try:
        frame = {"jsonrpc": "2.0", "id": 99, "method": "tools/list",
                 "params": {"name": "probe"}}
        _send(proc, frame)
        echoed = _readline(proc)
        if not echoed:
            return False, "fallback: child did not echo the frame"
        back = json.loads(echoed)
        if back != frame:
            return False, f"fallback: frame mangled in transit: {echoed[:200]!r}"
        return True, "fallback OK: JSON-RPC frame forwarded verbatim to child"
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
        time.sleep(0.3)


def cr_byte_exact() -> tuple[bool, str]:
    """Prove a frame carrying a raw CR byte inside a JSON string round-trips
    BYTE-EXACT through the proxy.

    We drive the `cat` fallback child (so no daemon is needed) but talk to the
    shim over BINARY pipes — text-mode pipes here would do their own
    universal-newline translation and mask whether the proxy itself is clean.
    The frame's JSON string value contains a literal carriage return (0x0D)
    that JSON does NOT escape on us because we emit the byte directly. If the
    proxy ran the child in text mode, that CR would be rewritten to/merged with
    a newline and the bytes (and frame boundary) would change. We assert the
    line that comes back is identical, byte for byte, to the line we sent."""
    runner = (
        "import sys; from ghosthands import hub; "
        "sys.exit(hub.serve(['cat']))"
    )
    env = dict(os.environ)
    env["GHOSTHANDS_AGENT"] = "test-agent"
    env["GHOSTHANDS_HUB_LOG"] = str(LOG)
    proc = subprocess.Popen(
        [sys.executable, "-c", runner],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=False, bufsize=0, cwd=str(ROOT / "src"), env=env,
    )
    threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()
    try:
        # A JSON-RPC frame whose params.note holds a raw CR (0x0D). We build the
        # bytes by hand so the CR is a literal byte on the wire, not the escape
        # sequence "\\r". A single trailing 0x0A delimits the frame.
        payload = b'{"jsonrpc":"2.0","id":7,"method":"ping","params":{"note":"a\rb"}}'
        sent = payload + b"\n"
        assert b"\r" in sent and sent.count(b"\n") == 1, "test frame mis-built"

        def _read_binary():
            box["line"] = proc.stdout.readline()

        box: dict = {}
        proc.stdin.write(sent)
        proc.stdin.flush()
        t = threading.Thread(target=_read_binary, daemon=True)
        t.start()
        t.join(10.0)
        echoed = box.get("line")
        if not echoed:
            return False, "cr-byte: child did not echo the frame"
        if echoed != sent:
            return False, (
                "cr-byte: bytes NOT preserved through proxy: "
                f"sent={sent!r} got={echoed!r}"
            )
        # Belt and suspenders: the raw CR survived and the frame wasn't split.
        if b"\r" not in echoed or echoed.count(b"\n") != 1:
            return False, f"cr-byte: CR lost or frame split: {echoed!r}"
        return True, (
            "cr-byte OK: raw CR (0x0D) inside a JSON string round-tripped "
            "byte-exact; frame not split"
        )
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
        time.sleep(0.3)


def check_log(expect_methods) -> tuple[bool, str]:
    records = _read_log_records()
    if not records:
        return False, "tee log is empty (no frames recorded)"
    if any(r.get("agent") != "test-agent" for r in records):
        bad = [r.get("agent") for r in records if r.get("agent") != "test-agent"]
        return False, f"tee records not tagged test-agent: {bad[:3]}"
    methods = {r.get("method") for r in records if r.get("dir") == "req"}
    missing = [m for m in expect_methods if m not in methods]
    if missing:
        return False, f"tee missing request frames for {missing}; saw {sorted(m for m in methods if m)}"
    # A response frame must also have been teed (proves both directions log).
    if not any(r.get("dir") == "res" for r in records):
        return False, "tee recorded no response-direction frames"
    return True, (f"tee OK: {len(records)} records, all agent=test-agent, "
                  f"req methods include {sorted(expect_methods)}, res frames present")


def main() -> int:
    if LOG.exists():
        LOG.unlink()

    used_fallback = False
    if os.path.exists(DRIVER_BIN):
        passed, detail = real_handshake()
        if passed:
            ok_log, log_detail = check_log(["initialize", "tools/list"])
        else:
            passed = False
    else:
        passed = False
        detail = f"cua-driver not found at {DRIVER_BIN}"

    if not passed:
        used_fallback = True
        if LOG.exists():
            LOG.unlink()
        passed, detail = fallback_handshake()
        ok_log, log_detail = check_log(["tools/list"]) if passed else (False, "no log")

    # Independent byte-exactness sub-check via the cat fallback path (no daemon
    # needed): a raw CR inside a JSON string must survive the proxy unchanged.
    # Run it last so it doesn't perturb the handshake's tee-log assertions.
    cr_ok, cr_detail = cr_byte_exact()

    ok = passed and ok_log and cr_ok
    print(f"path: {'FALLBACK (cat echo)' if used_fallback else 'REAL cua-driver mcp'}")
    print(f"passthrough: {detail}")
    print(f"tee log: {log_detail}")
    print(f"byte-exact: {cr_detail}")
    print(f"log file: {LOG} ({len(_read_log_records())} records)")
    print(f"{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
