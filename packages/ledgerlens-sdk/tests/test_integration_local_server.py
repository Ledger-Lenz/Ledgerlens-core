"""Integration test: runs the SDK against a real local HTTP server (stdlib
`http.server`, no extra dependencies) rather than a mocked transport, per
the issue's "Integration test runs SDK against a local test server"
acceptance criterion.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ledgerlens import LedgerLensAPIError, LedgerLensClient

_SCORE_RESPONSE = {
    "scores": [
        {
            "wallet": "GINTEGRATIONTEST",
            "asset_pair": "XLM/USDC",
            "score": 77,
            "benford_flag": True,
            "ml_flag": False,
            "confidence": 85,
            "disputed": False,
            "timestamp": "2026-01-01T00:00:00Z",
        }
    ],
    "cross_chain_links": [],
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # silence test server request logging

    def do_GET(self):
        if self.path == "/scores/GINTEGRATIONTEST":
            body = json.dumps(_SCORE_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/scores/UNKNOWN":
            body = json.dumps({"detail": "No scores found for wallet UNKNOWN"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_get_score_against_real_local_server(local_server):
    with LedgerLensClient(base_url=local_server) as client:
        result = client.get_score("GINTEGRATIONTEST")
        assert result.scores[0].wallet == "GINTEGRATIONTEST"
        assert result.scores[0].score == 77


def test_get_score_404_against_real_local_server(local_server):
    with LedgerLensClient(base_url=local_server) as client:
        with pytest.raises(LedgerLensAPIError) as exc_info:
            client.get_score("UNKNOWN")
        assert exc_info.value.status_code == 404
        assert "No scores found" in exc_info.value.detail
