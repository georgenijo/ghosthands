#!/usr/bin/env python3
"""Fixture site for the v2 benchmark — a tiny local web server we control.

Serves the static pages in bench/fixture/site/ and logs every request to an
events file. Done-detectors read the EVENT LOG (the world), never the agent's
words: a task passes only when the server actually received the terminal
request (e.g. /submit?plan=pro&addon=backups).

Why local fixtures: real sites redesign, A/B-test, and geo-vary, so their
scores drift for reasons that have nothing to do with the model. These pages
never change unless we change them — a score change IS a model change.

Endpoints:
  /reset            clear the event log (bench setup calls this)
  /events           JSON list of logged requests
  /submit, /learn/*, /set/*   terminal actions — logged like everything else
  anything else     static file from site/ (query string ignored for lookup)

Usage: python3 bench/fixture/server.py [--port 8773]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SITE = Path(__file__).resolve().parent / "site"
EVENTS = Path("/tmp/gh-fixture-events.jsonl")
PORT = 8773


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html") -> None:
        self.send_response(code)
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log_event(self, parsed) -> None:
        event = {"path": parsed.path,
                 "query": {k: v[0] for k, v in parse_qs(parsed.query).items()}}
        with EVENTS.open("a") as f:
            f.write(json.dumps(event) + "\n")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/reset":
            EVENTS.write_text("")
            self._send(200, b"reset", "text/plain")
            return
        if parsed.path == "/events":
            lines = EVENTS.read_text().splitlines() if EVENTS.exists() else []
            body = json.dumps([json.loads(l) for l in lines]).encode()
            self._send(200, body, "application/json")
            return
        self._log_event(parsed)
        # terminal actions get a plain confirmation page
        if parsed.path == "/submit" or parsed.path.startswith(("/learn/", "/set/")):
            name = parsed.path.strip("/").replace("/", " ")
            self._send(200, f"<html><head><title>Done: {name}</title></head>"
                            f"<body><h1>Recorded: {name}</h1></body></html>".encode())
            return
        rel = parsed.path.lstrip("/") or "index.html"
        target = (SITE / rel).resolve()
        if target.is_file() and target.is_relative_to(SITE):
            # tiny templating: {key} placeholders fill from the query string,
            # so multi-page flows can carry earlier choices forward in links
            html = target.read_text()
            for k, v in parse_qs(parsed.query).items():
                html = html.replace("{" + k + "}", v[0])
            self._send(200, html.encode())
        else:
            self._send(404, b"<html><body><h1>404</h1></body></html>")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    EVENTS.touch()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
