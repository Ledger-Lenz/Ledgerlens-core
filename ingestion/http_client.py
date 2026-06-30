"""Shared HTTP helper for Horizon API calls with rate-limiting and retry/backoff.

Horizon occasionally returns transient 5xx/429 responses under load;
ingestion modules use `RetryingHorizonClient` instead of calling `httpx`
directly so those are retried with full-jitter exponential backoff and a
proactive token-bucket rate limiter rather than failing the whole pipeline.

Key components
--------------
TokenBucketRateLimiter
    Proactively throttles outbound request dispatch to stay below the
    Horizon per-IP rate limit, preventing 429s from occurring in the first
    place.  The bucket is shared across all callers of the same
    ``RetryingHorizonClient`` instance.

compute_retry_delay
    AWS-style "full jitter" delay: ``uniform(0, min(max_delay, base*2^n))``.
    When a ``Retry-After`` value is present the computed delay is floored to
    ``retry_after + uniform(0, 1)`` so the server-dictated minimum is
    respected while still de-synchronising concurrent workers.

parse_retry_after
    Parses both the integer-seconds and HTTP-date forms of the
    ``Retry-After`` header, returning the number of seconds to wait.

RateLimitStats
    Lightweight counters attached to every ``RetryingHorizonClient``
    instance; useful for observability dashboards and test assertions.

MaxRetriesExceededError
    Raised when all retry attempts are exhausted without a successful
    response.

`AsyncHorizonClient` / `RetryingHorizonClient`
    Async HTTP client wrapping ``httpx.AsyncClient`` with semaphore-bounded
    concurrency, the token-bucket rate limiter, full-jitter retry, and
    Retry-After awareness.

`get_with_retry`
    Legacy sync helper retained for backward compatibility.
"""

import asyncio
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Status codes that warrant a retry with backoff.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

#: Status codes that must never be retried — raise immediately.
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 410}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MaxRetriesExceededError(Exception):
    """Raised when a request fails on every retry attempt.

    Deliberately omits response body content to avoid leaking potentially
    sensitive data from the Horizon node into exception messages and logs.

    Attributes
    ----------
    url:
        The request URL (path only, never a full URL with credentials).
    attempts:
        Total number of attempts made (initial request + retries).
    """

    def __init__(self, url: str, attempts: int) -> None:
        self.url = url
        self.attempts = attempts
        super().__init__(
            f"Request to {url!r} failed after {attempts} attempt(s)"
        )


# ---------------------------------------------------------------------------
# RateLimitStats
# ---------------------------------------------------------------------------


@dataclass
class RateLimitStats:
    """Counters tracking rate-limiting and retry activity on a single client.

    Attributes
    ----------
    requests_sent:
        Total number of HTTP requests dispatched (across all attempts).
    retries_total:
        Number of requests that required at least one retry.
    rate_limit_hits:
        Number of HTTP 429 responses received.
    total_wait_seconds:
        Cumulative seconds spent sleeping in retry/rate-limit backoff.
    """

    requests_sent: int = 0
    retries_total: int = 0
    rate_limit_hits: int = 0
    total_wait_seconds: float = 0.0

    @property
    def retry_rate(self) -> float:
        """Fraction of requests that required at least one retry.

        Returns 0.0 when no requests have been sent yet.
        """
        return self.retries_total / self.requests_sent if self.requests_sent else 0.0


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------


