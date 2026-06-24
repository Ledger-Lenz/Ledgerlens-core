---
title: "Add Schema Version Header Validation to All Horizon API Responses"
labels: ["difficulty: advanced", "area: ingestion", "type: reliability"]
assignees: []
---

## Summary
The Stellar Horizon API occasionally introduces breaking changes between versions (e.g., field renames, type changes, new required fields), and LedgerLens currently has no mechanism to detect when the Horizon instance it is talking to has drifted from the schema version the models were written against. Adding schema version header validation will surface API version mismatches as explicit, actionable errors before they silently corrupt ingested data or cause cryptic Pydantic parse failures deep in the pipeline.

## Background & Context
`ingestion/http_client.py` provides `RetryingHorizonClient`, which wraps all HTTP calls to the Horizon API. Every Horizon response includes the header `X-Stellar-Horizon-Version` (e.g., `"2.28.0"`) which identifies the server software version. The Horizon API also returns `Content-Type: application/hal+json` with a `_links` envelope whose structure varies across major versions.

Currently, `http_client.py` ignores these headers entirely. If a Horizon node upgrades and changes a field name — for example, renaming `base_amount` to `base_amount_decimal`, or changing `ledger_close_time` from a string to a numeric timestamp — `data_models.py` will fail to deserialise records, but the error will appear as a `ValidationError` deep in `horizon_streamer.py` or `historical_loader.py`, with no indication that the root cause is an API version mismatch.

This issue also affects the cross-chain bridge loaders (`bridge_loader.py`, `evm_loader.py`): while they do not use Horizon, they call EVM JSON-RPC endpoints whose method signatures and return formats can change between client versions.

The solution is a `VersionGuard` middleware layer inside `RetryingHorizonClient` that:
1. Reads the `X-Stellar-Horizon-Version` header from each response
2. Validates it against a `REQUIRED_HORIZON_VERSION` range defined in `config/settings.py`
3. Raises a `HorizonVersionError` (a distinct exception class, not a generic `ValueError`) if the version is outside the acceptable range
4. Emits a structured log entry at `WARNING` level if the version is within range but differs from the tested version pinned in `settings.py`

## Objectives
- [ ] Implement a `VersionGuard` class in `ingestion/http_client.py` that parses the `X-Stellar-Horizon-Version` header using semantic versioning (`packaging.version.Version`) and compares it against a configurable `[min_version, max_version)` range.
- [ ] Integrate `VersionGuard` into `RetryingHorizonClient._make_request()` so every response is version-checked before being returned to callers.
- [ ] Define a `HorizonVersionError(RuntimeError)` exception class that includes the detected version, expected range, and the endpoint URL in its message.
- [ ] Add a `GET /.well-known/stellar.toml` pre-flight check on `RetryingHorizonClient.__init__` that fetches the Horizon server info endpoint and logs the version at `INFO` level on startup.

## Technical Requirements

**`VersionGuard` interface:**
```python
from packaging.version import Version

class VersionGuard:
    HEADER_NAME = "X-Stellar-Horizon-Version"

    def __init__(
        self,
        min_version: str,         # e.g. "2.20.0"
        max_version: str,         # e.g. "3.0.0" (exclusive)
        tested_version: str,      # e.g. "2.28.0" — emit WARNING if differs
    ): ...

    def check(self, response_headers: Mapping[str, str], url: str) -> None:
        """
        Parse X-Stellar-Horizon-Version from headers.
        Raise HorizonVersionError if outside [min, max).
        Warn if differs from tested_version.
        No-op if header is absent (some proxy configs strip it).
        """
```

**`HorizonVersionError`:**
```python
class HorizonVersionError(RuntimeError):
    def __init__(self, detected: str, min_version: str, max_version: str, url: str):
        super().__init__(
            f"Horizon version {detected!r} at {url!r} is outside supported range "
            f"[{min_version}, {max_version}). Update HORIZON_MIN_VERSION / "
            f"HORIZON_MAX_VERSION in config/settings.py after verifying schema compatibility."
        )
        self.detected = detected
        self.min_version = min_version
        self.max_version = max_version
        self.url = url
```

**Integration in `RetryingHorizonClient`:**
```python
async def _make_request(self, method: str, url: str, **kwargs) -> httpx.Response:
    response = await self._client.request(method, url, **kwargs)
    response.raise_for_status()
    self._version_guard.check(response.headers, url)  # ← new
    return response
```

