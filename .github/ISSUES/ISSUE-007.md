---
title: "Implement Rate-Limit-Aware Retry Logic with Jitter for Horizon HTTP Client"
labels: ["difficulty: advanced", "area: ingestion", "type: reliability"]
assignees: []
---

## Summary
`ingestion/http_client.py` currently implements basic retry logic that does not correctly handle HTTP 429 Too Many Requests responses from Horizon — it does not respect the `Retry-After` header, does not apply jitter to prevent thundering-herd retry storms when multiple workers hit the rate limit simultaneously, and has no per-host rate budget to proactively avoid hitting the limit in the first place. Robust rate-limit-aware retry logic is essential for the parallel historical loader (ISSUE-003) and the real-time streamer to coexist without saturating the Horizon API.

## Background & Context
`ingestion/http_client.py` provides `RetryingHorizonClient`, which all ingestion modules use for HTTP calls to the Stellar Horizon API. The current retry implementation uses a simple fixed-delay retry loop with a maximum attempt count.

Horizon enforces rate limits per IP and returns `HTTP 429` with a `Retry-After` header specifying the number of seconds to wait. If multiple async workers (from the parallel historical loader in ISSUE-003) all hit the rate limit at the same time and all retry after the same fixed delay, they will all hammer the server again simultaneously — the classic **thundering herd** problem.

The solution requires three complementary mechanisms:
1. **`Retry-After` header respect**: parse the `Retry-After` header on 429 responses and wait exactly that long before retrying.
2. **Full jitter**: add random jitter to all retry delays (not just 429s) to desynchronise concurrent workers: `delay = random.uniform(0, base_delay)` (AWS "full jitter" pattern).
3. **Token-bucket rate limiter**: a client-side rate limiter that proactively throttles request dispatch to stay below the Horizon rate limit, preventing 429s from occurring in the first place.

## Objectives
- [ ] Implement a `TokenBucketRateLimiter` class in `ingestion/http_client.py` that enforces a configurable requests-per-second budget, shared across all workers using the same `RetryingHorizonClient` instance.
- [ ] Update the retry loop in `RetryingHorizonClient` to parse the `Retry-After` header on 429 responses and wait the specified duration (plus jitter) before retrying, with a configurable maximum wait cap.
- [ ] Apply full jitter to all retry delays (not just 429s) using `random.uniform(0, calculated_delay)` and log the computed delay at `DEBUG` level.
- [ ] Add a `RateLimitStats` dataclass tracking `requests_sent`, `retries_total`, `rate_limit_hits` (HTTP 429 count), `total_wait_seconds`, and expose it on `RetryingHorizonClient.rate_limit_stats`.

## Technical Requirements

**Token bucket algorithm:**
```python
import asyncio, time

class TokenBucketRateLimiter:
    def __init__(self, rate: float, burst: float | None = None):
        """
        rate: tokens (requests) per second to add to the bucket
        burst: max bucket capacity (default = rate * 2 for short bursts)
        """
        self._rate = rate
        self._capacity = burst or rate * 2
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available."""
        async with self._lock:
            self._refill()
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
                await asyncio.sleep(wait)
                self._refill()
            self._tokens -= 1.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now
```

**Full jitter retry delay computation:**
```python
import random

def compute_retry_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retry_after: float | None = None,
) -> float:
    """
    Full jitter: delay = uniform(0, min(max_delay, base * 2^attempt))
    If retry_after is provided (from Retry-After header), use max(retry_after, jittered_delay).
    """
    exponential_cap = min(max_delay, base_delay * (2 ** attempt))
    jittered = random.uniform(0, exponential_cap)
    if retry_after is not None:
        return max(retry_after + random.uniform(0, 1.0), jittered)
    return jittered
```

**`Retry-After` header parsing:**
```python
def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """
    Parse Retry-After header. Supports both integer seconds and HTTP date format.
    Returns seconds to wait, or None if header absent.
    """
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)       # integer seconds format
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        retry_dt = parsedate_to_datetime(value)
        wait = (retry_dt - datetime.now(tz=timezone.utc)).total_seconds()
        return max(0.0, wait)     # clamp to >= 0
    except Exception:
        return None
```

