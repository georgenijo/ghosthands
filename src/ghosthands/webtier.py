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
               bundle_id: str = DEFAULT_BUNDLE_ID, *,
               user_data_dir: str | None = None,
               new_instance: bool = False) -> Any:
    """Launch Brave with the remote-debugging port open and the page loaded.

    `electron_debugging_port` is the knob cua-driver forwards to Chromium as
    `--remote-debugging-port`, which is what enables the whole CDP surface used
    below. Returns the raw launch result (pid + windows).

    `user_data_dir` ISOLATES the run in its own Chromium profile (a distinct
    `--user-data-dir`), so automation never touches the user's working browser;
    it implies a new instance. `new_instance` alone forces a fresh instance
    (open -n) without changing the profile."""
    args: dict = {
        "bundle_id": bundle_id,
        "electron_debugging_port": port,
        "urls": [url],
    }
    extra: list[str] = []
    if user_data_dir:
        extra += [f"--user-data-dir={user_data_dir}",
                  "--no-first-run", "--no-default-browser-check"]
        new_instance = True
    if extra:
        args["additional_arguments"] = extra
    if new_instance:
        args["creates_new_application_instance"] = True
    return driver.call("launch_app", args)


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


# --------------------------------------------------------------------------- #
# Shadow-DOM piercing + accessible-name resolution (issue #8)                  #
# --------------------------------------------------------------------------- #

# A self-contained in-page resolver, re-injected into every expression so it
# survives navigations (a fresh document wipes any persisted global) and never
# depends on prior state. Returns {deepAll, deepQuery, accName, findByName}.
#
# deepQuery pierces OPEN shadow roots — `document.querySelector` stops at the
# shadow boundary, so App Store Connect (every control inside an open shadow
# root) reads as empty to a plain selector. We recurse into `el.shadowRoot` at
# every node. accName resolves an element's accessible name the way a screen
# reader would, for obfuscated apps with no stable selectors: aria-label,
# aria-labelledby (dereferenced within the same root), an associated <label>
# (for= or wrapping), placeholder, title, then visible text.
#
# KNOWN LIMIT (deferred, #8 gap 4): CLOSED shadow roots and cross-origin
# iframes are NOT reachable — `el.shadowRoot` is null for a closed root and
# `Runtime.evaluate` can't cross a frame boundary. The real fix is the CDP
# DOM/Accessibility domains driven per-frame; not present on App Store Connect
# (open roots), so it is documented rather than built here.
_RESOLVER_OBJ = r"""(() => {
  function deepAll(root) {
    const out = [];
    (function collect(r) {
      let kids;
      try { kids = r.querySelectorAll('*'); } catch (x) { return; }
      for (const e of kids) { out.push(e); if (e.shadowRoot) collect(e.shadowRoot); }
    })(root || document);
    return out;
  }
  function deepQuery(sel, root) {
    for (const e of deepAll(root)) {
      try { if (e.matches && e.matches(sel)) return e; } catch (x) {}
    }
    return null;
  }
  function _text(n) {
    return (n && n.textContent ? n.textContent : '').replace(/\s+/g, ' ').trim();
  }
  function accName(e) {
    const root = e.getRootNode();
    const byId = (id) => {
      try { return (root.getElementById ? root.getElementById(id) : null)
                   || document.getElementById(id); } catch (x) { return null; }
    };
    const al = e.getAttribute && e.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    const lb = e.getAttribute && e.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map((id) => _text(byId(id))).filter(Boolean).join(' ');
      if (t) return t;
    }
    if (e.id && root.querySelector) {
      const esc = (window.CSS && CSS.escape) ? CSS.escape(e.id) : e.id;
      const lab = root.querySelector('label[for="' + esc + '"]');
      if (lab) return _text(lab);
    }
    const wrap = e.closest && e.closest('label');
    if (wrap) return _text(wrap);
    const ph = e.getAttribute && e.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    const ti = e.getAttribute && e.getAttribute('title');
    if (ti && ti.trim()) return ti.trim();
    return _text(e);
  }
  function findByName(name) {
    const want = String(name).replace(/\s+/g, ' ').trim().toLowerCase();
    const roles = ['button', 'a', 'input', 'select', 'textarea'];
    const cands = deepAll(document).filter((e) => {
      const t = e.tagName ? e.tagName.toLowerCase() : '';
      return roles.includes(t) || (e.getAttribute && e.getAttribute('role'));
    });
    const named = cands.map((e) => [e, accName(e).toLowerCase()]);
    let hit = named.find((p) => p[1] === want);
    if (!hit) hit = named.find((p) => p[1] && p[1].startsWith(want));
    if (!hit) hit = named.find((p) => p[1] && p[1].includes(want));
    return hit ? hit[0] : null;
  }
  return { deepAll, deepQuery, accName, findByName };
})()"""


def _expr(body: str) -> str:
    """Wrap a JS body that uses `H` (the resolver) into a self-contained IIFE."""
    return "(() => { const H = " + _RESOLVER_OBJ + "; " + body + " })()"


def _verify(ws_url: str, predicate: str) -> bool:
    """Evaluate a JS predicate after an action and return whether it held — the
    post-action world check that turns "the element existed and .click() fired"
    into "the world actually changed". A predicate that throws (e.g. the page
    navigated out from under it) reads as False."""
    time.sleep(0.2)
    expr = "(() => { try { return !!(" + predicate + "); } catch (x) { return false; } })()"
    try:
        return bool(cdp_eval(ws_url, expr))
    except WebTierError:
        return False


