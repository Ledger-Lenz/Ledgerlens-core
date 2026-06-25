"""Unit tests for AsyncLedgerLensClient, using httpx.MockTransport so no
real network or server is needed. Also covers concurrent scoring via
asyncio.gather, per the issue's "async client tested with asyncio.gather
for concurrent scoring" acceptance criterion."""

import asyncio

import httpx
import pytest

from ledgerlens import AsyncLedgerLensClient, LedgerLensAPIError


def _async_client(handler) -> AsyncLedgerLensClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(base_url="https://test.local", transport=transport)
    return AsyncLedgerLensClient(base_url="https://test.local", client=http_client)


@pytest.mark.asyncio
async def test_get_score_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        wallet = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "scores": [
                    {
                        "wallet": wallet,
                        "asset_pair": "XLM/USDC",
                        "score": 42,
                        "benford_flag": False,
                        "ml_flag": False,
                        "confidence": 80,
                        "disputed": False,
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                ],
                "cross_chain_links": [],
            },
        )

    client = _async_client(handler)
    result = await client.get_score("GABC")
    assert result.scores[0].wallet == "GABC"
    await client.aclose()


@pytest.mark.asyncio
async def test_non_2xx_response_raises_ledgerlens_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    client = _async_client(handler)
    with pytest.raises(LedgerLensAPIError) as exc_info:
        await client.get_score("GABC")
    assert exc_info.value.status_code == 404
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_scoring_with_asyncio_gather():
    """Scores multiple wallets concurrently and asserts each response is
    correctly routed back to its own wallet (no cross-talk between
    concurrent requests)."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        wallet = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "scores": [
                    {
                        "wallet": wallet,
                        "asset_pair": "XLM/USDC",
                        "score": len(wallet),  # deterministic, distinguishable per wallet
                        "benford_flag": False,
                        "ml_flag": False,
                        "confidence": 80,
                        "disputed": False,
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                ],
                "cross_chain_links": [],
            },
        )

    client = _async_client(handler)
    wallets = ["GABC", "GDE", "GFGHIJ", "G"]
    results = await asyncio.gather(*(client.get_score(w) for w in wallets))

    assert call_count["n"] == len(wallets)
    for wallet, result in zip(wallets, results):
        assert result.scores[0].wallet == wallet
        assert result.scores[0].score == len(wallet)
    await client.aclose()


@pytest.mark.asyncio
async def test_async_context_manager_closes_underlying_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    async with _async_client(handler) as client:
        await client.health()
    assert client._client.is_closed
