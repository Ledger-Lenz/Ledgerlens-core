import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ingestion.http_client import AsyncHorizonClient, get_with_retry


def _client_with_handler(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_returns_response_on_success():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    client = _client_with_handler(handler)
    response = get_with_retry(client, "https://example.com")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_retries_on_retryable_status_then_succeeds():
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = _client_with_handler(handler)
    response = get_with_retry(client, "https://example.com", max_retries=3, backoff_seconds=0)

    assert response.status_code == 200
    assert calls["count"] == 3


def test_raises_after_exhausting_retries():
    def handler(request):
        return httpx.Response(503)

    client = _client_with_handler(handler)

    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(client, "https://example.com", max_retries=2, backoff_seconds=0)


def test_does_not_retry_non_retryable_error():
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        return httpx.Response(404)

    client = _client_with_handler(handler)

    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(client, "https://example.com", max_retries=3, backoff_seconds=0)

    assert calls["count"] == 1


# ---------------------------------------------------------------------------
# AsyncHorizonClient tests
# ---------------------------------------------------------------------------


def _make_mock_response(status_code: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.request = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=resp.request, response=resp
        )
    return resp


def _patch_client_get(client: AsyncHorizonClient, handler):
    """Replace the inner httpx.AsyncClient.get with an async callable."""
    client._client.get = handler
    return client


@pytest.mark.asyncio
async def test_async_client_returns_json_on_success():
    async def mock_get(url, params=None):
        return _make_mock_response(200, {"ok": True})

    client = AsyncHorizonClient("https://horizon.stellar.org")
    _patch_client_get(client, mock_get)

    result = await client.get("/trades")
    assert result == {"ok": True}
    await client.close()


@pytest.mark.asyncio
async def test_async_client_retries_on_retryable_status():
    calls = {"count": 0}

    async def mock_get(url, params=None):
        calls["count"] += 1
        if calls["count"] < 3:
            return _make_mock_response(503)
        return _make_mock_response(200, {"ok": True})

    client = AsyncHorizonClient("https://horizon.stellar.org", max_retries=3)
    _patch_client_get(client, mock_get)

    with patch("ingestion.http_client.asyncio.sleep"):
        result = await client.get("/trades")

    assert result == {"ok": True}
    assert calls["count"] == 3
    await client.close()


@pytest.mark.asyncio
async def test_async_client_raises_after_exhausting_retries():
    async def mock_get(url, params=None):
        return _make_mock_response(429)

    client = AsyncHorizonClient("https://horizon.stellar.org", max_retries=2)
    _patch_client_get(client, mock_get)

    with patch("ingestion.http_client.asyncio.sleep"):
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/trades")

    await client.close()


@pytest.mark.asyncio
async def test_async_client_does_not_retry_non_retryable_error():
    calls = {"count": 0}

    async def mock_get(url, params=None):
        calls["count"] += 1
        return _make_mock_response(404)

    client = AsyncHorizonClient("https://horizon.stellar.org", max_retries=3)
    _patch_client_get(client, mock_get)

    with pytest.raises(httpx.HTTPStatusError):
        await client.get("/trades")

    assert calls["count"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_async_client_respects_max_concurrency():
    """Never more than `max_concurrency` requests in flight simultaneously."""
    max_concurrent = 0
    current_concurrent = 0

    async def slow_get(url, params=None):
        nonlocal max_concurrent, current_concurrent
        current_concurrent += 1
        max_concurrent = max(max_concurrent, current_concurrent)
        await asyncio.sleep(0.02)
        current_concurrent -= 1
        return _make_mock_response(200, {})

    client = AsyncHorizonClient("https://horizon.stellar.org", max_concurrency=5)
    _patch_client_get(client, slow_get)

    tasks = [client.get(f"/accounts/{i}") for i in range(20)]
    await asyncio.gather(*tasks)

    assert max_concurrent <= 5
    await client.close()


@pytest.mark.asyncio
async def test_async_client_context_manager():
    async def mock_get(url, params=None):
        return _make_mock_response(200, {"data": 1})

    async with AsyncHorizonClient("https://horizon.stellar.org") as client:
        _patch_client_get(client, mock_get)
        result = await client.get("/test")

    assert result == {"data": 1}


@pytest.mark.asyncio
async def test_async_client_resolves_relative_paths():
    seen_urls = []

    async def mock_get(url, params=None):
        seen_urls.append(url)
        return _make_mock_response(200, {})

    client = AsyncHorizonClient("https://horizon.stellar.org")
    _patch_client_get(client, mock_get)

    await client.get("/trades")
    await client.get("accounts/test")
    await client.get("https://other.example.com/absolute")

    assert seen_urls[0] == "https://horizon.stellar.org/trades"
    assert seen_urls[1] == "https://horizon.stellar.org/accounts/test"
    assert seen_urls[2] == "https://other.example.com/absolute"
    await client.close()
