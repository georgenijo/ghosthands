"""Tests for the CDP/websocket web tier.

Layer 1 (offline, runs everywhere): exercises the RFC 6455 frame encoder and
decoder with no hardware — masks a known payload with a fixed mask, asserts the
on-wire bytes are exactly what the spec dictates, then decodes them back and
asserts the round-trip. Covers the 16-bit extended-length path (>=126 bytes)
and the 64-bit path (>=65536 bytes, length byte 127), both of which a real CDP
result hits. It also drives the websocket reader and the `_CDPSession` matcher
against an in-memory fake socket — crafted frames prove the session matches its
own response id while SKIPPING an unsolicited CDP event frame, that the reader
answers a PING with a PONG, and that it raises on a CLOSE frame. No daemon, no
Brave, no network.

Layer 2 (live, GUARDED by GH_LIVE): the full end-to-end against Brave + the
local fixture site. NOT run here — needs the desktop, Brave, and cua-driver.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import webtier  # noqa: E402


def _server_frame(payload: bytes, *, opcode: int = webtier._OP_TEXT) -> bytes:
    """Build a server->client frame the way Chromium does: FIN set, NO mask.

    We deliberately do NOT reuse encode_frame here — encode_frame masks (it is
    the client side). Server frames are unmasked, so the reader must decode them
    without a mask. This builds exactly that shape across all three length forms
    so the fake socket can feed the reader bytes identical to the real wire."""
    fin_op = 0x80 | (opcode & 0x0F)
    length = len(payload)
    header = bytearray([fin_op])
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += webtier.struct.pack("!H", length)
    else:
        header.append(127)
        header += webtier.struct.pack("!Q", length)
    return bytes(header) + payload


class _FakeSocket:
    """An in-memory stand-in for a connected websocket socket.

    `recv` hands back successive pre-loaded chunks (each chunk modelling one
    network read), then raises socket.timeout if asked again — modelling a peer
    that has gone quiet. Everything written with `sendall` is captured so a test
    can assert the reader actually emitted a PONG / CLOSE."""

    def __init__(self, recv_chunks: list[bytes],
                 *, repeat_last: bool = False):
        self._chunks = list(recv_chunks)
        self._repeat_last = repeat_last
        self.sent: list[bytes] = []

    def recv(self, _n: int) -> bytes:
        if self._chunks:
            chunk = self._chunks.pop(0)
            # When repeating, keep the last chunk available forever so the
            # reader always gets a valid frame and only a DEADLINE (not an
            # exhausted socket) can stop a matcher loop.
            if self._repeat_last and not self._chunks:
                self._chunks.append(chunk)
            return chunk
        raise webtier.socket.timeout("fake socket exhausted")

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, _t: float) -> None:  # pragma: no cover - noop
        pass

    def close(self) -> None:  # pragma: no cover - noop
        pass


def _ws_with(recv_chunks: list[bytes],
             *, repeat_last: bool = False) -> "webtier._WebSocket":
    """A `_WebSocket` wired to a `_FakeSocket`, bypassing the real handshake.

    `_WebSocket.__init__` would open a TCP connection; we skip it via
    __new__ and inject the fields the reader actually touches. `repeat_last`
    makes the socket replay its final chunk forever (an endless event stream)."""
    ws = webtier._WebSocket.__new__(webtier._WebSocket)
    ws._sock = _FakeSocket(recv_chunks, repeat_last=repeat_last)  # type: ignore[attr-defined]
    ws._buf = b""                         # type: ignore[attr-defined]
    ws._timeout = 15.0                    # type: ignore[attr-defined]
    return ws


def test_frame_roundtrip() -> None:
    """A masked client frame decodes back to the original payload, and the
    masking is genuinely applied (masked bytes differ from plaintext)."""
    payload = b'{"id":1,"method":"Runtime.evaluate"}'
    mask = b"\x01\x02\x03\x04"
    frame = webtier.encode_frame(payload, mask=mask)

    # Header: FIN+text opcode, MASK bit + 7-bit length, then the 4 mask bytes.
    assert frame[0] == 0x81, hex(frame[0])
    assert frame[1] == (0x80 | len(payload)), hex(frame[1])
    assert frame[2:6] == mask, frame[2:6]

    # The body must actually be masked (XORed), not sent in the clear.
    body = frame[6:]
    assert body != payload, "payload was sent unmasked"
    expected = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    assert body == expected, "masking does not match XOR spec"

    opcode, decoded, consumed = webtier.decode_frame(frame)
    assert opcode == 0x1, opcode
    assert decoded == payload, decoded
    assert consumed == len(frame), (consumed, len(frame))


def test_frame_extended_length() -> None:
    """Payloads of 126..65535 bytes use the 16-bit extended length form
    (length byte 126, then a big-endian uint16). CDP results routinely exceed
    126 bytes, so this path must be correct."""
    payload = b"x" * 300
    mask = b"\xaa\xbb\xcc\xdd"
    frame = webtier.encode_frame(payload, mask=mask)

    assert frame[0] == 0x81
    assert frame[1] == (0x80 | 126), hex(frame[1])  # extended-length flag
    declared = (frame[2] << 8) | frame[3]
    assert declared == 300, declared
    assert frame[4:8] == mask

    opcode, decoded, consumed = webtier.decode_frame(frame)
    assert opcode == 0x1
    assert decoded == payload
    assert consumed == len(frame)


def test_frame_partial_needs_more_bytes() -> None:
    """A truncated frame raises IndexError so the websocket reader knows to
    pull more bytes rather than mis-parse."""
    payload = b"hello world"
    frame = webtier.encode_frame(payload, mask=b"\x00\x00\x00\x00")
    truncated = frame[:-3]
    raised = False
    try:
        webtier.decode_frame(truncated)
    except IndexError:
        raised = True
    assert raised, "truncated frame should raise IndexError"


def test_server_frame_unmasked_decodes() -> None:
    """Server->client frames are unmasked (no MASK bit). The decoder must read
    them without expecting a mask, since that's the real wire shape of every
    CDP reply Chromium sends back."""
    payload = b'{"id":1,"result":{}}'
    header = bytes([0x81, len(payload)])  # FIN+text, no MASK bit
    frame = header + payload
    opcode, decoded, consumed = webtier.decode_frame(frame)
    assert opcode == 0x1
    assert decoded == payload
    assert consumed == len(frame)


def test_frame_extended_length_64bit() -> None:
    """Payloads >= 65536 bytes use the 64-bit extended length form (length byte
    127, then a big-endian uint64). A serialized DOM result can easily exceed
    64 KiB, so decode_frame must read the 8-byte length and the full payload."""
    payload = b"y" * 70000  # > 65535 forces the 127 path
    mask = b"\x10\x20\x30\x40"
    frame = webtier.encode_frame(payload, mask=mask)

    assert frame[0] == 0x81
    assert frame[1] == (0x80 | 127), hex(frame[1])  # 64-bit length flag
    (declared,) = webtier.struct.unpack("!Q", frame[2:10])
    assert declared == 70000, declared
    assert frame[10:14] == mask  # mask follows the 8-byte length

    opcode, decoded, consumed = webtier.decode_frame(frame)
    assert opcode == 0x1
    assert decoded == payload
    assert consumed == len(frame)


def test_cdp_call_matches_id_and_skips_event() -> None:
    """`_CDPSession.call` must return the reply whose `id` matches its request
    and SKIP an unsolicited CDP event frame (a `{method: ...}` with no/other
    id) that Chromium interleaves. We feed, in order: one event frame, then the
    real response — split across two `recv` chunks (and the response itself
    split mid-frame) to also prove the reader reassembles partial frames."""
    event = _server_frame(
        json.dumps({"method": "Network.requestWillBeSent",
                    "params": {"requestId": "x"}}).encode())
    # The session's first request gets id=1 (next_id starts at 0, ++ -> 1).
    response = _server_frame(
        json.dumps({"id": 1, "result": {"value": 42}}).encode())

    split = len(response) // 2
    ws = _ws_with([event, response[:split], response[split:]])
    cdp = webtier._CDPSession(ws)
    result = cdp.call("Runtime.evaluate", {"expression": "40 + 2"})
    assert result == {"value": 42}, result

    # The request was actually sent (one masked client text frame).
    sent = ws._sock.sent  # type: ignore[attr-defined]
    assert len(sent) == 1, sent
    op, payload, _ = webtier.decode_frame(sent[0])
    assert op == webtier._OP_TEXT
    req = json.loads(payload)
    assert req["id"] == 1 and req["method"] == "Runtime.evaluate", req


def test_cdp_call_error_reply_raises() -> None:
    """A CDP reply carrying an `error` (matched by id) becomes a WebTierError,
    so a caller never reads a result that isn't there."""
    response = _server_frame(
        json.dumps({"id": 1, "error": {"code": -32000,
                                       "message": "boom"}}).encode())
    ws = _ws_with([response])
    cdp = webtier._CDPSession(ws)
    raised = False
    try:
        cdp.call("Runtime.evaluate")
    except webtier.WebTierError as e:
        raised = True
        assert "boom" in str(e), e
    assert raised, "an error reply must raise WebTierError"


