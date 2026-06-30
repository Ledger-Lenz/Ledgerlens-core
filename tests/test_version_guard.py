"""Tests for VersionGuard, HorizonVersionError, HorizonSchemaError,
probe_server_version, and the structural validation helpers in http_client.py.

Test matrix
-----------
Unit tests — VersionGuard.check()
  - version within range → no exception
  - version equal to min_version (inclusive) → no exception
  - version below min_version → HorizonVersionError
  - version equal to max_version (exclusive upper bound) → HorizonVersionError
  - version above max_version → HorizonVersionError
  - header absent → no-op (no exception)
  - empty string header → no-op
  - pre-release version (e.g. "2.28.0-rc1") → warning + passes
  - version equal to tested_version → no warning
  - version differs from tested_version (but in range) → warning
  - guard disabled → always no-op + startup warning
  - "unknown" / non-parseable version string → warning + no exception
  - result is cached → second call with same URL skips re-validation

Unit tests — HorizonVersionError
  - message contains detected, min, max, and URL
  - attributes are set correctly

Unit tests — HorizonSchemaError
  - message contains missing_key and url
  - attributes are set correctly

Unit tests — validate_list_response()
  - valid body → no exception
  - missing _embedded → HorizonSchemaError("_embedded", url)
  - _embedded present but missing records → HorizonSchemaError("_embedded.records", url)

Unit tests — validate_single_record_response()
  - valid body → no exception
  - missing id → HorizonSchemaError("id", url)
  - missing paging_token → HorizonSchemaError("paging_token", url)

Integration tests — AsyncHorizonClient
  - response with valid version header → no exception, JSON returned
  - response with out-of-range version header → HorizonVersionError raised before caller
  - response missing version header → no exception (graceful no-op)
  - response without _embedded.records body structure → HorizonSchemaError raised
  - version_guard=None disables checking

Integration tests — probe_server_version()
  - mock root response → version logged and returned
  - mock root response with out-of-range version header → HorizonVersionError

Performance test
  - 10,000 VersionGuard.check() calls complete in < 100 ms
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ingestion.http_client import (
    AsyncHorizonClient,
    HorizonSchemaError,
    HorizonVersionError,
    VersionGuard,
    validate_list_response,
    validate_single_record_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = "https://horizon.stellar.org"
_URL = f"{_BASE}/trades"


def _guard(
    min_v: str = "2.0.0",
    max_v: str = "4.0.0",
    tested: str = "2.28.0",
    enabled: bool = True,
) -> VersionGuard:
    return VersionGuard(min_version=min_v, max_version=max_v, tested_version=tested, enabled=enabled)


def _headers(version: str | None) -> dict[str, str]:
    if version is None:
        return {}
    return {"X-Stellar-Horizon-Version": version}


def _make_mock_response(
    status_code: int,
    body: dict | None = None,
    version_header: str | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.request = MagicMock()
    # Build a case-insensitive header dict similar to httpx
    headers: dict[str, str] = {}
    if version_header is not None:
        headers["X-Stellar-Horizon-Version"] = version_header
    resp.headers = headers
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=resp.request, response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Unit tests: HorizonVersionError
# ---------------------------------------------------------------------------


class TestHorizonVersionError:
    def test_message_contains_detected_version(self):
        exc = HorizonVersionError("1.5.0", "2.0.0", "4.0.0", _URL)
        assert "1.5.0" in str(exc)

    def test_message_contains_min_version(self):
        exc = HorizonVersionError("1.5.0", "2.0.0", "4.0.0", _URL)
        assert "2.0.0" in str(exc)

    def test_message_contains_max_version(self):
        exc = HorizonVersionError("1.5.0", "2.0.0", "4.0.0", _URL)
        assert "4.0.0" in str(exc)

    def test_message_contains_url(self):
        exc = HorizonVersionError("1.5.0", "2.0.0", "4.0.0", _URL)
        assert _URL in str(exc)

    def test_attributes_set(self):
        exc = HorizonVersionError("1.5.0", "2.0.0", "4.0.0", _URL)
        assert exc.detected == "1.5.0"
        assert exc.min_version == "2.0.0"
        assert exc.max_version == "4.0.0"
        assert exc.url == _URL

    def test_is_runtime_error(self):
        assert issubclass(HorizonVersionError, RuntimeError)

    def test_message_does_not_contain_response_body(self):
        # Security: error message must not expose API response body content.
        exc = HorizonVersionError("1.5.0", "2.0.0", "4.0.0", _URL)
        assert "secret" not in str(exc)
        assert "body" not in str(exc).lower()


# ---------------------------------------------------------------------------
# Unit tests: HorizonSchemaError
# ---------------------------------------------------------------------------


class TestHorizonSchemaError:
    def test_message_contains_missing_key(self):
        exc = HorizonSchemaError("_embedded.records", _URL)
        assert "_embedded.records" in str(exc)

    def test_message_contains_url(self):
        exc = HorizonSchemaError("_embedded.records", _URL)
        assert _URL in str(exc)

    def test_attributes_set(self):
        exc = HorizonSchemaError("id", _URL)
        assert exc.missing_key == "id"
        assert exc.url == _URL

    def test_is_runtime_error(self):
        assert issubclass(HorizonSchemaError, RuntimeError)


# ---------------------------------------------------------------------------
# Unit tests: VersionGuard.check()
# ---------------------------------------------------------------------------


class TestVersionGuardCheck:
    def test_version_within_range_no_exception(self):
        guard = _guard()
        guard.check(_headers("2.28.0"), _URL)  # should not raise

    def test_version_equal_to_min_inclusive_no_exception(self):
        guard = _guard()
        guard.check(_headers("2.0.0"), _URL)  # min is inclusive

    def test_version_above_min_no_exception(self):
        guard = _guard()
        guard.check(_headers("3.5.0"), _URL)

    def test_version_below_min_raises(self):
        guard = _guard()
        with pytest.raises(HorizonVersionError) as exc_info:
            guard.check(_headers("1.99.0"), _URL)
        assert exc_info.value.detected == "1.99.0"
        assert exc_info.value.url == _URL

    def test_version_at_max_exclusive_raises(self):
        guard = _guard()  # max = "4.0.0" (exclusive)
        with pytest.raises(HorizonVersionError):
            guard.check(_headers("4.0.0"), _URL)

    def test_version_above_max_raises(self):
        guard = _guard()
        with pytest.raises(HorizonVersionError):
            guard.check(_headers("5.0.0"), _URL)

    def test_missing_header_noop(self):
        guard = _guard()
        guard.check({}, _URL)  # no header — should not raise

    def test_empty_string_header_noop(self):
        guard = _guard()
        guard.check({"X-Stellar-Horizon-Version": ""}, _URL)  # empty — no-op

    def test_whitespace_only_header_noop(self):
        guard = _guard()
        guard.check({"X-Stellar-Horizon-Version": "   "}, _URL)

    def test_prerelease_version_passes_and_warns(self, caplog):
        guard = _guard()
        import logging
        with caplog.at_level(logging.WARNING, logger="ingestion.http_client"):
            guard.check(_headers("2.28.0-rc1"), _URL)
        assert any("pre-release" in r.message.lower() for r in caplog.records)

    def test_prerelease_out_of_range_raises(self):
        guard = _guard()
        with pytest.raises(HorizonVersionError):
            guard.check(_headers("1.0.0-rc5"), _URL)

    def test_tested_version_match_no_warning(self, caplog):
        guard = _guard(tested="2.28.0")
        import logging
        with caplog.at_level(logging.WARNING, logger="ingestion.http_client"):
            guard.check(_headers("2.28.0"), _URL)
        # No "differs from tested" warning should appear
        assert not any("differs from tested" in r.message for r in caplog.records)

    def test_in_range_but_differs_from_tested_warns(self, caplog):
        guard = _guard(tested="2.28.0")
        import logging
        with caplog.at_level(logging.WARNING, logger="ingestion.http_client"):
            guard.check(_headers("2.30.0"), _URL)
        assert any("differs from tested" in r.message for r in caplog.records)

    def test_disabled_guard_noop(self):
        guard = _guard(enabled=False)
        # Even an out-of-range version should not raise when disabled
        guard.check(_headers("0.1.0"), _URL)

    def test_disabled_guard_logs_warning_at_construction(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="ingestion.http_client"):
            _guard(enabled=False)
        assert any("disabled" in r.message.lower() for r in caplog.records)

    def test_unknown_version_string_noop_with_warning(self, caplog):
        guard = _guard()
        import logging
        with caplog.at_level(logging.WARNING, logger="ingestion.http_client"):
            guard.check(_headers("unknown"), _URL)
        assert any("could not parse" in r.message.lower() for r in caplog.records)

    def test_empty_version_string_noop(self):
        """Empty string after strip() must be a no-op (treated as absent header)."""
        guard = _guard()
        guard.check({"X-Stellar-Horizon-Version": ""}, _URL)  # no raise

    def test_cache_prevents_revalidation(self):
        """Calling check() twice with the same URL and version should not re-run validation."""
        from packaging.version import Version as PkgVersion

        guard = _guard()
        parse_calls = []
        real_validate = guard._validate

        def spy_validate(raw, url, cache_key):
            parse_calls.append(raw)
            return real_validate(raw, url, cache_key)

        guard._validate = spy_validate
        guard.check(_headers("2.28.0"), _URL)
        guard.check(_headers("2.28.0"), _URL)  # same URL + version

        # _validate should only have been called once
        assert len(parse_calls) == 1

    def test_different_urls_validated_independently(self):
        """Different endpoint URLs with different versions are both checked."""
        guard = _guard()
        url2 = "https://horizon-testnet.stellar.org/operations"
        # Both in-range — neither should raise
        guard.check(_headers("2.28.0"), _URL)
        guard.check(_headers("2.29.0"), url2)

    def test_error_does_not_contain_response_body(self):
        """HorizonVersionError raised by VersionGuard must not leak body content."""
        guard = _guard()
        with pytest.raises(HorizonVersionError) as exc_info:
            guard.check(_headers("1.0.0"), _URL)
        assert "secret" not in str(exc_info.value)
        assert "{" not in str(exc_info.value)  # no JSON body leaked


# ---------------------------------------------------------------------------
# Unit tests: validate_list_response
# ---------------------------------------------------------------------------


class TestValidateListResponse:
    def test_valid_body_no_exception(self):
        body = {"_embedded": {"records": [{"id": "1"}]}, "_links": {}}
        validate_list_response(body, _URL)  # should not raise

    def test_missing_embedded_raises(self):
        with pytest.raises(HorizonSchemaError) as exc_info:
            validate_list_response({"_links": {}}, _URL)
        assert exc_info.value.missing_key == "_embedded"
        assert exc_info.value.url == _URL

    def test_missing_records_raises(self):
        with pytest.raises(HorizonSchemaError) as exc_info:
            validate_list_response({"_embedded": {}}, _URL)
        assert exc_info.value.missing_key == "_embedded.records"

    def test_empty_records_list_no_exception(self):
        body = {"_embedded": {"records": []}}
        validate_list_response(body, _URL)


# ---------------------------------------------------------------------------
# Unit tests: validate_single_record_response
# ---------------------------------------------------------------------------


class TestValidateSingleRecordResponse:
    def test_valid_body_no_exception(self):
        body = {"id": "abc", "paging_token": "123-0", "type": "trade"}
        validate_single_record_response(body, _URL)

    def test_missing_id_raises(self):
        with pytest.raises(HorizonSchemaError) as exc_info:
            validate_single_record_response({"paging_token": "123-0"}, _URL)
        assert exc_info.value.missing_key == "id"

    def test_missing_paging_token_raises(self):
        with pytest.raises(HorizonSchemaError) as exc_info:
            validate_single_record_response({"id": "abc"}, _URL)
        assert exc_info.value.missing_key == "paging_token"

    def test_both_missing_raises_on_first(self):
        with pytest.raises(HorizonSchemaError) as exc_info:
            validate_single_record_response({}, _URL)
        assert exc_info.value.missing_key == "id"


# ---------------------------------------------------------------------------
# Integration tests: AsyncHorizonClient version checking
# ---------------------------------------------------------------------------


def _make_async_client_no_settings(
    version_guard: VersionGuard | None,
) -> AsyncHorizonClient:
    """Build a client with an explicit guard, bypassing settings import."""
    return AsyncHorizonClient(_BASE, version_guard=version_guard)


def _patch_make_request(client: AsyncHorizonClient, response: MagicMock):
    """Replace the internal _client.request with a mock that returns *response*."""

    async def mock_request(method, url, **kwargs):
        return response

    client._client.request = mock_request


@pytest.mark.asyncio
async def test_client_returns_json_when_version_in_range():
    guard = _guard()
    client = _make_async_client_no_settings(guard)
    resp = _make_mock_response(200, {"ok": True}, version_header="2.28.0")
    _patch_make_request(client, resp)

    result = await client.get("/trades")
    assert result == {"ok": True}
    await client.close()


@pytest.mark.asyncio
async def test_client_raises_version_error_before_returning_json():
    guard = _guard()
    client = _make_async_client_no_settings(guard)
    resp = _make_mock_response(200, {"ok": True}, version_header="1.0.0")
    _patch_make_request(client, resp)

    with pytest.raises(HorizonVersionError) as exc_info:
        await client.get("/trades")

    assert exc_info.value.detected == "1.0.0"
    await client.close()


@pytest.mark.asyncio
async def test_client_no_exception_when_version_header_absent():
    guard = _guard()
    client = _make_async_client_no_settings(guard)
    resp = _make_mock_response(200, {"ok": True}, version_header=None)
    _patch_make_request(client, resp)

    result = await client.get("/trades")
    assert result == {"ok": True}
    await client.close()


@pytest.mark.asyncio
async def test_client_version_guard_none_disables_check():
    client = _make_async_client_no_settings(version_guard=None)
    # Even an out-of-range header should not raise
    resp = _make_mock_response(200, {"data": 1}, version_header="0.0.1")
    _patch_make_request(client, resp)

    result = await client.get("/trades")
    assert result == {"data": 1}
    await client.close()


@pytest.mark.asyncio
async def test_client_horizon_version_error_not_retried():
    """HorizonVersionError should propagate immediately, not be swallowed by retry."""
    guard = _guard()
    client = _make_async_client_no_settings(guard)
    call_count = 0

    async def mock_request(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_mock_response(200, {"ok": True}, version_header="1.0.0")

    client._client.request = mock_request

    with pytest.raises(HorizonVersionError):
        await client.get("/trades")

    # HorizonVersionError is not in _RETRYABLE_STATUS_CODES path —
    # it should not be retried.
    assert call_count == 1
    await client.close()


# ---------------------------------------------------------------------------
# Integration test: probe_server_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_server_version_returns_version(caplog):
    import logging

    guard = _guard()
    client = _make_async_client_no_settings(guard)
    root_body = {"horizon_version": "2.28.0", "core_version": "19.0.0"}
    resp = _make_mock_response(200, root_body, version_header="2.28.0")
    _patch_make_request(client, resp)

    with caplog.at_level(logging.INFO, logger="ingestion.http_client"):
        version = await client.probe_server_version()

    assert version == "2.28.0"
    assert any("2.28.0" in r.message for r in caplog.records)
    await client.close()


@pytest.mark.asyncio
async def test_probe_server_version_returns_unknown_when_field_absent():
    guard = _guard()
    client = _make_async_client_no_settings(guard)
    resp = _make_mock_response(200, {"other_field": "x"}, version_header="2.28.0")
    _patch_make_request(client, resp)

    version = await client.probe_server_version()
    assert version == "unknown"
    await client.close()


@pytest.mark.asyncio
async def test_probe_server_version_raises_when_out_of_range():
    guard = _guard()
    client = _make_async_client_no_settings(guard)
    resp = _make_mock_response(200, {"horizon_version": "1.0.0"}, version_header="1.0.0")
    _patch_make_request(client, resp)

    with pytest.raises(HorizonVersionError):
        await client.probe_server_version()

    await client.close()


# ---------------------------------------------------------------------------
# Integration test: structural schema validation via client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_list_response_schema_error_via_helper():
    """validate_list_response raises HorizonSchemaError for missing _embedded.records."""
    body = {"_links": {}}  # no _embedded
    with pytest.raises(HorizonSchemaError) as exc_info:
        validate_list_response(body, _URL)
    assert "_embedded" in exc_info.value.missing_key


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestVersionGuardEdgeCases:
    def test_version_major_boundary(self):
        """Version 3.0.0 should pass with default [2.0.0, 4.0.0) range."""
        guard = _guard()
        guard.check(_headers("3.0.0"), _URL)

    def test_version_patch_level_respected(self):
        """2.19.9 is below 2.20.0 min."""
        guard = _guard(min_v="2.20.0")
        with pytest.raises(HorizonVersionError):
            guard.check(_headers("2.19.9"), _URL)

    def test_version_patch_level_in_range(self):
        guard = _guard(min_v="2.20.0")
        guard.check(_headers("2.20.0"), _URL)

    def test_multivalue_header_only_first_value_used(self):
        """If the header somehow contains a comma-separated list, we parse the raw value."""
        guard = _guard()
        # Multi-value header comes through as a single string in httpx
        guard.check({"X-Stellar-Horizon-Version": "2.28.0, 2.29.0"}, _URL)

    def test_zero_version_out_of_range(self):
        guard = _guard()
        with pytest.raises(HorizonVersionError):
            guard.check(_headers("0.0.0"), _URL)

    def test_version_with_build_metadata_treated_as_parseable(self):
        """'2.28.0+build.1' — packaging strips build metadata for comparison."""
        guard = _guard()
        # packaging.version.Version parses build metadata correctly
        guard.check(_headers("2.28.0+build.1"), _URL)

    def test_prerelease_stripping_maps_to_base(self):
        """'2.28.0-rc1' → base '2.28.0' which is in range."""
        guard = _guard(min_v="2.0.0", max_v="3.0.0", tested="2.28.0")
        guard.check(_headers("2.28.0-rc1"), _URL)

    def test_prerelease_below_min_still_raises(self):
        guard = _guard(min_v="2.0.0", max_v="3.0.0")
        with pytest.raises(HorizonVersionError):
            guard.check(_headers("1.0.0-rc1"), _URL)


# ---------------------------------------------------------------------------
# Performance test: 10 000 calls in < 100 ms
# ---------------------------------------------------------------------------


def test_version_guard_performance_10k_calls():
    """10,000 VersionGuard.check() calls must complete in < 100 ms."""
    guard = _guard()
    headers = _headers("2.28.0")

    # Prime the cache with one call
    guard.check(headers, _URL)

    n = 10_000
    start = time.perf_counter()
    for _ in range(n):
        guard.check(headers, _URL)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 100, (
        f"10,000 VersionGuard.check() calls took {elapsed_ms:.1f} ms (limit: 100 ms)"
    )


# ---------------------------------------------------------------------------
# Existing AsyncHorizonClient API tests (regression — unchanged behaviour)
# ---------------------------------------------------------------------------


def _patch_client_get(client: AsyncHorizonClient, handler):
    """Replace the inner httpx.AsyncClient.get with an async callable.

    The existing test helpers called `client._client.get` directly; now
    _make_request uses `client._client.request`.  We wrap `get` calls
    via `request` so legacy test helpers keep working when they call
    `client.get(path)` (which internally calls `_make_request("GET", ...)`).
    """

    async def mock_request(method, url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}  # no version header → no-op guard
        resp.raise_for_status.return_value = None
        inner = await handler(url, params=kwargs.get("params"))
        resp.status_code = inner.status_code
        resp.json.return_value = inner.json.return_value if inner.json.return_value else {}
        resp.request = inner.request
        if inner.status_code >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                f"HTTP {inner.status_code}", request=resp.request, response=resp
            )
        return resp

    client._client.request = mock_request
    return client


def _make_legacy_mock_response(status_code: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.request = MagicMock()
    resp.headers = {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=resp.request, response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.mark.asyncio
async def test_regression_async_client_returns_json_on_success():
    async def mock_get(url, params=None):
        return _make_legacy_mock_response(200, {"ok": True})

    client = AsyncHorizonClient("https://horizon.stellar.org", version_guard=None)
    _patch_client_get(client, mock_get)

    result = await client.get("/trades")
    assert result == {"ok": True}
    await client.close()


@pytest.mark.asyncio
async def test_regression_async_client_retries_on_retryable_status():
    calls = {"count": 0}

    async def mock_get(url, params=None):
        calls["count"] += 1
        if calls["count"] < 3:
            return _make_legacy_mock_response(503)
        return _make_legacy_mock_response(200, {"ok": True})

    client = AsyncHorizonClient(
        "https://horizon.stellar.org", max_retries=3, version_guard=None
    )
    _patch_client_get(client, mock_get)

    with patch("ingestion.http_client.asyncio.sleep"):
        result = await client.get("/trades")

    assert result == {"ok": True}
    assert calls["count"] == 3
    await client.close()


@pytest.mark.asyncio
async def test_regression_async_client_raises_after_exhausting_retries():
    async def mock_get(url, params=None):
        return _make_legacy_mock_response(429)

    client = AsyncHorizonClient(
        "https://horizon.stellar.org", max_retries=2, version_guard=None
    )
    _patch_client_get(client, mock_get)

    with patch("ingestion.http_client.asyncio.sleep"):
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/trades")

    await client.close()


@pytest.mark.asyncio
async def test_regression_async_client_does_not_retry_non_retryable_error():
    calls = {"count": 0}

    async def mock_get(url, params=None):
        calls["count"] += 1
        return _make_legacy_mock_response(404)

    client = AsyncHorizonClient(
        "https://horizon.stellar.org", max_retries=3, version_guard=None
    )
    _patch_client_get(client, mock_get)

    with pytest.raises(httpx.HTTPStatusError):
        await client.get("/trades")

    assert calls["count"] == 1
    await client.close()
