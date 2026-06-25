"""Unit tests for the synchronous LedgerLensClient, using httpx.MockTransport
so no real network or server is needed."""

import httpx
import pytest

from ledgerlens import LedgerLensAPIError, LedgerLensClient


def _client(handler) -> LedgerLensClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://test.local", transport=transport)
    return LedgerLensClient(base_url="https://test.local", client=http_client)


def test_get_score_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/scores/GABC"
        return httpx.Response(
            200,
            json={
                "scores": [
                    {
                        "wallet": "GABC",
                        "asset_pair": "XLM/USDC",
                        "score": 82,
                        "benford_flag": True,
                        "ml_flag": True,
                        "confidence": 90,
                        "disputed": False,
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                ],
                "cross_chain_links": [],
            },
        )

    client = _client(handler)
    result = client.get_score("GABC")
    assert len(result.scores) == 1
    assert result.scores[0].wallet == "GABC"
    assert result.scores[0].score == 82
    assert result.cross_chain_links == []


def test_get_score_includes_cross_chain_links():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "scores": [],
                "cross_chain_links": [
                    {"chain": "ethereum", "evm_wallet": "0xabc", "last_bridge_at": "2026-01-01T00:00:00Z"}
                ],
            },
        )

    client = _client(handler)
    result = client.get_score("GABC")
    assert len(result.cross_chain_links) == 1
    assert result.cross_chain_links[0].chain == "ethereum"


def test_non_2xx_response_raises_ledgerlens_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No scores found for wallet GABC"})

    client = _client(handler)
    with pytest.raises(LedgerLensAPIError) as exc_info:
        client.get_score("GABC")
    assert exc_info.value.status_code == 404
    assert "No scores found" in exc_info.value.detail


def test_non_json_error_body_falls_back_to_raw_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    client = _client(handler)
    with pytest.raises(LedgerLensAPIError) as exc_info:
        client.get_score("GABC")
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "internal server error"


def test_list_scores_sends_query_params_and_drops_none():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    client = _client(handler)
    client.list_scores(min_score=50, benford_flag=None)
    assert captured["params"]["min_score"] == "50"
    assert "benford_flag" not in captured["params"]


def test_create_webhook_posts_body_and_parses_subscriber_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/webhooks"
        return httpx.Response(201, json={"subscriber_id": "sub_123"})

    client = _client(handler)
    result = client.create_webhook(url="https://example.com/hook", secret="s3cr3t")
    assert result.subscriber_id == "sub_123"


def test_api_key_sent_as_admin_header_even_with_custom_client():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("x-ledgerlens-admin-key")
        return httpx.Response(200, json={"recorded": 3})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://test.local", transport=transport)
    client = LedgerLensClient(base_url="https://test.local", api_key="my-admin-key", client=http_client)
    client.submit_feedback("GABC", "XLM/USDC", ground_truth=1, scored_at="2026-01-01T00:00:00Z")
    assert captured["header"] == "my-admin-key"


def test_context_manager_closes_underlying_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    with _client(handler) as client:
        client.health()
    assert client._client.is_closed