def test_cdp_call_deadline_raises_not_loops() -> None:
    """If the matching response id NEVER arrives (target crashed mid-call) but
    foreign-id event frames keep streaming, the matcher must NOT loop forever:
    the wall-clock deadline must break the loop. The socket here replays a
    foreign-id frame ENDLESSLY (recv always succeeds), so ONLY the deadline can
    stop call(); a tiny deadline must therefore raise WebTierError promptly."""
    foreign = _server_frame(json.dumps({"id": 999, "result": {}}).encode())
    ws = _ws_with([foreign], repeat_last=True)  # endless event stream
    cdp = webtier._CDPSession(ws)
    start = webtier.time.monotonic()
    raised = False
    try:
        cdp.call("Runtime.evaluate", deadline=0.05)
    except webtier.WebTierError as e:
        raised = True
        assert "no response" in str(e), e
    elapsed = webtier.time.monotonic() - start
    assert raised, "a never-arriving response must raise, not hang"
    assert elapsed < 5.0, f"deadline did not bound the loop ({elapsed}s)"


def test_recv_text_answers_ping_then_reads_text() -> None:
    """The reader must transparently answer a server PING with a PONG (echoing
    the ping payload) and then go on to return the following TEXT message —
    never surfacing the control frame to the caller."""
    ping = _server_frame(b"hb", opcode=webtier._OP_PING)
    text = _server_frame(b'{"id":1,"result":{}}')
    ws = _ws_with([ping + text])  # both frames arrive in one read

    msg = ws.recv_text()
    assert msg == '{"id":1,"result":{}}', msg

    # Exactly one frame was written back: a PONG carrying the ping payload.
    sent = ws._sock.sent  # type: ignore[attr-defined]
    assert len(sent) == 1, sent
    op, payload, _ = webtier.decode_frame(sent[0])
    assert op == webtier._OP_PONG, op
    assert payload == b"hb", payload


