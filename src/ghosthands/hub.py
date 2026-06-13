"""Transparent stdio MCP proxy — the GhostHands "shim".

The parent agent (Claude Code / Codex / …) speaks MCP to *this* process
instead of directly to the hands. The shim spawns the real hands
(`<DRIVER_BIN> mcp`) as a child and forwards every byte both ways
VERBATIM, so to the agent it is indistinguishable from talking to
cua-driver directly. Alongside the passthrough it TEES each JSON-RPC
frame to an append-only JSONL log tagged with the agent's identity — a
per-agent audit trail of who asked the hands to do what.

Framing: MCP over stdio is newline-delimited JSON-RPC 2.0 — exactly one
JSON object per line, each direction. We pump whole lines as raw BYTES, so a
line is a frame; we never re-serialize it (forward the original bytes) so the
proxy cannot alter or reorder anything on the wire. The child runs in binary
mode (text=False, bufsize=0) and the pumps read/write bytes, so Python's
universal-newline translation never touches a frame — a raw CR (or CRLF)
embedded inside a JSON string round-trips byte-exact. Decoding to ``str``
happens ONLY inside the tee, purely for the human-readable log.

Tee discipline (non-negotiable): the tee is best-effort and must NEVER
block the pump or corrupt the stream. Every parse/write the tee does is
wrapped so a malformed frame, a full disk, or a permission error degrades
to "no log line" — never to a dropped or mangled MCP frame. The agent's
hands keep working even if logging is broken.

Wire it up: register a wrapper that execs `python -m ghosthands.hub` as
the MCP command (see module __main__ and the integrator notes), set
GHOSTHANDS_AGENT to the agent's id, and the hands appear unchanged while
every call is logged to ~/.ghosthands/hub/<agent>.jsonl.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from .driver import DRIVER_BIN

AGENT = os.environ.get("GHOSTHANDS_AGENT", "unknown")

_SUMMARY_MAX = 200


def _log_path() -> Path:
    """Where to tee. GHOSTHANDS_HUB_LOG overrides; default is a per-agent
    file under ~/.ghosthands/hub/. Parent dirs are created lazily."""
    override = os.environ.get("GHOSTHANDS_HUB_LOG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ghosthands" / "hub" / f"{AGENT}.jsonl"


def _truncate(text: str, limit: int = _SUMMARY_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _params_summary(frame: dict) -> str:
    """A short human-readable gist of a frame's payload: for tool calls the
    tool name plus a clipped repr of its arguments, otherwise a clipped repr
    of params/result/error. Kept tiny — the raw frame already has the full
    bytes if anyone needs them (via raw_len + the originals on the wire)."""
    params = frame.get("params")
    if isinstance(params, dict):
        name = params.get("name")
        if name is not None:  # tools/call shape: {name, arguments}
            args = params.get("arguments", {})
            return _truncate(f"{name} {args!r}")
        return _truncate(repr(params))
    for key in ("result", "error"):
        if key in frame:
            return _truncate(f"{key}={frame[key]!r}")
    return ""


def _tee_record(agent: str, direction: str, line: bytes) -> dict | None:
    """Build the tee record for one wire line, or None if the line isn't a
    JSON-RPC frame we can summarize (we still forwarded the raw bytes; only
    the *log* skips it).

    `line` is the verbatim wire bytes; we decode here (utf-8,
    errors="replace") solely for logging/summary. The bytes themselves are
    already on their way to the destination untouched — decoding cannot
    affect the forwarded stream."""
    raw_len = len(line)
    try:
        frame = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(frame, dict):
        return None
    record: dict = {
        "ts": time.time(),
        "agent": agent,
        "dir": direction,
        "raw_len": raw_len,
    }
    if "method" in frame:  # requests and notifications carry a method
        record["method"] = frame["method"]
    if "id" in frame:
        record["id"] = frame["id"]
    summary = _params_summary(frame)
    if summary:
        record["params_summary"] = summary
    return record


class _Tee:
    """Append-only JSONL sink, opened lazily and guarded so a logging fault
    can never propagate into the MCP pumps. A single lock serializes the two
    pump threads' writes so interleaved frames stay one-record-per-line."""

    def __init__(self, path: Path, agent: str):
        self.path = path
        self.agent = agent
        self._fh: IO[str] | None = None
        self._lock = threading.Lock()
        self._broken = False

    def _ensure_open(self) -> None:
        if self._fh is None and not self._broken:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, direction: str, line: bytes) -> None:
        record = _tee_record(self.agent, direction, line)
        if record is None:
            return
        with self._lock:
            if self._broken:
                return
            try:
                self._ensure_open()
                assert self._fh is not None
                self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._fh.flush()
            except Exception:  # noqa: BLE001 — logging must never break the proxy
                self._broken = True

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


def _pump(src: IO[bytes], dst: IO[bytes], tee: _Tee, direction: str) -> None:
    """Forward whole lines src→dst byte-for-byte, teeing each. One thread per
    direction. Both streams are BINARY: we ``readline`` raw bytes (frame
    boundaries for the tee) and ``write`` those exact bytes downstream — no
    universal-newline translation, no re-serialization. A frame carrying a
    raw CR (or CRLF) inside a JSON string therefore arrives byte-identical.
    Bytes in == bytes out."""
    try:
        for line in iter(src.readline, b""):
            try:
                dst.write(line)
                dst.flush()
            except OSError:
                # The far end closed/reset (agent quit, child died, ECONNRESET
                # or EAGAIN at teardown). Stop pumping this direction without a
                # traceback; the main thread reaps the child. (OSError covers
                # BrokenPipeError; ValueError is raised on a closed file but is
                # benign here — we are tearing down anyway.)
                break
            except ValueError:
                break
            tee.write(direction, line)
    finally:
        # Signal EOF downstream so the peer's pump can drain and exit.
        try:
            dst.close()
        except OSError:
            pass


def serve(child_cmd: list[str] | None = None) -> int:
    """Run the proxy until the child exits or stdin closes. Spawns the real
    hands (`<DRIVER_BIN> mcp` by default), pumps stdin↔child both ways, and
    tees frames to the per-agent JSONL log. Returns the child's exit code."""
    cmd = child_cmd if child_cmd is not None else [DRIVER_BIN, "mcp"]
    tee = _Tee(_log_path(), AGENT)

    child = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,  # let the child's stderr (banners, logs) reach our stderr
        text=False,  # BINARY: forward raw bytes, no universal-newline rewrite
        bufsize=0,  # unbuffered — frames go out the instant we write them
    )
    assert child.stdin is not None and child.stdout is not None

    # Pump the BINARY underlying buffers of our own std streams so a raw CR/CRLF
    # inside a JSON string is never translated on the way through.
    up = threading.Thread(
        target=_pump,
        args=(sys.stdin.buffer, child.stdin, tee, "req"),
        daemon=True,
    )
    down = threading.Thread(
        target=_pump,
        args=(child.stdout, sys.stdout.buffer, tee, "res"),
        daemon=True,
    )
    up.start()
    down.start()

    code = child.wait()
    # The downstream pump ends when the child closes stdout; give it a beat to
    # flush the last frames. The upstream pump is a daemon — once the child is
    # gone there is nowhere to forward, so we don't wait on stdin.
    down.join(timeout=2.0)
    tee.close()
    return code


def main() -> int:
    return serve()


if __name__ == "__main__":
    sys.exit(main())