**Startup pre-flight check:**
```python
async def probe_server_version(self) -> str:
    """
    Call GET /  (Horizon root) to retrieve server version.
    Logs: INFO "Connected to Horizon {version} at {base_url}"
    Returns the version string.
    Raises HorizonVersionError if out of range.
    """
    resp = await self._make_request("GET", self.base_url)
    data = resp.json()
    version = data.get("horizon_version", "unknown")
    logger.info("Connected to Horizon %s at %s", version, self.base_url)
    return version
```

**Semantic version parsing**: use `packaging.version.Version` (already a transitive dependency via `pip`). Handle pre-release versions (e.g., `"2.28.0-rc1"`) by stripping the pre-release suffix before comparison, with a `WARNING` log.

**Configuration** (add to `config/settings.py`):
- `HORIZON_MIN_VERSION`: default `"2.0.0"`
- `HORIZON_MAX_VERSION`: default `"4.0.0"` (exclusive upper bound)
- `HORIZON_TESTED_VERSION`: default `"2.28.0"` (the version against which models were validated)
- `HORIZON_VERSION_CHECK_ENABLED`: default `True` — set to `False` to disable for private/custom Horizon nodes that omit the header

**Response header caching**: once a version has been validated for a given `base_url`, cache the result in memory for the lifetime of the `RetryingHorizonClient` instance to avoid re-parsing the header on every response (which adds string parsing overhead to every call).

**Partial-response validation**: in addition to version headers, validate that each Horizon response body contains the expected top-level `_embedded.records` key (for list endpoints) or `id`/`paging_token` keys (for single-record endpoints) before passing to Pydantic. If these structural keys are absent, raise `HorizonSchemaError` (separate exception class) rather than letting Pydantic emit a confusing `KeyError`.

## Security Considerations
- `HorizonVersionError` messages must not include response body content — only the URL and version string — to prevent leaking potentially sensitive API response data into logs or exception tracebacks.
- The `HORIZON_VERSION_CHECK_ENABLED=False` escape hatch must emit a `WARNING` log at startup: `"Horizon version checking disabled — schema compatibility not guaranteed"`. This prevents operators from silently disabling the check in production without awareness.
- Horizon base URLs must be validated against `http://` or `https://` schemes and must not include `file://` or other non-HTTP schemes (SSRF mitigation at the configuration layer).
- The pre-flight probe must have a configurable timeout (default: 5 seconds) to prevent startup hangs if the Horizon node is unreachable.

## Testing Requirements
- Unit tests covering `VersionGuard.check()`: version within range (no exception), version below min (raises `HorizonVersionError`), version at or above max (raises), missing header (no-op), pre-release version (warning + passes)
- Unit tests covering `HorizonVersionError`: message contains detected version, min, max, and URL
- Unit tests covering `probe_server_version()`: mock Horizon root response; assert version logged and returned
- Integration tests: mock `RetryingHorizonClient` HTTP layer returning a response with `X-Stellar-Horizon-Version: 1.0.0`; assert `HorizonVersionError` is raised before the response reaches the caller
- Integration tests: mock response missing the header; assert no exception (graceful no-op)
- Integration tests: structural key validation — mock response body without `_embedded.records`; assert `HorizonSchemaError` is raised
- Edge cases: version string `"unknown"` (non-parseable), version `"2.28.0-rc1"` (pre-release), empty string header, multi-valued header
- Performance benchmark: 10,000 header checks should complete in < 100 ms (version guard must not be a hot-path bottleneck)

## Documentation Requirements
- Update `config/settings.py` with comments explaining `HORIZON_MIN_VERSION` / `HORIZON_MAX_VERSION` and how to update them after a Horizon upgrade
- Add docstrings to `VersionGuard`, `HorizonVersionError`, `HorizonSchemaError`, and `probe_server_version`
- Update `docs/ingestion.md` with a section on API version management: how to check the Horizon version in use, how to update the tested version range, and what to do when a `HorizonVersionError` is raised in production
- Add `packaging` to `requirements.txt` if not already present

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: HTTP client middleware, semantic versioning (`packaging` library), Stellar Horizon API versioning, exception design patterns
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python backend engineer with experience building robust HTTP client middleware and API version management. Familiarity with Horizon's response envelope format and semantic versioning conventions is expected. Experience with defensive API client patterns (pre-flight checks, structural validation) is a strong plus.
