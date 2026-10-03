"""Shared reference server for the cross-SDK conformance suite.

Serves the canned responses in ``cases.json``. A runner selects the active
case with ``POST /__conformance/select?case=<id>`` and then invokes the SDK
operation. The server checks that the SDK sent the request the case expects
(method, path, query) and answers ``418`` with a diagnostic otherwise, so a
divergent SDK fails the case. Like the real API, paths are accepted both with
and without the ``/v1`` prefix.

Usage: python tests/contract/conformance/reference_server.py [port]
Stdlib only, so every SDK's CI job can run it without extra dependencies.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

CASES = {
    c["id"]: c for c in json.loads((Path(__file__).parent / "cases.json").read_text())["cases"]
}


class Handler(BaseHTTPRequestHandler):
    active: dict | None = None

    def _send(self, status: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/__conformance/select":
            case_id = dict(parse_qsl(url.query)).get("case", "")
            if case_id not in CASES:
                self._send(400, {"detail": f"unknown case {case_id!r}"})
                return
            Handler.active = CASES[case_id]
            self._send(200, {"selected": case_id})
            return

        case = Handler.active
        if case is None:
            self._send(418, {"detail": "no conformance case selected"})
            return
        path = url.path[3:] if url.path.startswith("/v1/") else url.path
        got = {"method": self.command, "path": path, "query": dict(parse_qsl(url.query))}
        if got != case["request"]:
            self._send(
                418, {"detail": "unexpected request", "expected": case["request"], "got": got}
            )
            return
        self._send(case["response"]["status"], case["response"]["body"])

    do_GET = do_POST = do_DELETE = _handle

    def log_message(self, fmt: str, *args: object) -> None:  # keep CI logs quiet
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print(f"conformance reference server on :{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
