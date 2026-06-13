"""DOM tier for Chromium (Brave) over the Chrome DevTools Protocol.

The AX tier (ownloop/actions) is the right hammer for native AppKit apps, but
it is the wrong one for a Chromium web view, where it loses on four counts:

- *Background tabs.* AX only exposes the frontmost tab's web area; a backgrounded
  tab reads as empty chrome. CDP addresses every tab by id without fronting one
  (DESIGN §"no-foreground contract") — `list_targets` proves this alone.
- *AXPress vs React.* An AX press on a Chromium node dispatches a synthetic
  accessibility action that React's synthetic-event system frequently ignores;
  a real `element.click()` in page context fires the handlers the framework
  actually listens to.
- *Hidden <select> options.* A native popup menu's options never enter the AX
  tree until the menu opens; in the DOM they are always queryable and settable.
- *Type-without-focus.* AX typing needs a focused field; CDP sets `.value` and
  dispatches `input`/`change` on any element, focused or not.

So this tier drives Brave through CDP instead. Brave is launched with
`--remote-debugging-port` (cua-driver passes `electron_debugging_port` straight
through to Chromium), exposing an HTTP endpoint that lists every tab and a
per-tab WebSocket for the protocol itself.

The project is stdlib-only by design (see README); rather than add a websocket
dependency we speak just enough of RFC 6455 by hand — an HTTP Upgrade handshake
plus masked client frames — which is all CDP's JSON-RPC needs.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import driver

DEFAULT_PORT = 9333
DEFAULT_BUNDLE_ID = "com.brave.Browser"

# RFC 6455 opcodes we care about.
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

# A CDP message can be large (a serialized DOM result); read in chunks.
_RECV_CHUNK = 65536


class WebTierError(RuntimeError):
    """A CDP / websocket operation failed."""


# --------------------------------------------------------------------------- #
# Launch + target discovery (no fronting)                                      #
# --------------------------------------------------------------------------- #

def launch_web(url: str, port: int = DEFAULT_PORT,
               bundle_id: str = DEFAULT_BUNDLE_ID) -> Any:
    """Launch Brave with the remote-debugging port open and the page loaded.

    `electron_debugging_port` is the knob cua-driver forwards to Chromium as
    `--remote-debugging-port`, which is what enables the whole CDP surface used
    below. Returns the raw launch result (pid + windows)."""
    return driver.call("launch_app", {
        "bundle_id": bundle_id,
        "electron_debugging_port": port,
        "urls": [url],
    })


def list_targets(port: int = DEFAULT_PORT) -> list[dict]:
    """Every debuggable tab as reported by `/json/list`, WITHOUT fronting any
    of them. Each entry carries `id`, `url`, `title` and the per-tab
    `webSocketDebuggerUrl` used to attach. This is the proof of per-tab
    addressing: a backgrounded tab shows up here in full while the AX tree
    would read it as empty."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/list")
    # Wrap BOTH the HTTP read and the JSON decode: a transport failure raises
    # OSError/socket.timeout, but a 200 with a malformed/empty/non-JSON body
    # raises json.JSONDecodeError (a ValueError subclass) from json.loads —
    # that used to leak out raw. Normalise the whole lot to WebTierError.
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read())
    except (OSError, socket.timeout, json.JSONDecodeError, ValueError) as e:
        raise WebTierError(f"/json/list on port {port} failed: {e}") from e
    # Only 'page' targets are real tabs (service workers / iframes also appear).
    return [t for t in data if t.get("type", "page") == "page"]


def target_for_url(port: int, url_contains: str) -> dict:
    """The first page target whose URL contains `url_contains`. Lets a caller
    bind to a specific background tab by URL without ordering assumptions."""
    for t in list_targets(port):
        if url_contains in t.get("url", ""):
            return t
    raise WebTierError(f"no target matching {url_contains!r} on port {port}")


# --------------------------------------------------------------------------- #
# Minimal RFC 6455 websocket client (stdlib only)                             #
# --------------------------------------------------------------------------- #

def encode_frame(payload: bytes, *, opcode: int = _OP_TEXT,
                 mask: bytes | None = None) -> bytes:
    """Encode one final, masked client frame (RFC 6455 §5).

    Client-to-server frames MUST be masked. We support the two length forms a
    CDP request ever needs: the 7-bit form (<126 bytes) and the 16-bit
    extended form (126..65535, signalled by length byte 126). `mask` is
    overridable so the offline test can assert a fixed, known masking."""
    if mask is None:
        mask = os.urandom(4)
    if len(mask) != 4:
        raise ValueError("mask must be exactly 4 bytes")

    fin_op = 0x80 | (opcode & 0x0F)
    length = len(payload)
    header = bytearray([fin_op])
    if length < 126:
        header.append(0x80 | length)            # MASK bit + 7-bit length
    elif length < 65536:
        header.append(0x80 | 126)               # MASK bit + 16-bit length flag
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)               # MASK bit + 64-bit length flag
        header += struct.pack("!Q", length)
    header += mask
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return bytes(header) + masked


