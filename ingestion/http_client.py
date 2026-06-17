"""Shared HTTP helper for Horizon API calls with retry/backoff.

Horizon occasionally returns transient 5xx/429 responses under load;
ingestion modules use `get_with_retry` instead of calling `httpx` directly
so those are retried with exponential backoff rather than failing the
whole pipeline run.

`AsyncHorizonClient` provides an async variant with semaphore-bounded
concurrency, used by the async pipeline entry point in `run_pipeline.async_run`.
"""

import asyncio
import random
import time

import httpx

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def get_with_retry(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> httpx.Response:
    """GET `url` via `client`, retrying transient failures with exponential backoff.

    Retries on connection errors and on `_RETRYABLE_STATUS_CODES` responses.
    Raises the underlying `httpx` exception (or calls `raise_for_status`) if
    all attempts fail.
    """
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            last_exception = exc
        else:
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response
            last_exception = httpx.HTTPStatusError(
                f"Retryable status {response.status_code} from {url}",
                request=response.request,
                response=response,
            )

        if attempt < max_retries:
            time.sleep(backoff_seconds * (2**attempt))

    assert last_exception is not None
    raise last_exception


class AsyncHorizonClient:
    """Async HTTP client for Horizon with semaphore-bounded concurrency and retry.

    Wraps `httpx.AsyncClient` with:
    - A semaphore that caps concurrent in-flight requests at `max_concurrency`.
    - Exponential backoff with jitter on 429 and 5xx responses (max `max_retries` retries).

    Supports async context-manager usage::

        async with AsyncHorizonClient(settings.horizon_url) as client:
            data = await client.get("/trades", params={"limit": 200})
    """

    def __init__(
        self,
        base_url: str,
        max_concurrency: int = 20,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(timeout=30.0)
        self._max_retries = max_retries

    def _resolve_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    async def get(self, path: str, params: dict | None = None) -> dict:
        """Async GET, returning parsed JSON.

        Acquires the concurrency semaphore for the duration of each HTTP
        round-trip. Retries on `_RETRYABLE_STATUS_CODES` or transport errors
        with exponential backoff + jitter.
        """
        url = self._resolve_url(path)
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                delay = (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                await asyncio.sleep(delay)

            try:
                async with self._semaphore:
                    response = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                continue

            if response.status_code not in _RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response.json()

            last_exc = httpx.HTTPStatusError(
                f"Retryable status {response.status_code} from {url}",
                request=response.request,
                response=response,
            )

        assert last_exc is not None
        raise last_exc

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncHorizonClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
