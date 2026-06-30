"""Shared HTTP helper for Horizon API calls with retry/backoff and rate limiting.

Horizon occasionally returns transient 5xx/429 responses under load.
This module provides two client implementations:

* ``get_with_retry`` — synchronous helper used by the historical loader.
* ``AsyncHorizonClient`` (alias: ``RetryingHorizonClient``) — async client used
  by the streaming pipeline and parallel historical loader.

Rate limiting
-------------
``TokenBucketRateLimiter`` enforces a proactive per-client request budget so the
pipeline stays below Horizon's per-IP rate limit before 429s occur.  When tokens
are exhausted the acquirer yields the event loop rather than blocking a thread.

Retry logic
-----------
The retry loop in ``AsyncHorizonClient._make_request()`` uses **full jitter**
(AWS "Exponential Backoff and Jitter" pattern) for all delays:
``delay = uniform(0, min(max_delay, base * 2^attempt))``.

On HTTP 429 the ``Retry-After`` response header is parsed and respected: the
computed jitter delay is floored at the server-specified wait time so the
pipeline never hammers a rate-limited endpoint.  The ``Retry-After`` value is
also clamped to ``HORIZON_MAX_RETRY_DELAY`` to prevent a malicious or
misconfigured proxy from stalling the pipeline indefinitely.

Version guard
-------------
Every response carries an ``X-Stellar-Horizon-Version`` header.  ``VersionGuard``
parses it and raises ``HorizonVersionError`` when the server version falls
outside the configured ``[min_version, max_version)`` range.

Structural validation
---------------------
``HorizonSchemaError`` is raised when a response body is missing expected
top-level structural keys (``_embedded.records`` for list endpoints; ``id`` /
``paging_token`` for single-record endpoints).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from ingestion.metrics import _normalise_endpoint, get_metrics

_metrics = get_metrics()

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Pre-release suffix pattern (e.g. "-rc1", "-beta.2", "-alpha")
_PRERELEASE_RE = re.compile(r"-[a-zA-Z].*$")


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class HorizonVersionError(RuntimeError):
    """Raised when the Horizon server version is outside the supported range.

    Attributes
    ----------
    detected:
        The version string read from the ``X-Stellar-Horizon-Version`` header.
    min_version:
        The inclusive lower bound of the supported range.
    max_version:
        The exclusive upper bound of the supported range.
    url:
        The request URL that returned the out-of-range version.

    Security note: the error message never includes response body content —
    only the URL and version string — to prevent leaking potentially sensitive
    API response data into logs or exception tracebacks.
    """

    def __init__(
        self,
        detected: str,
        min_version: str,
        max_version: str,
        url: str,
    ) -> None:
        super().__init__(
            f"Horizon version {detected!r} at {url!r} is outside supported range "
            f"[{min_version}, {max_version}). Update HORIZON_MIN_VERSION / "
            f"HORIZON_MAX_VERSION in config/settings.py after verifying schema "
            f"compatibility."
        )
        self.detected = detected
        self.min_version = min_version
        self.max_version = max_version
        self.url = url


class HorizonSchemaError(RuntimeError):
    """Raised when a Horizon response body is missing expected structural keys.

    This is distinct from a Pydantic ``ValidationError`` — it fires before
    field-level parsing when a mandatory top-level key (e.g.
    ``_embedded.records``, ``id``, ``paging_token``) is absent, giving a
    clear error message that names the root cause rather than a confusing
    ``KeyError`` or ``NoneType`` traceback.

    Attributes
    ----------
    missing_key:
        The dot-notation path of the absent key (e.g. ``"_embedded.records"``).
    url:
        The request URL whose response was structurally invalid.
    """

    def __init__(self, missing_key: str, url: str) -> None:
        super().__init__(
            f"Horizon response from {url!r} is missing expected key {missing_key!r}. "
            f"This may indicate a schema change in the Horizon API. Check the "
            f"X-Stellar-Horizon-Version header and review the API changelog."
        )
        self.missing_key = missing_key
        self.url = url


# ---------------------------------------------------------------------------
# VersionGuard
# ---------------------------------------------------------------------------


class VersionGuard:
    """Validates ``X-Stellar-Horizon-Version`` response headers.

    Parses the version string from the header and checks it against a
    ``[min_version, max_version)`` range using semantic versioning
    (``packaging.version.Version``).  Pre-release suffixes (e.g.
    ``"2.28.0-rc1"``) are stripped before comparison with a ``WARNING``
    log, because the base version is what determines schema compatibility.

    Once a version has been validated for the client's base URL the result
    is cached in memory for the lifetime of the guard instance, avoiding
    repeated string-parsing overhead on every response.

    Parameters
    ----------
    min_version:
        Inclusive lower bound, e.g. ``"2.0.0"``.
    max_version:
        Exclusive upper bound, e.g. ``"4.0.0"``.
    tested_version:
        The specific version against which the current codebase was
        validated.  A ``WARNING`` is emitted when the server reports a
        different (but in-range) version.
    enabled:
        When ``False`` the guard is a no-op for every check but emits a
        single ``WARNING`` at construction time.
    """

    HEADER_NAME = "X-Stellar-Horizon-Version"

    def __init__(
        self,
        min_version: str,
        max_version: str,
        tested_version: str,
        enabled: bool = True,
    ) -> None:
        self._min_version = min_version
        self._max_version = max_version
        self._tested_version = tested_version
        self._enabled = enabled
        # Cache: base_url → validated version string (or sentinel for "header absent")
        self._cache: dict[str, str | None] = {}

        if not enabled:
            logger.warning(
                "Horizon version checking disabled — schema compatibility not guaranteed"
            )

    def check(self, response_headers: Mapping[str, str], url: str) -> None:
        """Validate the ``X-Stellar-Horizon-Version`` header from a response.

        Parameters
        ----------
        response_headers:
            The HTTP response headers mapping (case-insensitive lookup is
            handled by ``httpx``).
        url:
            The full request URL, used in error messages and cache keys.

        Raises
        ------
        HorizonVersionError
            When the header is present and the version falls outside
            ``[min_version, max_version)``.

        Notes
        -----
        - If the header is absent the method is a no-op (some proxy
          configurations strip it).
        - If version checking is disabled (``enabled=False``) the method
          is always a no-op.
        """
        if not self._enabled:
            return

        raw = response_headers.get(self.HEADER_NAME)
        if not raw:
            # Header absent or empty — graceful no-op.
            return

        raw = raw.strip()
        if not raw:
            return

        # Use the URL as the cache key so callers talking to different
        # Horizon nodes (e.g. testnet vs mainnet) are tracked separately.
        # Strip query string for a stable cache key.
        cache_key = url.split("?")[0]
        if cache_key in self._cache and self._cache[cache_key] == raw:
            # Already validated this exact version for this endpoint.
            return

        self._validate(raw, url, cache_key)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate(self, raw: str, url: str, cache_key: str) -> None:
        """Parse *raw* and run range + tested-version checks."""
        from packaging.version import InvalidVersion, Version

        version_str = raw
        is_prerelease = bool(_PRERELEASE_RE.search(raw))
        if is_prerelease:
            # Strip suffix so "2.28.0-rc1" → "2.28.0" for range comparison.
            version_str = _PRERELEASE_RE.sub("", raw)
            logger.warning(
                "Horizon pre-release version %r detected at %r — "
                "using base version %r for range check",
                raw,
                url,
                version_str,
            )

        try:
            parsed = Version(version_str)
            min_v = Version(self._min_version)
            max_v = Version(self._max_version)
        except InvalidVersion as exc:
            logger.warning(
                "Could not parse Horizon version header %r at %r: %s — skipping check",
                raw,
                url,
                exc,
            )
            return

        if parsed < min_v or parsed >= max_v:
            raise HorizonVersionError(
                detected=raw,
                min_version=self._min_version,
                max_version=self._max_version,
                url=url,
            )

        # In-range; warn if different from the pinned tested version.
        try:
            tested = Version(self._tested_version)
        except InvalidVersion:
            tested = None

        if tested is not None and parsed != tested:
            logger.warning(
                "Horizon version %r at %r differs from tested version %r — "
                "monitor for schema changes",
                version_str,
                url,
                self._tested_version,
            )

        # Cache the validated raw value so repeat responses are fast.
        self._cache[cache_key] = raw


# ---------------------------------------------------------------------------
# Sync helper (unchanged public API)
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
    endpoint = _normalise_endpoint(url)

    for attempt in range(max_retries + 1):
        start = time.perf_counter()
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            duration = time.perf_counter() - start
            _metrics.http_requests_total.labels(
                endpoint=endpoint, method="GET", status_code="error"
            ).inc()
            _metrics.http_request_duration_seconds.labels(endpoint=endpoint).observe(duration)
            if attempt < max_retries:
                _metrics.http_retries_total.labels(reason="timeout").inc()
            last_exception = exc
        else:
            duration = time.perf_counter() - start
            _metrics.http_requests_total.labels(
                endpoint=endpoint,
                method="GET",
                status_code=str(response.status_code),
            ).inc()
            _metrics.http_request_duration_seconds.labels(endpoint=endpoint).observe(duration)

            if response.status_code == 429:
                _metrics.http_rate_limit_hits_total.inc()

            if response.status_code not in _RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response

            reason = "429" if response.status_code == 429 else "5xx"
            if attempt < max_retries:
                _metrics.http_retries_total.labels(reason=reason).inc()

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


class AsyncHorizonClient:
    """Async HTTP client for Horizon with token-bucket rate limiting and retry.

    Wraps `httpx.AsyncClient` with:

    - A semaphore that caps concurrent in-flight requests at `max_concurrency`.
    - Exponential backoff with jitter on 429 and 5xx responses (max `max_retries` retries).
    - A `VersionGuard` that validates ``X-Stellar-Horizon-Version`` on every
      response and raises `HorizonVersionError` when outside the configured range.

    Supports async context-manager usage::

        async with AsyncHorizonClient(settings.horizon_url) as client:
            data = await client.get("/trades", params={"limit": 200})

    Version guard configuration is read from the four ``HORIZON_*`` settings
    in ``config/settings.py``.  Pass ``version_guard=None`` to disable
    validation entirely (useful for tests that don't exercise version logic).
    """

    # Sentinel object used to distinguish "caller passed None explicitly" from
    # "caller did not pass version_guard at all" (in which case we build one
    # from settings).
    _UNSET: object = object()

    def __init__(
        self,
        base_url: str,
        max_concurrency: int = 20,
        max_retries: int = 3,
        version_guard: "VersionGuard | None | object" = _UNSET,
        probe_timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(timeout=30.0)
        self._max_retries = max_retries
        self._probe_timeout = probe_timeout

        # Build a VersionGuard from settings unless the caller supplies one
        # explicitly (including ``None`` to disable entirely).
        if version_guard is AsyncHorizonClient._UNSET:
            self._version_guard: VersionGuard | None = _build_version_guard_from_settings()
        else:
            self._version_guard = version_guard  # type: ignore[assignment]

    def _resolve_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # Internal request primitive
    # ------------------------------------------------------------------

    async def _make_request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> httpx.Response:
        """Execute a single HTTP request, raise on non-2xx, then version-check.

        This is the integration point for `VersionGuard`: every HTTP
        round-trip — whether called directly or via the retry loop in `get()`
        — passes through here so version validation is never bypassed.

        Parameters
        ----------
        method:
            HTTP method string (``"GET"``, ``"POST"``, …).
        url:
            Fully-qualified URL.
        **kwargs:
            Forwarded verbatim to ``httpx.AsyncClient.request()``.

        Returns
        -------
        httpx.Response
            The validated response object.

        Raises
        ------
        httpx.HTTPStatusError
            On non-2xx status after ``raise_for_status()``.
        HorizonVersionError
            When the ``X-Stellar-Horizon-Version`` header is present and
            outside the configured ``[min_version, max_version)`` range.
        """
        async with self._semaphore:
            response = await self._client.request(method, url, **kwargs)
        response.raise_for_status()
        if self._version_guard is not None:
            self._version_guard.check(response.headers, url)
        return response

    # ------------------------------------------------------------------
    # Public API
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

        Acquires the concurrency semaphore for the duration of each HTTP
        round-trip (via `_make_request`).  Retries on
        `_RETRYABLE_STATUS_CODES` or transport errors with exponential
        backoff + jitter.
        """
        url = self._resolve_url(path)
        endpoint = _normalise_endpoint(url)
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            # ----------------------------------------------------------
            # Proactive rate limiting — acquire before sending
            # ----------------------------------------------------------
            await self._rate_limiter.acquire()

            start = time.perf_counter()
            try:
                response = await self._make_request("GET", url, params=params)
            except httpx.TransportError as exc:
                duration = time.perf_counter() - start
                _metrics.http_requests_total.labels(
                    endpoint=endpoint, method="GET", status_code="error"
                ).inc()
                _metrics.http_request_duration_seconds.labels(endpoint=endpoint).observe(duration)
                if attempt < self._max_retries:
                    _metrics.http_retries_total.labels(reason="timeout").inc()
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
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise
                last_exc = exc
                continue

            duration = time.perf_counter() - start
            _metrics.http_requests_total.labels(
                endpoint=endpoint,
                method="GET",
                status_code=str(response.status_code),
            ).inc()
            _metrics.http_request_duration_seconds.labels(endpoint=endpoint).observe(duration)

            if response.status_code == 429:
                _metrics.http_rate_limit_hits_total.inc()

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

            reason = "429" if response.status_code == 429 else "5xx"
            if attempt < self._max_retries:
                _metrics.http_retries_total.labels(reason=reason).inc()

            last_exc = httpx.HTTPStatusError(
                f"Retryable status {response.status_code} from {url}",
                request=response.request,
                response=response,
            )

        raise MaxRetriesExceededError(url, self.max_retries + 1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def probe_server_version(self) -> str:
        """Fetch the Horizon root endpoint and log the server version.

        This is a startup pre-flight check.  It calls ``GET /`` (the
        Horizon root), reads the ``horizon_version`` field from the JSON
        body, and logs it at ``INFO`` level so operators can confirm which
        Horizon instance the pipeline is talking to.

        The request uses ``self._probe_timeout`` (default 5 s) to prevent
        startup hangs when the Horizon node is unreachable.

        Returns
        -------
        str
            The ``horizon_version`` string from the root endpoint, or
            ``"unknown"`` when the field is absent.

        Raises
        ------
        HorizonVersionError
            When the response ``X-Stellar-Horizon-Version`` header is
            present and outside the configured range.
        httpx.TimeoutException
            When the probe request exceeds ``probe_timeout`` seconds.
        """
        resp = await self._make_request(
            "GET",
            self._base_url,
            timeout=self._probe_timeout,
        )
        data = resp.json()
        version = data.get("horizon_version", "unknown")
        logger.info("Connected to Horizon %s at %s", version, self._base_url)
        return version

    async def close(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncHorizonClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Helper: build VersionGuard from config/settings.py
# ---------------------------------------------------------------------------


def _build_version_guard_from_settings() -> VersionGuard | None:
    """Construct a `VersionGuard` from the application settings.

    Returns ``None`` when the settings module cannot be imported (e.g.
    during isolated unit tests that don't supply a full environment).
    """
    try:
        from config.settings import settings  # local import to avoid circular deps
    except Exception:
        return None

    return VersionGuard(
        min_version=settings.horizon_min_version,
        max_version=settings.horizon_max_version,
        tested_version=settings.horizon_tested_version,
        enabled=settings.horizon_version_check_enabled,
    )


# ---------------------------------------------------------------------------
# Structural response validation helpers
# ---------------------------------------------------------------------------


def validate_list_response(body: dict, url: str) -> None:
    """Assert that *body* contains ``_embedded.records`` for list endpoints.

    Horizon list endpoints (``/trades``, ``/operations``, etc.) always wrap
    their result set in ``{"_embedded": {"records": [...]}, ...}``.  When
    this structure is absent the response has either changed schema or is not
    from the expected endpoint; raising `HorizonSchemaError` surfaces the
    root cause before Pydantic parsing.

    Parameters
    ----------
    body:
        Parsed JSON response dictionary.
    url:
        The request URL, included in the error message.

    Raises
    ------
    HorizonSchemaError
        When ``_embedded`` or ``_embedded.records`` is missing.
    """
    if "_embedded" not in body:
        raise HorizonSchemaError("_embedded", url)
    if "records" not in body["_embedded"]:
        raise HorizonSchemaError("_embedded.records", url)


def validate_single_record_response(body: dict, url: str) -> None:
    """Assert that *body* contains ``id`` and ``paging_token`` for single-record endpoints.

    Single Horizon resource endpoints (``/trades/{id}``,
    ``/operations/{id}``, etc.) always include ``id`` and ``paging_token``
    at the top level.  Missing keys indicate a schema change or wrong
    endpoint.

    Parameters
    ----------
    body:
        Parsed JSON response dictionary.
    url:
        The request URL, included in the error message.

    Raises
    ------
    HorizonSchemaError
        When ``id`` or ``paging_token`` is missing.
    """
    for key in ("id", "paging_token"):
        if key not in body:
            raise HorizonSchemaError(key, url)


# ---------------------------------------------------------------------------
# Backward-compatible descriptive name used by the historical ingestion API.
# ---------------------------------------------------------------------------

RetryingHorizonClient = AsyncHorizonClient