class TokenBucketRateLimiter:
    """Async token-bucket rate limiter for outbound HTTP requests.

    Tokens are added at ``rate`` per second up to a maximum ``burst``
    capacity.  Each call to :meth:`acquire` consumes one token; when the
    bucket is empty the caller is suspended until enough tokens have
    accumulated.

    The internal ``asyncio.Lock`` ensures correct behaviour under high
    concurrency in a single event loop — it must **not** be shared across
    threads.  For multi-process deployments, create one limiter per process.

    Parameters
    ----------
    rate:
        Steady-state token replenishment rate in requests per second.
        This should be set to slightly below Horizon's documented per-IP
        limit to leave headroom.
    burst:
        Maximum bucket capacity.  Allows short bursts above the steady
        rate.  Defaults to ``rate * 2`` when not specified.

    Example
    -------
    ::

        limiter = TokenBucketRateLimiter(rate=5.0, burst=10.0)
        async with AsyncHorizonClient(..., rate_limiter=limiter) as client:
            data = await client.get("/trades")
    """

    def __init__(self, rate: float, burst: float | None = None) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate!r}")
        self._rate = rate
        self._capacity = burst if burst is not None else rate * 2
        if self._capacity < rate:
            raise ValueError(
                f"burst ({burst}) must be >= rate ({rate})"
            )
        self._tokens: float = self._capacity
        self._last_refill: float = time.monotonic()
        # Must be an asyncio.Lock — a threading.Lock would deadlock in the
        # async event loop because it is not awaitable.
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self) -> None:
        """Block until a request token is available, then consume it.

        This is the only method callers need.  The rate limiter tracks
        elapsed wall time since the last refill to compute the correct
        token count even when the event loop is busy.
        """
        async with self._lock:
            self._refill()
            if self._tokens < 1.0:
                # Not enough tokens — sleep for the exact time needed and
                # then refill again to handle any elapsed time during sleep.
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
                await asyncio.sleep(wait)
                self._refill()
            self._tokens -= 1.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens proportional to elapsed time since the last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now


# ---------------------------------------------------------------------------
# Retry delay computation
# ---------------------------------------------------------------------------


def compute_retry_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retry_after: float | None = None,
) -> float:
    """Compute a full-jitter retry delay for the given attempt index.

    Uses the AWS "full jitter" pattern: ``uniform(0, min(max_delay, base * 2^attempt))``.
    Jitter de-synchronises concurrent workers that all hit a transient
    failure at the same time, preventing the thundering-herd re-storm.

    If *retry_after* is supplied (parsed from the server's ``Retry-After``
    header), the returned delay is floored to ``retry_after + uniform(0, 1)``
    so the server-mandated minimum wait is respected, while a small random
    offset prevents multiple clients from retrying simultaneously at the exact
    same moment.

    Parameters
    ----------
    attempt:
        Zero-based attempt index.  ``attempt=0`` for the very first retry
        (i.e. the second overall request).
    base_delay:
        Base delay in seconds.  The exponential cap grows as
        ``base_delay * 2^attempt``.
    max_delay:
        Hard upper bound on the jittered delay in seconds.
    retry_after:
        Server-dictated minimum wait in seconds, already clamped to
        ``max_delay`` by the caller.  If ``None``, only jitter is applied.

    Returns
    -------
    float
        Seconds to sleep before the next attempt.  Always in ``[0, max_delay]``.
    """
    exponential_cap = min(max_delay, base_delay * (2 ** attempt))
    jittered = random.uniform(0.0, exponential_cap)
    if retry_after is not None:
        # Floor to the server minimum, plus a tiny uniform offset to spread
        # concurrent retries.  The +1 s window is intentionally small so the
        # server-prescribed delay remains the dominant factor.
        return max(retry_after + random.uniform(0.0, 1.0), jittered)
    return jittered


# ---------------------------------------------------------------------------
# Retry-After header parsing
# ---------------------------------------------------------------------------


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse the ``Retry-After`` response header.

    Supports both the integer-seconds form (``Retry-After: 30``) and the
    HTTP-date form (``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT``).

    Parameters
    ----------
    headers:
        A case-insensitive mapping of HTTP response headers.  Both
        ``Retry-After`` and ``retry-after`` keys are checked.

    Returns
    -------
    float or None
        Seconds to wait before retrying, or ``None`` when the header is
        absent or cannot be parsed.  Negative values are clamped to ``0.0``.
    """
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    # Integer-seconds form
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    # HTTP-date form
    try:
        from email.utils import parsedate_to_datetime

        retry_dt = parsedate_to_datetime(value)
        wait = (retry_dt - datetime.now(tz=timezone.utc)).total_seconds()
        return max(0.0, wait)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sync helper (backward compatibility)
# ---------------------------------------------------------------------------


def get_with_retry(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 1.0,
) -> httpx.Response:
    """GET ``url`` via ``client``, retrying transient failures with exponential backoff.

    Retries on connection errors and on ``_RETRYABLE_STATUS_CODES`` responses.
    Raises the underlying ``httpx`` exception (or calls ``raise_for_status``)
    if all attempts fail.

    .. note::
        This is the legacy synchronous helper.  New code should use
        :class:`RetryingHorizonClient` (async) for full rate-limit support.
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


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


