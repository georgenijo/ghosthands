"""Thin layer over the `cua-driver` CLI.

Every Cua tool call goes through `cua-driver call <tool> <json>`, which
proxies to the persistent CuaDriver daemon over its Unix socket (and
auto-launches the daemon if it is not running). Responses are JSON for
query tools and plain text for action tools — `call()` returns the parsed
JSON when possible, otherwise the raw text.

Error classification (DESIGN.md §8):
- "Element index N not found in cache"  -> StaleIndexError (re-snapshot and retry)
- EAGAIN / "daemon closed connection"   -> TransientDriverError (the action may
  still have landed; verify state before blindly retrying)
- anything else                          -> DriverError (fatal for this call)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

DRIVER_BIN = os.environ.get(
    "GHOSTHANDS_DRIVER_BIN",
    os.path.expanduser("~/.local/bin/cua-driver"),
)

DEFAULT_TIMEOUT = 30.0

_TRANSIENT_MARKERS = (
    "eagain",
    "resource temporarily unavailable",
    "daemon closed connection",
    "transport error",
    "connection refused",
    "broken pipe",
)

_STALE_MARKERS = ("not found in cache",)


class DriverError(RuntimeError):
    """A cua-driver call failed and is not known to be recoverable."""

    def __init__(self, tool: str, message: str):
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class TransientDriverError(DriverError):
    """Daemon flakiness (EAGAIN / closed connection). The action may have
    landed anyway — verify state before retrying blind."""


class StaleIndexError(DriverError):
    """The element_index cache expired or belongs to another window's index
    set. Re-snapshot and resolve the element again."""


def classify_error(tool: str, message: str) -> DriverError:
    lowered = message.lower()
    if any(m in lowered for m in _STALE_MARKERS):
        return StaleIndexError(tool, message)
    if any(m in lowered for m in _TRANSIENT_MARKERS):
        return TransientDriverError(tool, message)
    return DriverError(tool, message)


def driver_available() -> bool:
    return os.path.exists(DRIVER_BIN) or shutil.which("cua-driver") is not None


def call(tool: str, args: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Invoke one Cua tool. Returns parsed JSON when the tool emits JSON,
    otherwise the raw text response. Raises a classified DriverError on
    failure. Never injects a `session` — GhostHands runs cursor-less until
    Screen Recording is granted (DESIGN.md §8.3)."""
    payload = json.dumps(args or {})
    try:
        proc = subprocess.run(
            [DRIVER_BIN, "call", tool, payload],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise DriverError(tool, f"cua-driver binary not found at {DRIVER_BIN}")
    except subprocess.TimeoutExpired:
        raise TransientDriverError(tool, f"call timed out after {timeout}s")

    out = proc.stdout.strip()
    err = proc.stderr.strip()
    if proc.returncode != 0:
        raise classify_error(tool, out or err or f"exit code {proc.returncode}")

    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


class PendingCall:
    """An in-flight action fired with `fire()`. The daemon executes an AX
    action within ~250ms but pads the client response to ~1.1s (measured on
    cua-driver 0.5.1); firing without blocking reclaims that padding. Call
    `collect()` (or `error()`) later to classify any failure."""

    def __init__(self, tool: str, proc: subprocess.Popen):
        self.tool = tool
        self.proc = proc

    def collect(self, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Block until the call returns; raise the classified DriverError on
        failure, exactly like `call()`."""
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise TransientDriverError(self.tool, f"call timed out after {timeout}s")
        out = (out or "").strip()
        err = (err or "").strip()
        if self.proc.returncode != 0:
            raise classify_error(self.tool, out or err or f"exit code {self.proc.returncode}")
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out

    def error(self, timeout: float = DEFAULT_TIMEOUT) -> DriverError | None:
        """Like collect() but returns the classified error instead of raising
        (None on success). For post-hoc batch checks."""
        try:
            self.collect(timeout=timeout)
            return None
        except DriverError as e:
            return e


def fire(tool: str, args: dict[str, Any] | None = None) -> PendingCall:
    """Issue one Cua ACTION without waiting for the daemon's padded response.
    The action itself lands fast; use the returned PendingCall to classify
    errors after the fact (e.g. during the post-action settle). Use `call()`
    for query tools — their responses are needed and return quickly anyway."""
    payload = json.dumps(args or {})
    try:
        proc = subprocess.Popen(
            [DRIVER_BIN, "call", tool, payload],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise DriverError(tool, f"cua-driver binary not found at {DRIVER_BIN}")
    return PendingCall(tool, proc)


def cli(*argv: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """Run a non-tool cua-driver subcommand (status, permissions, ...).
    Returns (exit_code, combined_output)."""
    try:
        proc = subprocess.run(
            [DRIVER_BIN, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f"cua-driver binary not found at {DRIVER_BIN}"
    except subprocess.TimeoutExpired:
        return 124, f"'{' '.join(argv)}' timed out after {timeout}s"
    output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return proc.returncode, output