def cdp_click(ws_url: str, selector: str, *, deep: bool = True,
              verify: str | None = None) -> bool:
    """Click the first element matching `selector` with a REAL DOM click —
    `HTMLElement.click()` in page context, which fires the synthetic-event
    handlers React (and friends) actually listen to, unlike an AX press.

    `deep=True` (default) PIERCES open shadow roots via the deep resolver; pass
    `deep=False` to restrict to the light DOM (`document.querySelector`). Plain
    light-DOM selectors resolve under both.

    `verify` is an optional JS predicate evaluated AFTER the click: the click
    returning True only means an element was found and `.click()` fired — which
    is True even when the click no-ops (a disabled control, a handler that
    bailed). With `verify`, the function returns True only if the world actually
    changed, so a no-op click is reported as False instead of a false success.

    Returns True if an element was found and clicked (and `verify`, if given,
    held), else False. `selector` is embedded via JSON so quotes/brackets can't
    break out of the expression.

    IN-FLIGHT NAVIGATION CAVEAT: if the click triggers a navigation it is still
    in flight when this returns and when the websocket is torn down; we do NOT
    wait for the destination. A caller needing post-navigation state MUST poll
    it (the live test sleeps, then reads the fixture's `/events`). Pair a
    navigating click with an external world check, not `verify`."""
    sel = json.dumps(selector)
    finder = ("H.deepQuery(" + sel + ")") if deep else ("document.querySelector(" + sel + ")")
    body = "const el = " + finder + "; if (!el) return false; el.click(); return true;"
    if not bool(cdp_eval(ws_url, _expr(body))):
        return False
    return _verify(ws_url, verify) if verify is not None else True


def cdp_set_value(ws_url: str, selector: str, value: str, *, deep: bool = True,
                  verify: str | None = None) -> bool:
    """Type-without-focus: set an input/select `.value` and dispatch the
    `input` + `change` events frameworks bind to. Handles the hidden-<select>
    case the AX tier cannot reach (options aren't in the AX tree until the
    native popup opens; in the DOM they're always settable).

    `deep=True` (default) pierces open shadow roots; `verify` is the same
    post-action world check as `cdp_click`. Returns whether the element was
    found (and `verify`, if given, held)."""
    sel = json.dumps(selector)
    val = json.dumps(value)
    finder = ("H.deepQuery(" + sel + ")") if deep else ("document.querySelector(" + sel + ")")
    body = ("const el = " + finder + "; if (!el) return false; el.value = " + val + ";"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true})); return true;")
    if not bool(cdp_eval(ws_url, _expr(body))):
        return False
    return _verify(ws_url, verify) if verify is not None else True


def find_by_name(ws_url: str, name: str) -> dict | None:
    """Resolve an element by its ACCESSIBLE NAME (aria-label, aria-labelledby,
    associated <label>, placeholder, title, or visible text), piercing open
    shadow roots — for obfuscated apps with no stable selectors. Returns a
    descriptor {tag, type, role, name} of the best match, or None. Match order:
    exact name, then prefix, then substring (case-insensitive)."""
    nm = json.dumps(name)
    body = ("const el = H.findByName(" + nm + "); if (!el) return null;"
            " return {tag: el.tagName.toLowerCase(),"
            " type: el.getAttribute('type'), role: el.getAttribute('role'),"
            " name: H.accName(el)};")
    return cdp_eval(ws_url, _expr(body))


def click_by_name(ws_url: str, name: str, *, verify: str | None = None) -> bool:
    """Click the element resolved by accessible name (see `find_by_name`).
    Pierces open shadow roots; `verify` is the post-action world check."""
    nm = json.dumps(name)
    body = "const el = H.findByName(" + nm + "); if (!el) return false; el.click(); return true;"
    if not bool(cdp_eval(ws_url, _expr(body))):
        return False
    return _verify(ws_url, verify) if verify is not None else True


def set_value_by_name(ws_url: str, name: str, value: str, *,
                      verify: str | None = None) -> bool:
    """Set `.value` on the element resolved by accessible name (see
    `find_by_name`) and dispatch input/change. Pierces open shadow roots;
    `verify` is the post-action world check. Returns whether it was found."""
    nm = json.dumps(name)
    val = json.dumps(value)
    body = ("const el = H.findByName(" + nm + "); if (!el) return false; el.value = " + val + ";"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true})); return true;")
    if not bool(cdp_eval(ws_url, _expr(body))):
        return False
    return _verify(ws_url, verify) if verify is not None else True


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


# --------------------------------------------------------------------------- #
# Surface routing (issue #9)                                                   #
# --------------------------------------------------------------------------- #

# Chromium/WebKit bundles whose windows are web surfaces — drive these through
# the DOM tier (CDP), where a backgrounded tab is fully addressable, real
# clicks fire React, hidden <select> options are settable, and typing needs no
# focus. The AX tier stays the right hammer for native AppKit apps.
_BROWSER_BUNDLE_HINTS = ("brave", "chrom", "safari", "edgemac", "firefox",
                         "webkit", "vivaldi", "arc", "opera")


def route_surface(*, bundle_id: str | None = None,
                  markdown: str | None = None) -> str:
    """Pick the tier for a target: ``"web"`` (DOM/CDP) for a browser/web
    surface, ``"native"`` (AX) otherwise.

    Two independent signals, OR'd: a browser bundle id, or an ``AXWebArea``
    anywhere in an AX snapshot (an embedded web view inside an otherwise-native
    app still routes to DOM for its web content). Either alone is enough; with
    neither, the target is native."""
    if bundle_id:
        b = bundle_id.lower()
        if any(h in b for h in _BROWSER_BUNDLE_HINTS):
            return "web"
    if markdown and "AXWebArea" in markdown:
        return "web"
    return "native"