def test_recv_text_close_frame_raises() -> None:
    """A server CLOSE frame (opcode 0x8) must raise WebTierError, not be
    silently treated as data."""
    close = _server_frame(b"", opcode=webtier._OP_CLOSE)
    ws = _ws_with([close])
    raised = False
    try:
        ws.recv_text()
    except webtier.WebTierError as e:
        raised = True
        assert "closed" in str(e).lower(), e
    assert raised, "a CLOSE frame must raise WebTierError"


def test_recv_text_timeout_raises() -> None:
    """socket.recv() raises socket.timeout (it does NOT return b'') on a read
    timeout; recv_text must catch it and raise WebTierError rather than let a
    raw timeout escape or block forever."""
    ws = _ws_with([])  # _FakeSocket.recv immediately raises socket.timeout
    raised = False
    try:
        ws.recv_text()
    except webtier.WebTierError as e:
        raised = True
        assert "tim" in str(e).lower(), e
    assert raised, "a recv timeout must surface as WebTierError"


class _FakeResp:
    """Minimal context-manager response with a `.read()`, like urlopen's."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


def test_list_targets_malformed_json_raises_webtiererror() -> None:
    """A 200 response with a non-JSON body makes json.loads raise
    json.JSONDecodeError (a ValueError). list_targets must catch it and raise
    WebTierError, not leak the raw decode error to the caller."""
    real_urlopen = webtier.urllib.request.urlopen
    webtier.urllib.request.urlopen = (  # type: ignore[assignment]
        lambda *a, **k: _FakeResp(b"<html>not json</html>"))
    try:
        raised = False
        try:
            webtier.list_targets(port=1)
        except webtier.WebTierError as e:
            raised = True
            assert "failed" in str(e), e
        assert raised, "malformed body must raise WebTierError"
    finally:
        webtier.urllib.request.urlopen = real_urlopen  # type: ignore[assignment]


def test_list_targets_filters_to_pages() -> None:
    """A well-formed body is parsed and filtered to page targets only (service
    workers and other non-page types are dropped)."""
    body = json.dumps([
        {"type": "page", "id": "a", "url": "http://x"},
        {"type": "service_worker", "id": "b", "url": "http://y"},
        {"id": "c", "url": "http://z"},  # missing type defaults to page
    ]).encode()
    real_urlopen = webtier.urllib.request.urlopen
    webtier.urllib.request.urlopen = (  # type: ignore[assignment]
        lambda *a, **k: _FakeResp(body))
    try:
        targets = webtier.list_targets(port=1)
    finally:
        webtier.urllib.request.urlopen = real_urlopen  # type: ignore[assignment]
    ids = sorted(t["id"] for t in targets)
    assert ids == ["a", "c"], ids


def _run_offline() -> None:
    test_frame_roundtrip()
    test_frame_extended_length()
    test_frame_extended_length_64bit()
    test_frame_partial_needs_more_bytes()
    test_server_frame_unmasked_decodes()
    test_cdp_call_matches_id_and_skips_event()
    test_cdp_call_error_reply_raises()
    test_cdp_call_deadline_raises_not_loops()
    test_recv_text_answers_ping_then_reads_text()
    test_recv_text_close_frame_raises()
    test_recv_text_timeout_raises()
    test_list_targets_malformed_json_raises_webtiererror()
    test_list_targets_filters_to_pages()
    print("OFFLINE OK: frame_roundtrip, frame_extended_length, "
          "frame_extended_length_64bit, frame_partial_needs_more_bytes, "
          "server_frame_unmasked_decodes, cdp_call_matches_id_and_skips_event, "
          "cdp_call_error_reply_raises, cdp_call_deadline_raises_not_loops, "
          "recv_text_answers_ping_then_reads_text, recv_text_close_frame_raises, "
          "recv_text_timeout_raises, list_targets_malformed_json_raises_webtiererror, "
          "list_targets_filters_to_pages")


def _run_live() -> None:
    """End-to-end against Brave + the fixture. Requires the fixture server on
    :8773 already running and reset. Proves: per-tab addressing without
    fronting, a real DOM click that the server records as a navigation."""
    port = int(os.environ.get("GH_LIVE_PORT", "9333"))
    page = "http://127.0.0.1:8773/wizard1.html"

    webtier.launch_web(page, port=port)
    # Give Brave a moment to register the debug port + load the page.
    import time
    for _ in range(40):
        try:
            tgt = webtier.target_for_url(port, "wizard1.html")
            break
        except webtier.WebTierError:
            time.sleep(0.5)
    else:
        raise SystemExit("LIVE FAIL: wizard1 tab never appeared on /json/list")

    ws_url = tgt["webSocketDebuggerUrl"]
    # Wait for the document to finish loading the link text.
    for _ in range(40):
        label = webtier.cdp_eval(
            ws_url, "document.querySelector('a') && "
                    "document.querySelector('a').textContent")
        if label:
            break
        time.sleep(0.5)
    print(f"LIVE: read background-tab link label = {label!r}")
    assert label and "Starter" in label, label

    # A REAL DOM click on the Pro-plan link -> navigates -> server logs the GET.
    clicked = webtier.cdp_click(
        ws_url, "a[href='/wizard2.html?plan=pro']")
    assert clicked, "cdp_click found no matching link"

    import urllib.request
    time.sleep(1.0)
    with urllib.request.urlopen("http://127.0.0.1:8773/events") as r:
        events = json.loads(r.read())
    hit = any(e["path"] == "/wizard2.html" and e["query"].get("plan") == "pro"
              for e in events)
    assert hit, f"server never recorded the pro-plan click; events={events}"
    print("LIVE OK: per-tab eval + real DOM click recorded by the fixture")


def _run_live_shadow() -> None:
    """End-to-end for issue #8 against an ISOLATED Brave on the shadow fixture
    (own --user-data-dir, so the user's working Brave is untouched). Proves the
    four gaps the field report hit, each world-checked by the fixture's
    /events log:

      1. deepQuery PIERCES an open shadow root (a plain selector found nothing).
      2. find-by-accessible-name resolves + sets a shadow field with no stable
         value-selector.
      3. verify catches a click that no-ops (a disabled control).
      4. the deep path still clicks plain light DOM.
    """
    import subprocess
    import time
    import urllib.request

    profile = "/tmp/gh-isolated-brave-test"
    port = int(os.environ.get("GH_LIVE_SHADOW_PORT", "9446"))
    base = "http://127.0.0.1:8773"
    page = f"{base}/shadow.html"
    subprocess.run(["pkill", "-f", f"user-data-dir={profile}"], capture_output=True)
    time.sleep(1.0)

    res = webtier.launch_web(page, port=port, user_data_dir=profile)
    pid = res.get("pid") if isinstance(res, dict) else None
    try:
        target = None
        for _ in range(60):
            try:
                target = webtier.target_for_url(port, "shadow.html")
                break
            except webtier.WebTierError:
                time.sleep(0.5)
        assert target, "LIVE shadow: tab never appeared on the debug port"
        ws = target["webSocketDebuggerUrl"]
        for _ in range(60):
            if webtier.cdp_eval(ws, "(() => { const p=document.querySelector('gh-panel');"
                                    " return !!(p&&p.shadowRoot&&p.shadowRoot.getElementById('save')); })()"):
                break
            time.sleep(0.5)

        def reset_to_page() -> None:
            urllib.request.urlopen(f"{base}/reset", timeout=5).read()
            webtier.cdp_navigate(ws, page)
            for _ in range(40):
                if webtier.cdp_eval(ws, "!!(document.querySelector('gh-panel') && "
                                        "document.querySelector('gh-panel').shadowRoot)"):
                    return
                time.sleep(0.25)

        def logged(path: str, query: dict | None = None) -> bool:
            evs = json.loads(urllib.request.urlopen(f"{base}/events", timeout=5).read())
            return any(e["path"] == path and (query is None or e["query"] == query)
                       for e in evs)

        # 1. plain selector can't pierce; deepQuery can -> server logs the click
        reset_to_page()
        assert not bool(webtier.cdp_eval(
            ws, "!!document.querySelector('#save')")), \
            "control should be hidden from a plain (non-piercing) selector"
        assert webtier.cdp_click(ws, "#save"), "deep cdp_click did not find #save"
        time.sleep(1.0)
        assert logged("/set/shadowclick"), "shadow click not recorded by the world"

        # 2. find-by-accessible-name sets a shadow field -> server logs the value
        reset_to_page()
        assert webtier.set_value_by_name(ws, "Project name", "Acme"), \
            "set_value_by_name did not resolve the aria-labelled shadow field"
        time.sleep(1.0)
        assert logged("/set/name", {"value": "Acme"}), \
            "the shadow field value never reached the world log"

        # 3. verify catches a no-op (disabled button): click 'fires' but the
        #    world does not change -> verify returns False, /set/confirm absent
        reset_to_page()
        ok = webtier.cdp_click(
            ws, "#confirm",
            verify="!document.querySelector('gh-panel').shadowRoot"
                   ".getElementById('confirmed').hidden")
        assert ok is False, "verify failed to catch the no-op click"
        time.sleep(0.5)
        assert not logged("/set/confirm"), "a no-op click reached the world"

        # 4. the deep path still clicks plain light DOM
        reset_to_page()
        assert webtier.cdp_click(ws, "#light-link"), "deep path missed light DOM"
        time.sleep(1.0)
        assert logged("/set/lightclick"), "light-DOM click not recorded"

        print("LIVE OK (#8): shadow pierce + find-by-name + verify no-op + light DOM")
    finally:
        if pid:
            try:
                webtier.driver.call("kill_app", {"pid": pid})
            except Exception:  # noqa: BLE001
                subprocess.run(["pkill", "-f", f"user-data-dir={profile}"],
                               capture_output=True)


if __name__ == "__main__":
    try:
        _run_offline()
        if os.environ.get("GH_LIVE"):
            _run_live()
            _run_live_shadow()
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
    sys.exit(0)