import logging

logger = logging.getLogger(__name__)


class AsyncHorizonClient:
    """Async HTTP client for Horizon with token-bucket rate limiting and retry.

    Wraps ``httpx.AsyncClient`` with:

    - A **token-bucket rate limiter** that proactively throttles dispatch to
      stay below the Horizon per-IP request budget before 429s occur.
    - A **semaphore** that caps the number of concurrent in-flight requests.
    - **Full-jitter exponential backoff** on 429 and 5xx responses, with
      ``Retry-After`` header awareness so the server-mandated minimum wait is
      respected.
    - **Non-retriable status codes** (400, 401, 403, 404, 410) are raised
      immediately without burning retry budget.
    - **:attr:`rate_limit_stats`** for observability.

    Parameters
    ----------
    base_url:
        Root URL of the Horizon node, e.g. ``"https://horizon.stellar.org"``.
    max_concurrency:
        Maximum number of in-flight HTTP requests at any moment.
    max_retries:
        Maximum number of retry attempts after the initial request.
    base_retry_delay:
        Base delay (seconds) for the full-jitter exponential back-off.
    max_retry_delay:
        Hard cap on any single retry sleep, including ``Retry-After`` values.
        This prevents a malicious or mis-configured proxy from stalling the
        pipeline indefinitely with an arbitrarily large ``Retry-After``.
    rate_limiter:
        A pre-constructed :class:`TokenBucketRateLimiter`.  When ``None``,
        a limiter is built from *rate_limit_rps* / *rate_burst*.
    rate_limit_rps:
        Steady-state requests per second for the default rate limiter.
        Ignored when *rate_limiter* is provided.
    rate_burst:
        Burst capacity for the default rate limiter.  Ignored when
        *rate_limiter* is provided.

    Supports async context-manager usage::

        async with AsyncHorizonClient(settings.horizon_url) as client:
            data = await client.get("/trades", params={"limit": 200})
    """

    def __init__(
        self,
        base_url: str,
        max_concurrency: int = 20,
        max_retries: int = 3,
        base_retry_delay: float = 1.0,
        max_retry_delay: float = 60.0,
        rate_limiter: TokenBucketRateLimiter | None = None,
        rate_limit_rps: float = 5.0,
        rate_burst: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(timeout=30.0)
        self.max_retries = max_retries
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay
        self._rate_limiter: TokenBucketRateLimiter = rate_limiter or TokenBucketRateLimiter(
            rate=rate_limit_rps, burst=rate_burst
        )
        self.rate_limit_stats = RateLimitStats()

    # ------------------------------------------------------------------
    # URL resolution
    # ------------------------------------------------------------------

    def _resolve_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # Public request API
    # ------------------------------------------------------------------

    async def get(self, path: str, params: dict | None = None) -> dict:
        """Async GET returning parsed JSON.

        Acquires the token-bucket rate limiter before each attempt and the
        concurrency semaphore for the duration of the HTTP round-trip.
        Retries transient failures with full-jitter backoff and respects any
        ``Retry-After`` header on 429 responses.

        Parameters
        ----------
        path:
            Absolute URL or path relative to *base_url*.
        params:
            Optional query-string parameters.

        Returns
        -------
        dict
            Parsed JSON body of the successful response.

        Raises
        ------
        MaxRetriesExceededError
            All retry attempts exhausted without a successful response.
        httpx.HTTPStatusError
            Non-retriable status code received (400, 401, 403, 404, 410).
        """
        url = self._resolve_url(path)
        return await self._make_request("GET", url, params=params)

    async def _make_request(
        self, method: str, url: str, **kwargs: object
    ) -> dict:
        """Internal request dispatcher with rate limiting, retry, and stats.

        Implements the full retry loop:

        1. Acquire a token from the rate limiter (may sleep to respect RPS
           budget).
        2. Acquire the concurrency semaphore.
        3. Dispatch the request.
        4. On 429 — parse ``Retry-After``, update stats, sleep with jitter,
           continue.
        5. On non-retriable 4xx — raise immediately.
        6. On retriable 5xx or transport error — sleep with jitter, continue.
        7. After all retries exhausted — raise
           :exc:`MaxRetriesExceededError`.

        Parameters
        ----------
        method:
            HTTP method string (e.g. ``"GET"``).
        url:
            Fully-resolved request URL.
        **kwargs:
            Extra keyword arguments forwarded to ``httpx.AsyncClient.request``.
        """
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            # ----------------------------------------------------------
            # Proactive rate limiting — acquire before sending
            # ----------------------------------------------------------
            await self._rate_limiter.acquire()

            try:
                async with self._semaphore:
                    response = await self._client.request(method, url, **kwargs)
                self.rate_limit_stats.requests_sent += 1
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = compute_retry_delay(
                        attempt,
                        base_delay=self._base_retry_delay,
                        max_delay=self._max_retry_delay,
                    )
                    logger.debug(
                        "Transport error on %s (attempt %d/%d); retrying in %.2fs",
                        url,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    self.rate_limit_stats.retries_total += 1
                    self.rate_limit_stats.total_wait_seconds += delay
                    await asyncio.sleep(delay)
                continue

            # ----------------------------------------------------------
            # HTTP 429 — rate limited by Horizon
            # ----------------------------------------------------------
            if response.status_code == 429:
                self.rate_limit_stats.rate_limit_hits += 1
                raw_retry_after = parse_retry_after(response.headers)
                # Clamp to max_retry_delay to prevent DoS via huge header
                clamped_retry_after = (
                    min(raw_retry_after, self._max_retry_delay)
                    if raw_retry_after is not None
                    else None
                )
                delay = compute_retry_delay(
                    attempt,
                    base_delay=self._base_retry_delay,
                    max_delay=self._max_retry_delay,
                    retry_after=clamped_retry_after,
                )
                logger.warning(
                    "Rate limited by Horizon (attempt %d/%d); waiting %.2fs "
                    "(Retry-After=%s)",
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                    raw_retry_after,
                )
                if attempt < self.max_retries:
                    self.rate_limit_stats.retries_total += 1
                    self.rate_limit_stats.total_wait_seconds += delay
                    await asyncio.sleep(delay)
                    continue
                # Final attempt also got 429 — fall through to exhaustion
                last_exc = httpx.HTTPStatusError(
                    f"HTTP 429 from {url}",
                    request=response.request,
                    response=response,
                )
                continue

            # ----------------------------------------------------------
            # Non-retriable 4xx — fail fast
            # ----------------------------------------------------------
            if response.status_code in _NON_RETRYABLE_STATUS_CODES:
                response.raise_for_status()

            # ----------------------------------------------------------
            # Retriable 5xx
            # ----------------------------------------------------------
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {response.status_code} from {url}",
                    request=response.request,
                    response=response,
                )
                if attempt < self.max_retries:
                    delay = compute_retry_delay(
                        attempt,
                        base_delay=self._base_retry_delay,
                        max_delay=self._max_retry_delay,
                    )
                    logger.warning(
                        "HTTP %d on %s (attempt %d/%d); retrying in %.2fs",
                        response.status_code,
                        url,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    self.rate_limit_stats.retries_total += 1
                    self.rate_limit_stats.total_wait_seconds += delay
                    await asyncio.sleep(delay)
                continue

            # ----------------------------------------------------------
            # Success
            # ----------------------------------------------------------
            response.raise_for_status()
            return response.json()

        raise MaxRetriesExceededError(url, self.max_retries + 1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncHorizonClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Backward-compatible alias
# ---------------------------------------------------------------------------

#: Descriptive alias used by historical ingestion and streaming modules.
RetryingHorizonClient = AsyncHorizonClient
