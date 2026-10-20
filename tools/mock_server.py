#!/usr/bin/env python3
"""A fixture-backed mock of the Hyperliquid Peer API. Python stdlib only.

    python3 tools/mock_server.py --port 8931
    curl -s 'http://127.0.0.1:8931/peers?tier=1' | head

It serves exactly what is in `fixtures/http/*.json` and invents nothing. Use it
for integration tests and for the runnable examples in the docs.

Matching
--------
A request matches a fixture when method, path and the FULL query string (as a
set of key/value pairs) are equal, and every header named in the fixture's
`request.headers` is present with that value (case-insensitive name, exact
value). Fixtures that name headers are preferred over ones that do not, so
`X-Mock-Scenario: rate-limited` beats the plain 200 for the same query.

Beyond replaying fixtures it adds exactly two behaviours, both of which the
real API has and neither of which can be expressed as a static fixture:

  * If-None-Match matching the fixture's ETag  -> 304, no body.
  * --fail-first-n N: the first N requests answer 503, to exercise retries.

Unmatched requests answer 404 problem+json naming the closest fixtures, so a
test failure says which fixture you meant to hit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP_DIR = os.path.join(ROOT, "fixtures", "http")


def load_fixtures(path=HTTP_DIR):
    out = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(path, name), encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


def qkey(pairs):
    return tuple(sorted((str(k), str(v)) for k, v in pairs))


class State:
    def __init__(self, fixtures, fail_first_n=0, verbose=False):
        self.fixtures = fixtures
        self.fail_first_n = fail_first_n
        self.verbose = verbose
        self.count = 0
        self.lock = threading.Lock()

    def next_request(self):
        with self.lock:
            self.count += 1
            return self.count


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: State = None  # set by main()

    def log_message(self, fmt, *args):
        if self.state.verbose:
            sys.stderr.write("mock_server: " + fmt % args + "\n")

    # -- matching ----------------------------------------------------------
    def _candidates(self, path, query):
        exact, loose = [], []
        for fx in self.state.fixtures:
            rq = fx["request"]
            if rq["method"] != self.command or rq["path"] != path:
                continue
            if qkey(rq.get("query", {}).items()) != query:
                continue
            want = rq.get("headers") or {}
            if want:
                if all(self.headers.get(k) == v for k, v in want.items()):
                    exact.append(fx)
            else:
                loose.append(fx)
        return exact + loose

    def _send(self, status, headers, body_bytes):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, str(v))
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        if body_bytes:
            self.wfile.write(body_bytes)

    def _problem(self, status, type_suffix, title, detail, headers=None):
        body = json.dumps({
            "type": "https://example.invalid/problems/" + type_suffix,
            "title": title, "status": status, "detail": detail,
        }).encode()
        h = {"Content-Type": "application/problem+json"}
        h.update(headers or {})
        self._send(status, h, body)

    def do_GET(self):  # noqa: N802
        n = self.state.next_request()
        if n <= self.state.fail_first_n:
            self._problem(503, "producer-unavailable", "Peer graph unavailable",
                          "Injected failure %d of %d (--fail-first-n)."
                          % (n, self.state.fail_first_n))
            return

        parsed = urlparse(self.path)
        query = qkey(parse_qsl(parsed.query, keep_blank_values=True))
        matches = self._candidates(parsed.path, query)

        if not matches:
            near = sorted({fx["request"]["path"] for fx in self.state.fixtures
                           if fx["request"]["method"] == self.command})
            self._problem(404, "no-fixture", "No fixture matches this request",
                          "%s %s : no fixture. Known paths: %s"
                          % (self.command, self.path, ", ".join(near)))
            return

        fx = matches[0]
        resp = fx["response"]
        headers = dict(resp.get("headers") or {})

        # conditional request handling, for any fixture that carries an ETag
        inm = self.headers.get("If-None-Match")
        if inm and resp.get("status") == 200 and headers.get("ETag") == inm:
            keep = {k: v for k, v in headers.items()
                    if k in ("ETag", "Cache-Control", "API-Version") or k.startswith("RateLimit-")}
            self._send(304, keep, b"")
            return

        if "body_text" in resp:
            body = resp["body_text"].encode("utf-8")
        elif resp.get("body") is None:
            body = b""
        else:
            body = json.dumps(resp["body"], ensure_ascii=False).encode("utf-8")
        self._send(resp["status"], headers, body)

    do_HEAD = do_GET


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8931)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--fixtures", default=HTTP_DIR)
    ap.add_argument("--fail-first-n", type=int, default=0,
                    help="answer 503 to the first N requests, to exercise retries")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    fixtures = load_fixtures(args.fixtures)
    Handler.state = State(fixtures, args.fail_first_n, args.verbose)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write("mock_server: %d fixtures on http://%s:%d\n"
                     % (len(fixtures), args.host, args.port))
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