def decode_frame(data: bytes) -> tuple[int, bytes, int]:
    """Decode one frame from the front of `data`.

    Returns `(opcode, payload, consumed)` where `consumed` is the number of
    bytes the frame occupied — so a caller can slice it off and keep any
    trailing bytes for the next frame. Raises `IndexError` (caught by the
    reader as "need more bytes") when `data` does not yet hold a full frame.

    Server-to-client frames are unmasked per RFC 6455 §5.1, but this also
    unmasks masked frames so it round-trips anything `encode_frame` produces —
    which is exactly what the offline test relies on."""
    if len(data) < 2:
        raise IndexError("incomplete header")
    b0, b1 = data[0], data[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        if len(data) < offset + 2:
            raise IndexError("incomplete 16-bit length")
        (length,) = struct.unpack("!H", data[offset:offset + 2])
        offset += 2
    elif length == 127:
        if len(data) < offset + 8:
            raise IndexError("incomplete 64-bit length")
        (length,) = struct.unpack("!Q", data[offset:offset + 8])
        offset += 8
    mask = b""
    if masked:
        if len(data) < offset + 4:
            raise IndexError("incomplete mask")
        mask = data[offset:offset + 4]
        offset += 4
    if len(data) < offset + length:
        raise IndexError("incomplete payload")
    payload = data[offset:offset + length]
    if masked:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return opcode, payload, offset + length


class _WebSocket:
    """A bare websocket connection to one CDP target. Synchronous, one
    request/response at a time — which is all the CDP calls here need."""

    def __init__(self, ws_url: str, *, timeout: float = 15.0):
        parsed = urlparse(ws_url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebTierError(f"not a websocket url: {ws_url!r}")
        if parsed.scheme == "wss":
            raise WebTierError("wss not supported by the stdlib tier (local CDP is ws)")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self._timeout = timeout
        self._buf = b""
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._handshake(host, port, path)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(req.encode())
        # Read until the end of the HTTP response headers. Anything after the
        # blank line is the first websocket frame — keep it.
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(_RECV_CHUNK)
            if not chunk:
                raise WebTierError("connection closed during websocket handshake")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise WebTierError(f"websocket upgrade rejected: {status!r}")
        self._buf = rest

    def send_text(self, text: str) -> None:
        self._sock.sendall(encode_frame(text.encode()))

    def recv_text(self) -> str:
        """Read frames until a full text message arrives, transparently
        answering pings and skipping control frames.

        `socket.recv()` does NOT return b'' on a read timeout — it raises
        `socket.timeout` (an `OSError` subclass). We catch it and surface a
        `WebTierError` so a stalled/crashed target never propagates a raw
        timeout to callers (and never blocks forever waiting on a closed peer)."""
        while True:
            try:
                opcode, payload, consumed = decode_frame(self._buf)
            except IndexError:
                try:
                    chunk = self._sock.recv(_RECV_CHUNK)
                except socket.timeout as e:
                    raise WebTierError(
                        f"timed out reading websocket frame "
                        f"after {self._timeout}s") from e
                if not chunk:
                    raise WebTierError("connection closed while reading frame")
                self._buf += chunk
                continue
            self._buf = self._buf[consumed:]
            if opcode == _OP_TEXT:
                return payload.decode("utf-8", "replace")
            if opcode == _OP_PING:
                self._sock.sendall(encode_frame(payload, opcode=_OP_PONG))
                continue
            if opcode == _OP_CLOSE:
                raise WebTierError("server closed the websocket")
            # Pong / continuation / binary — not used by CDP; ignore.

    def close(self) -> None:
        try:
            self._sock.sendall(encode_frame(b"", opcode=_OP_CLOSE))
        except OSError:
            pass
        finally:
            self._sock.close()

    def __enter__(self) -> "_WebSocket":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# CDP client                                                                   #
# --------------------------------------------------------------------------- #

class _CDPSession:
    """One CDP JSON-RPC session over a websocket. Sends `{id, method, params}`
    and matches the reply by id, skipping the unsolicited `{method: ...}`
    protocol events Chromium streams (e.g. Network/Page lifecycle)."""

    def __init__(self, ws: _WebSocket):
        self._ws = ws
        self._next_id = 0

    def call(self, method: str, params: dict | None = None,
             *, deadline: float = 10.0) -> dict:
        """Send one CDP request and return its matching reply's `result`.

        Chromium streams unsolicited protocol events (`{method: ...}` with no
        `id`, or a different `id`) on the same socket, so we loop, skipping any
        frame whose `id` is not ours. To avoid spinning forever when the
        matching response NEVER arrives (e.g. the target crashed mid-call),
        we enforce a wall-clock `deadline` (default 10s) and raise
        `WebTierError` once it elapses instead of looping indefinitely."""
        self._next_id += 1
        msg_id = self._next_id
        self._ws.send_text(json.dumps({
            "id": msg_id, "method": method, "params": params or {},
        }))
        # Skip events until our matching response id comes back, but never past
        # the wall-clock deadline.
        end = time.monotonic() + deadline
        while True:
            if time.monotonic() >= end:
                raise WebTierError(
                    f"{method}: no response id={msg_id} within {deadline}s")
            reply = json.loads(self._ws.recv_text())
            if reply.get("id") != msg_id:
                continue
            if "error" in reply:
                raise WebTierError(f"{method}: {reply['error']}")
            return reply.get("result", {})


def _runtime_evaluate(ws_url: str, expression: str, *,
                      return_by_value: bool = True) -> dict:
    with _WebSocket(ws_url) as ws:
        cdp = _CDPSession(ws)
        cdp.call("Runtime.enable")
        return cdp.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": True,
        })