**Updated retry loop in `_make_request`:**
```python
for attempt in range(self.max_retries + 1):
    await self._rate_limiter.acquire()
    try:
        response = await self._client.request(method, url, **kwargs)
        if response.status_code == 429:
            retry_after = parse_retry_after(response.headers)
            delay = compute_retry_delay(attempt, retry_after=retry_after)
            self._stats.rate_limit_hits += 1
            logger.warning("Rate limited by Horizon (attempt %d/%d); waiting %.2fs",
                           attempt + 1, self.max_retries, delay)
            await asyncio.sleep(delay)
            continue
        response.raise_for_status()
        return response
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            raise  # don't retry 4xx (except 429 handled above)
        delay = compute_retry_delay(attempt)
        logger.warning("HTTP %d on %s (attempt %d/%d); retrying in %.2fs",
                       exc.response.status_code, url, attempt + 1, self.max_retries, delay)
        await asyncio.sleep(delay)
raise MaxRetriesExceededError(url, self.max_retries)
```

**Non-retriable status codes**: 400, 401, 403, 404, 410 — raise immediately without retry. 429, 500, 502, 503, 504 — retry with jitter.

**`RateLimitStats`:**
```python
@dataclass
class RateLimitStats:
    requests_sent: int = 0
    retries_total: int = 0
    rate_limit_hits: int = 0
    total_wait_seconds: float = 0.0

    @property
    def retry_rate(self) -> float:
        return self.retries_total / self.requests_sent if self.requests_sent else 0.0
```

**Configuration** (add to `config/settings.py`):
- `HORIZON_RATE_LIMIT_RPS`: default `5.0` (requests per second)
- `HORIZON_RATE_BURST`: default `10.0` (burst capacity)
- `HORIZON_MAX_RETRIES`: default `5`
- `HORIZON_BASE_RETRY_DELAY`: default `1.0`
- `HORIZON_MAX_RETRY_DELAY`: default `60.0`

## Security Considerations
- The `Retry-After` header value must be clamped to a maximum of `HORIZON_MAX_RETRY_DELAY` seconds — an attacker-controlled proxy could return `Retry-After: 999999` to cause a denial-of-service by stalling the ingestion pipeline indefinitely.
- Jitter must use `random.uniform` from the standard library (not a predictable PRNG seed) so retry timings cannot be predicted and exploited to synchronise attacks.
- `MaxRetriesExceededError` must include the URL and attempt count but must not include response body content (which could contain sensitive data from the Horizon node).
- The token bucket `_lock` must be an `asyncio.Lock` (not a threading lock) to prevent deadlocks in the async event loop.

## Testing Requirements
- Unit tests covering `TokenBucketRateLimiter`: acquire tokens at configured rate, burst capacity not exceeded, wait correctly when tokens exhausted
- Unit tests covering `compute_retry_delay`: output within `[0, max_delay]`, `retry_after` floor applied, exponential growth with attempt count
- Unit tests covering `parse_retry_after`: integer string, HTTP date string, missing header (None), malformed value (None)
- Unit tests covering retry loop: mock 2 consecutive 429s then a 200; assert `rate_limit_hits == 2`, `retries_total == 2`, final response returned correctly
- Unit tests covering non-retriable codes: 400, 404 raise immediately without retry; 503 retries
- Integration tests: mock Horizon server that returns 429 with `Retry-After: 1` on first request; assert the client waits ~1 second and retries successfully
- Integration tests with `concurrency=5` workers: assert no thundering herd (retry times are spread across a time window, not all identical)
- Edge cases: `max_retries=0` (no retries, raise on first failure), `Retry-After: 0` (immediate retry with jitter), HTTP date in the past
- Performance benchmark: 1,000 successful requests through the rate limiter at 100 RPS should take ~10 seconds (verifying rate limiter accuracy)

## Documentation Requirements
- Add docstrings to `TokenBucketRateLimiter`, `compute_retry_delay`, `parse_retry_after`, and the updated `_make_request`
- Update `docs/ingestion.md` with a section on rate limiting configuration and how to tune `HORIZON_RATE_LIMIT_RPS` for different deployment environments (public testnet vs private mainnet node)
- Update `README.md` configuration table with the new `HORIZON_*` environment variables
- Add a comment in `config/settings.py` explaining the relationship between `HORIZON_RATE_LIMIT_RPS` and Horizon's per-IP limits

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: Python `asyncio`, HTTP retry patterns, token bucket / leaky bucket rate limiting, `httpx` async client
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python async engineer with deep experience in resilient HTTP client design. Specific knowledge of rate-limiting algorithms (token bucket, leaky bucket), jitter patterns (AWS full jitter paper is a great reference), and `Retry-After` header semantics is essential. Experience building clients for public blockchain APIs under rate-limit pressure is a strong plus.