def cdp_eval(ws_url: str, expression: str) -> Any:
    """Evaluate `expression` in the page and return its value by value.

    Raises `WebTierError` on a JS exception so a caller never silently reads a
    bad result. Works against a background tab — no fronting required."""
    result = _runtime_evaluate(ws_url, expression)
    details = result.get("exceptionDetails")
    if details:
        text = details.get("exception", {}).get("description") or details.get("text")
        raise WebTierError(f"JS exception: {text}")
    return result.get("result", {}).get("value")


def cdp_click(ws_url: str, selector: str) -> bool:
    """Click the first element matching `selector` with a REAL DOM click —
    `HTMLElement.click()` in page context, which fires the synthetic-event
    handlers React (and friends) actually listen to, unlike an AX press.

    Returns True if an element was found and clicked, False if the selector
    matched nothing. `selector` is embedded via JSON so quotes and brackets in
    it can never break out of the expression.

    IN-FLIGHT NAVIGATION CAVEAT: `el.click()` keeps synchronous CDP semantics —
    it returns as soon as the click handler has been dispatched and `true`
    serialized back. But if the click triggers a navigation, that navigation is
    still in flight when this function returns and when the websocket is torn
    down in `_WebSocket.__exit__`; we do NOT wait for the destination to load.
    A caller that needs the post-navigation state MUST poll the destination
    itself (the live test sleeps, then reads the fixture's `/events`)."""
    sel = json.dumps(selector)
    expr = (
        "(() => {"
        f"  const el = document.querySelector({sel});"
        "   if (!el) return false;"
        "   el.click();"
        "   return true;"
        "})()"
    )
    return bool(cdp_eval(ws_url, expr))


def cdp_set_value(ws_url: str, selector: str, value: str) -> bool:
    """Type-without-focus: set an input/select `.value` and dispatch the
    `input` + `change` events frameworks bind to. Handles the hidden-<select>
    case the AX tier cannot reach (options aren't in the AX tree until the
    native popup opens; in the DOM they're always settable). Returns whether
    the element was found."""
    sel = json.dumps(selector)
    val = json.dumps(value)
    expr = (
        "(() => {"
        f"  const el = document.querySelector({sel});"
        "   if (!el) return false;"
        f"  el.value = {val};"
        "   el.dispatchEvent(new Event('input', {bubbles: true}));"
        "   el.dispatchEvent(new Event('change', {bubbles: true}));"
        "   return true;"
        "})()"
    )
    return bool(cdp_eval(ws_url, expr))


def cdp_navigate(ws_url: str, url: str) -> dict:
    """Navigate the target's tab to `url` via `Page.navigate`. Returns the CDP
    result (frameId / loaderId).

    IN-FLIGHT NAVIGATION CAVEAT: `Page.navigate` returns once the navigation has
    been *committed*, NOT once the destination has finished loading. The page
    load is still in flight when this returns and when the websocket closes in
    `_WebSocket.__exit__` (we do not subscribe to `Page.loadEventFired`). The
    same holds for any `cdp_click` whose click triggers a navigation. Callers
    that need the loaded destination MUST poll it themselves — the live test
    sleeps before reading the fixture's `/events`."""
    with _WebSocket(ws_url) as ws:
        cdp = _CDPSession(ws)
        cdp.call("Page.enable")
        return cdp.call("Page.navigate", {"url": url})
