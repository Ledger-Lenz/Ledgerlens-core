"""Tests for EVMProviderPool, EVMProvider, provider_score, and related classes.

Coverage:
- provider_score(): healthy, lagging, circuit-open providers
- EVMProviderPool.call(): primary success, failover, all-fail, circuit opening
- Circuit reset via health probe
- Block-lag alerts
- ProviderHealthProbe (_probe_loop) updating current_block
- Integration: mock two providers, primary 429 → secondary serves
- Edge cases: single provider, no-key URL, no providers for chain,
  concurrent calls during circuit transition
- Performance benchmark: 1,000 concurrent calls, 3-provider pool, 1 failing
- EVMProvider.__repr__ URL masking
- _mask_rpc_url and _validate_rpc_url security helpers
- EVMProviderPoolStats correctness
- Settings validator: valid / invalid EVM_PROVIDERS JSON
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from ingestion.evm_loader import (
    EVMProvider,
    EVMProviderPool,
    EVMProviderPoolExhaustedError,
    EVMRPCError,
    _mask_rpc_url,
    _validate_rpc_url,
    _validate_rpc_params,
    provider_score,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

def make_provider(
    chain_id: int = 1,
    name: str = "test",
    priority: int = 0,
    health_score: float = 1.0,
    current_block: int = 100,
    is_circuit_open: bool = False,
    consecutive_failures: int = 0,
    rpc_url: str = "https://eth.example.com/rpc",
) -> EVMProvider:
    p = EVMProvider(
        chain_id=chain_id,
        rpc_url=rpc_url,
        name=name,
        priority=priority,
        health_score=health_score,
        current_block=current_block,
        is_circuit_open=is_circuit_open,
        consecutive_failures=consecutive_failures,
    )
    return p


# ---------------------------------------------------------------------------
# _mask_rpc_url
# ---------------------------------------------------------------------------

class TestMaskRpcUrl:
    def test_masks_api_key_in_path(self):
        url = "https://mainnet.infura.io/v3/abc123SECRET"
        masked = _mask_rpc_url(url)
        assert "abc123SECRET" not in masked
        assert masked.startswith("https://mainnet.infura.io/v3/")
        assert "***" in masked

    def test_masks_alchemy_key(self):
        url = "https://eth-mainnet.alchemyapi.io/v2/supersecretkey"
        masked = _mask_rpc_url(url)
        assert "supersecretkey" not in masked
        assert "***" in masked

    def test_no_key_in_path_unchanged(self):
        url = "https://eth.llamarpc.com"
        # Short path — nothing after third slash to mask
        masked = _mask_rpc_url(url)
        assert "llamarpc" in masked

    def test_repr_uses_masked_url(self):
        p = make_provider(rpc_url="https://mainnet.infura.io/v3/SECRETKEY")
        r = repr(p)
        assert "SECRETKEY" not in r
        assert "***" in r


# ---------------------------------------------------------------------------
# _validate_rpc_url
# ---------------------------------------------------------------------------

class TestValidateRpcUrl:
    def test_https_passes(self):
        assert _validate_rpc_url("https://eth.llamarpc.com") == "https://eth.llamarpc.com"

    def test_http_raises(self):
        with pytest.raises(ValueError, match="https://"):
            _validate_rpc_url("http://eth.example.com/rpc")

    def test_no_scheme_raises(self):
        with pytest.raises(ValueError):
            _validate_rpc_url("eth.example.com/rpc")

    def test_http_url_error_does_not_leak_key(self):
        """The error message must not contain the raw URL with API key."""
        url = "http://mainnet.infura.io/v3/SECRETAPIKEY"
        with pytest.raises(ValueError) as exc_info:
            _validate_rpc_url(url)
        assert "SECRETAPIKEY" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# EVMProvider construction
# ---------------------------------------------------------------------------

class TestEVMProvider:
    def test_http_url_rejected_at_construction(self):
        with pytest.raises(ValueError, match="https://"):
            EVMProvider(chain_id=1, rpc_url="http://eth.example.com", name="bad")

    def test_valid_provider_constructed(self):
        p = make_provider()
        assert p.chain_id == 1
        assert p.health_score == 1.0
        assert not p.is_circuit_open

    def test_repr_masks_url(self):
        p = make_provider(rpc_url="https://mainnet.infura.io/v3/MYSECRET")
        assert "MYSECRET" not in repr(p)
        assert "infura" in repr(p)


# ---------------------------------------------------------------------------
# _validate_rpc_params
# ---------------------------------------------------------------------------

class TestValidateRpcParams:
    def test_valid_simple_list(self):
        assert _validate_rpc_params(["0x1", 100, True, None]) == ["0x1", 100, True, None]

    def test_valid_nested_dict(self):
        params = [{"fromBlock": "0x1", "toBlock": "0x2", "address": "0xABC"}]
        assert _validate_rpc_params(params) == params

    def test_not_a_list_raises(self):
        with pytest.raises(ValueError, match="must be a list"):
            _validate_rpc_params({"method": "eth_blockNumber"})  # type: ignore[arg-type]

    def test_nested_object_in_list_passes(self):
        _validate_rpc_params([[{"key": "val"}]])

    def test_too_deep_raises(self):
        deep = [[[["too", "deep"]]]]
        with pytest.raises(ValueError, match="nested too deeply"):
            _validate_rpc_params(deep)

    def test_dict_with_non_string_key_raises(self):
        with pytest.raises(ValueError, match="key must be str"):
            _validate_rpc_params([{1: "value"}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# provider_score
# ---------------------------------------------------------------------------

class TestProviderScore:
    def test_healthy_provider_full_score(self):
        p = make_provider(health_score=1.0, current_block=100, is_circuit_open=False)
        score = provider_score(p, reference_block=100)
        assert score == pytest.approx(1.0)

    def test_lagging_provider_penalised(self):
        # Provider is 5 blocks behind; penalty = 5 * 0.1 = 0.5
        p = make_provider(health_score=1.0, current_block=95, is_circuit_open=False)
        score = provider_score(p, reference_block=100)
        assert score == pytest.approx(0.5)

    def test_circuit_open_provider_scores_negative(self):
        p = make_provider(health_score=1.0, current_block=100, is_circuit_open=True)
        score = provider_score(p, reference_block=100)
        assert score == -1.0

    def test_degraded_health_score(self):
        p = make_provider(health_score=0.6, current_block=100, is_circuit_open=False)
        score = provider_score(p, reference_block=100)
        assert score == pytest.approx(0.6)

    def test_lagging_and_degraded(self):
        # health=0.8, lag=3 blocks → penalty=0.3 → score=0.5
        p = make_provider(health_score=0.8, current_block=97, is_circuit_open=False)
        score = provider_score(p, reference_block=100)
        assert score == pytest.approx(0.5)

    def test_provider_ahead_of_reference_no_penalty(self):
        # Provider block > reference: lag = max(0, ...) = 0
        p = make_provider(health_score=1.0, current_block=105, is_circuit_open=False)
        score = provider_score(p, reference_block=100)
        assert score == pytest.approx(1.0)

    def test_circuit_open_beats_nothing(self):
        good = make_provider(health_score=0.01, current_block=100)
        bad = make_provider(health_score=1.0, current_block=100, is_circuit_open=True)
        assert provider_score(good, 100) > provider_score(bad, 100)


# ---------------------------------------------------------------------------
# EVMProviderPool.call() — success / failover / exhaustion / circuit opening
# ---------------------------------------------------------------------------

class TestEVMProviderPoolCall:
    """Unit tests for the call() failover logic using mocked _rpc_call."""

    def _make_pool(self, providers, **kwargs):
        return EVMProviderPool(providers=providers, **kwargs)

    @pytest.mark.asyncio
    async def test_primary_success_no_failover(self):
        """Primary provider succeeds — secondary never called."""
        primary = make_provider(name="primary", priority=0)
        secondary = make_provider(name="secondary", priority=1)
        pool = self._make_pool([primary, secondary])

        async def mock_rpc(provider, method, params, timeout):
            if provider.name == "primary":
                return "0x64"  # block 100
            pytest.fail("Secondary should not be called")

        pool._rpc_call = mock_rpc
        result = await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert result == "0x64"
        assert primary.health_score > 1.0 - 1e-9  # bumped up
        assert primary.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_primary_fails_failover_to_secondary(self):
        """Primary raises EVMRPCError → secondary serves the request."""
        primary = make_provider(name="primary", priority=0)
        secondary = make_provider(name="secondary", priority=1)
        pool = self._make_pool([primary, secondary], circuit_breaker_threshold=5)

        async def mock_rpc(provider, method, params, timeout):
            if provider.name == "primary":
                raise EVMRPCError("primary", 1, method, {"code": -32005, "message": "rate limit"})
            return "0x65"

        pool._rpc_call = mock_rpc
        result = await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert result == "0x65"
        assert primary.consecutive_failures == 1
        assert primary.health_score < 1.0

    @pytest.mark.asyncio
    async def test_all_providers_fail_raises_exhausted(self):
        """All providers fail → EVMProviderPoolExhaustedError raised."""
        p1 = make_provider(name="p1", priority=0)
        p2 = make_provider(name="p2", priority=1)
        pool = self._make_pool([p1, p2], circuit_breaker_threshold=5)

        async def mock_rpc(provider, method, params, timeout):
            raise asyncio.TimeoutError()

        pool._rpc_call = mock_rpc
        with pytest.raises(EVMProviderPoolExhaustedError) as exc_info:
            await pool.call(chain_id=1, method="eth_blockNumber", params=[])

        err = exc_info.value
        assert err.chain_id == 1
        assert "p1" in err.provider_names
        assert "p2" in err.provider_names

    @pytest.mark.asyncio
    async def test_exhausted_error_does_not_contain_rpc_url(self):
        """EVMProviderPoolExhaustedError must not expose API keys."""
        p = make_provider(name="infura", rpc_url="https://mainnet.infura.io/v3/SECRETKEY")
        pool = self._make_pool([p])

        async def mock_rpc(provider, method, params, timeout):
            raise asyncio.TimeoutError()

        pool._rpc_call = mock_rpc
        with pytest.raises(EVMProviderPoolExhaustedError) as exc_info:
            await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert "SECRETKEY" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold_failures(self):
        """Circuit opens when consecutive_failures reaches threshold."""
        p = make_provider(name="flaky", priority=0)
        pool = self._make_pool([p], circuit_breaker_threshold=3)

        async def mock_rpc(provider, method, params, timeout):
            raise asyncio.TimeoutError()

        pool._rpc_call = mock_rpc
        # First 3 calls — each increments consecutive_failures
        for _ in range(3):
            with pytest.raises(EVMProviderPoolExhaustedError):
                await pool.call(chain_id=1, method="eth_blockNumber", params=[])

        assert p.is_circuit_open
        assert p.consecutive_failures >= 3

    @pytest.mark.asyncio
    async def test_circuit_open_provider_skipped(self):
        """Provider with open circuit is skipped; next healthy one serves."""
        broken = make_provider(name="broken", priority=0, is_circuit_open=True)
        healthy = make_provider(name="healthy", priority=1)
        pool = self._make_pool([broken, healthy])

        async def mock_rpc(provider, method, params, timeout):
            if provider.name == "broken":
                pytest.fail("Circuit-open provider must not be called")
            return "0x1"

        pool._rpc_call = mock_rpc
        result = await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert result == "0x1"

    @pytest.mark.asyncio
    async def test_health_score_bounded_at_1_0(self):
        """Repeated successes do not push health_score above 1.0."""
        p = make_provider(name="good", health_score=1.0)
        pool = self._make_pool([p])

        async def mock_rpc(provider, method, params, timeout):
            return "0x1"

        pool._rpc_call = mock_rpc
        for _ in range(20):
            await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert p.health_score <= 1.0

    @pytest.mark.asyncio
    async def test_health_score_bounded_at_0_0(self):
        """Repeated failures do not push health_score below 0.0."""
        p1 = make_provider(name="bad", health_score=0.1, priority=0)
        p2 = make_provider(name="ok", priority=1)
        pool = self._make_pool([p1, p2], circuit_breaker_threshold=100)

        call_count = {"bad": 0}

        async def mock_rpc(provider, method, params, timeout):
            if provider.name == "bad":
                call_count["bad"] += 1
                raise asyncio.TimeoutError()
            return "0x1"

        pool._rpc_call = mock_rpc
        for _ in range(10):
            await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert p1.health_score >= 0.0

    @pytest.mark.asyncio
    async def test_no_providers_for_chain_raises_exhausted(self):
        """Calling for a chain with no providers raises EVMProviderPoolExhaustedError."""
        p = make_provider(chain_id=137, name="polygon")  # chain 137
        pool = self._make_pool([p])

        with pytest.raises(EVMProviderPoolExhaustedError) as exc_info:
            await pool.call(chain_id=1, method="eth_blockNumber", params=[])  # chain 1 — no provider
        assert exc_info.value.chain_id == 1
        assert exc_info.value.provider_names == []

    @pytest.mark.asyncio
    async def test_request_count_incremented_on_each_attempt(self):
        """requests_total increments for every provider attempt, not just successes."""
        p1 = make_provider(name="p1", priority=0)
        p2 = make_provider(name="p2", priority=1)
        pool = self._make_pool([p1, p2])

        call_n = {"n": 0}

        async def mock_rpc(provider, method, params, timeout):
            call_n["n"] += 1
            if provider.name == "p1":
                raise asyncio.TimeoutError()
            return "0x1"

        pool._rpc_call = mock_rpc
        await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert pool._requests_total[("p1", 1)] == 1
        assert pool._requests_total[("p2", 1)] == 1

    @pytest.mark.asyncio
    async def test_invalid_params_rejected_before_rpc(self):
        """Malformed params raise ValueError before any network call."""
        p = make_provider()
        pool = self._make_pool([p])
        called = {"yes": False}

        async def mock_rpc(provider, method, params, timeout):
            called["yes"] = True
            return "0x1"

        pool._rpc_call = mock_rpc
        with pytest.raises(ValueError):
            await pool.call(chain_id=1, method="eth_blockNumber", params=None)  # type: ignore[arg-type]
        assert not called["yes"]


# ---------------------------------------------------------------------------
# Circuit reset via health probe
# ---------------------------------------------------------------------------

class TestCircuitReset:
    @pytest.mark.asyncio
    async def test_circuit_resets_after_successful_probe(self):
        """Provider with open circuit + consecutive_failures=0 has circuit cleared by probe."""
        p = make_provider(name="recovering", is_circuit_open=True, consecutive_failures=0)
        pool = EVMProviderPool(providers=[p], probe_interval_seconds=0.05)

        async def mock_rpc(provider, method, params, timeout):
            return "0x64"  # block 100

        pool._rpc_call = mock_rpc

        # Run one probe cycle directly (bypasses asyncio.sleep)
        # We call _probe_loop via task but stop it after 1 iteration
        await pool.start_health_probing()
        await asyncio.sleep(0.15)  # allow at least 2 probe ticks
        await pool.stop_health_probing()

        assert not p.is_circuit_open

    @pytest.mark.asyncio
    async def test_circuit_not_reset_while_failures_nonzero(self):
        """Open circuit with consecutive_failures > 0 is NOT cleared by probe."""
        p = make_provider(name="still-broken", is_circuit_open=True, consecutive_failures=5)
        pool = EVMProviderPool(providers=[p], probe_interval_seconds=0.05)

        async def mock_rpc(provider, method, params, timeout):
            return "0x64"

        pool._rpc_call = mock_rpc

        await pool.start_health_probing()
        await asyncio.sleep(0.15)
        await pool.stop_health_probing()

        # consecutive_failures still > 0, so circuit stays open
        assert p.is_circuit_open

    @pytest.mark.asyncio
    async def test_stop_health_probing_is_idempotent(self):
        """stop_health_probing() can be called multiple times without error."""
        pool = EVMProviderPool(providers=[make_provider()])
        await pool.stop_health_probing()  # no task running
        await pool.stop_health_probing()  # still fine

    @pytest.mark.asyncio
    async def test_start_health_probing_idempotent(self):
        """start_health_probing() called twice does not create two probe tasks."""
        p = make_provider()
        pool = EVMProviderPool(providers=[p], probe_interval_seconds=100.0)
        pool._rpc_call = AsyncMock(return_value="0x1")

        await pool.start_health_probing()
        task1 = pool._probe_task
        await pool.start_health_probing()  # second call
        task2 = pool._probe_task

        assert task1 is task2
        await pool.stop_health_probing()


# ---------------------------------------------------------------------------
# Block-lag alert
# ---------------------------------------------------------------------------

class TestBlockLagAlert:
    @pytest.mark.asyncio
    async def test_lag_alert_set_when_all_providers_behind(self):
        """All providers > max_block_lag behind reference → lag_alert_active for chain."""
        # reference block = 200, providers at 185 and 184 → both lag > 10
        p1 = make_provider(name="p1", current_block=185)
        p2 = make_provider(name="p2", current_block=184)
        pool = EVMProviderPool(providers=[p1, p2], max_block_lag=10, probe_interval_seconds=100.0)

        # Manually set the reference via probe-like update — just call _check_lag_alerts
        # after overriding current_block to simulate a scenario where reference=200
        # but all providers are at 185/184 (<= 200-10-1 = 189)
        # Reference = max(185, 184) = 185; 185-10=175 — NOT all behind yet.
        # To trigger: set both providers well behind a high reference.
        # Easiest: one provider saw block 200 but is now reset; we patch directly.
        p1.current_block = 185
        p2.current_block = 183
        # Inject a "ghost" provider with current_block=200 to establish reference,
        # but we only want to test the alert, so let's add a third provider that
        # has seen block 200 then check that p1 and p2 (both <190) are flagged.
        p_ref = make_provider(name="ref-only", current_block=200)
        pool._all_providers.append(p_ref)
        pool._requests_total[("ref-only", 1)] = 0
        pool._errors_total[("ref-only", 1)] = 0
        # Now mark p_ref's circuit open so it won't be used for calls
        p_ref.is_circuit_open = True

        pool._check_lag_alerts()
        assert 1 in pool._lag_alert_chains

    @pytest.mark.asyncio
    async def test_lag_alert_cleared_when_provider_catches_up(self):
        """Lag alert is cleared once a provider catches up."""
        p1 = make_provider(name="p1", current_block=185)
        p2 = make_provider(name="p2", current_block=183)
        p_ref = make_provider(name="ref", current_block=200, is_circuit_open=True)
        pool = EVMProviderPool(providers=[p1, p2, p_ref], max_block_lag=10)
        pool._lag_alert_chains.add(1)

        # p1 catches up
        p1.current_block = 199
        pool._check_lag_alerts()
        assert 1 not in pool._lag_alert_chains

    def test_lag_alert_not_set_when_reference_is_zero(self):
        """No alert when no probes have succeeded yet (current_block=0 everywhere)."""
        p1 = make_provider(name="p1", current_block=0)
        p2 = make_provider(name="p2", current_block=0)
        pool = EVMProviderPool(providers=[p1, p2], max_block_lag=10)
        pool._check_lag_alerts()
        assert 1 not in pool._lag_alert_chains

    def test_lag_alert_in_stats(self):
        """chains_with_lag_alert is reflected in pool.stats."""
        p = make_provider(name="p", current_block=0)
        pool = EVMProviderPool(providers=[p], max_block_lag=10)
        pool._lag_alert_chains.add(1)
        stats = pool.stats
        assert 1 in stats.chains_with_lag_alert


# ---------------------------------------------------------------------------
# ProviderHealthProbe (_probe_loop) — current_block updates
# ---------------------------------------------------------------------------

class TestProviderHealthProbe:
    @pytest.mark.asyncio
    async def test_probe_updates_current_block(self):
        """Health probe updates current_block and last_probe_at on success."""
        p = make_provider(name="p", current_block=0)
        pool = EVMProviderPool(providers=[p], probe_interval_seconds=0.05)

        block_counter = {"n": 100}

        async def mock_rpc(provider, method, params, timeout):
            block_counter["n"] += 1
            return hex(block_counter["n"])

        pool._rpc_call = mock_rpc
        await pool.start_health_probing()
        await asyncio.sleep(0.18)
        await pool.stop_health_probing()

        assert p.current_block > 100
        assert p.last_probe_at is not None

    @pytest.mark.asyncio
    async def test_probe_does_not_crash_on_exception(self):
        """Probe failure is swallowed; the loop continues."""
        p = make_provider(name="flaky")
        pool = EVMProviderPool(providers=[p], probe_interval_seconds=0.05)
        call_count = {"n": 0}

        async def mock_rpc(provider, method, params, timeout):
            call_count["n"] += 1
            raise asyncio.TimeoutError()

        pool._rpc_call = mock_rpc
        await pool.start_health_probing()
        await asyncio.sleep(0.18)
        await pool.stop_health_probing()
        # Loop ran multiple times without crashing
        assert call_count["n"] >= 2

    @pytest.mark.asyncio
    async def test_probe_updates_multiple_providers(self):
        """Each provider in the pool gets its block updated on each probe cycle."""
        p1 = make_provider(name="p1", current_block=0, chain_id=1)
        p2 = make_provider(name="p2", current_block=0, chain_id=137, rpc_url="https://polygon.example.com/rpc")
        pool = EVMProviderPool(providers=[p1, p2], probe_interval_seconds=0.05)

        async def mock_rpc(provider, method, params, timeout):
            if provider.name == "p1":
                return "0x64"   # 100
            return "0xc8"       # 200

        pool._rpc_call = mock_rpc
        await pool.start_health_probing()
        await asyncio.sleep(0.18)
        await pool.stop_health_probing()

        assert p1.current_block == 100
        assert p2.current_block == 200


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestIntegration:
    @pytest.mark.asyncio
    async def test_primary_429_falls_back_to_secondary(self):
        """Primary returns aiohttp.ClientError (simulating 429) → secondary serves."""
        import aiohttp

        primary = make_provider(name="primary", priority=0)
        secondary = make_provider(name="secondary", priority=1)
        pool = EVMProviderPool(providers=[primary, secondary], circuit_breaker_threshold=5)

        async def mock_rpc(provider, method, params, timeout):
            if provider.name == "primary":
                raise aiohttp.ClientError("HTTP 429 from provider 'primary'")
            return "0xABC"

        pool._rpc_call = mock_rpc
        result = await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert result == "0xABC"

        # primary was tried (1 request, 1 error)
        assert pool._requests_total[("primary", 1)] == 1
        assert pool._errors_total[("primary", 1)] == 1
        # secondary was tried (1 request, 0 errors)
        assert pool._requests_total[("secondary", 1)] == 1
        assert pool._errors_total[("secondary", 1)] == 0

    @pytest.mark.asyncio
    async def test_stats_reflect_accumulated_counts(self):
        """EVMProviderPoolStats.providers reflects accumulated request/error counts."""
        p1 = make_provider(name="p1", priority=0, current_block=100)
        p2 = make_provider(name="p2", priority=1, current_block=100)
        pool = EVMProviderPool(providers=[p1, p2])

        call_n = {"n": 0}

        async def mock_rpc(provider, method, params, timeout):
            call_n["n"] += 1
            # First call via p1 succeeds, next via p1 fails, then p2 serves
            if provider.name == "p1" and call_n["n"] <= 2:
                raise asyncio.TimeoutError()
            return "0x1"

        pool._rpc_call = mock_rpc
        # Call once — p1 fails, p2 serves
        await pool.call(chain_id=1, method="eth_blockNumber", params=[])

        stats = pool.stats
        p1_stats = next(s for s in stats.providers if s.provider_name == "p1")
        p2_stats = next(s for s in stats.providers if s.provider_name == "p2")

        assert p1_stats.requests_total == 1
        assert p1_stats.errors_total == 1
        assert p1_stats.error_rate == pytest.approx(1.0)
        assert p2_stats.requests_total == 1
        assert p2_stats.errors_total == 0
        assert p2_stats.error_rate == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_concurrent_calls_all_succeed(self):
        """Concurrent calls to pool with 3 providers complete without error."""
        providers = [
            make_provider(name=f"p{i}", priority=i, rpc_url=f"https://node{i}.example.com/rpc")
            for i in range(3)
        ]
        pool = EVMProviderPool(providers=providers, circuit_breaker_threshold=5)

        async def mock_rpc(provider, method, params, timeout):
            await asyncio.sleep(0.001)  # simulate small latency
            return "0x1"

        pool._rpc_call = mock_rpc
        tasks = [pool.call(chain_id=1, method="eth_blockNumber", params=[]) for _ in range(50)]
        results = await asyncio.gather(*tasks)
        assert all(r == "0x1" for r in results)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_single_provider_no_failover_available(self):
        """With only one provider, failure immediately raises EVMProviderPoolExhaustedError."""
        p = make_provider(name="only-one")
        pool = EVMProviderPool(providers=[p], circuit_breaker_threshold=5)

        async def mock_rpc(provider, method, params, timeout):
            raise asyncio.TimeoutError()

        pool._rpc_call = mock_rpc
        with pytest.raises(EVMProviderPoolExhaustedError) as exc_info:
            await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        assert exc_info.value.chain_id == 1
        assert "only-one" in exc_info.value.provider_names

    def test_provider_url_without_api_key_path(self):
        """Providers without API key path segments are accepted."""
        p = make_provider(rpc_url="https://eth.llamarpc.com")
        assert p.rpc_url == "https://eth.llamarpc.com"

    @pytest.mark.asyncio
    async def test_chain_with_no_configured_providers(self):
        """Requesting a chain not in the pool raises EVMProviderPoolExhaustedError immediately."""
        p = make_provider(chain_id=137, name="polygon-only", rpc_url="https://polygon.example.com/rpc")
        pool = EVMProviderPool(providers=[p])
        with pytest.raises(EVMProviderPoolExhaustedError) as exc_info:
            await pool.call(chain_id=8453, method="eth_blockNumber", params=[])  # Base
        assert exc_info.value.chain_id == 8453

    @pytest.mark.asyncio
    async def test_concurrent_circuit_open_close_no_crash(self):
        """Pool handles concurrent calls when circuit transitions during execution."""
        p1 = make_provider(name="p1", priority=0)
        p2 = make_provider(name="p2", priority=1)
        pool = EVMProviderPool(providers=[p1, p2], circuit_breaker_threshold=2)
        call_counts = {"p1": 0, "p2": 0}

        async def mock_rpc(provider, method, params, timeout):
            call_counts[provider.name] = call_counts.get(provider.name, 0) + 1
            if provider.name == "p1":
                raise asyncio.TimeoutError()
            await asyncio.sleep(0.001)
            return "0x1"

        pool._rpc_call = mock_rpc
        # Fire 10 concurrent calls; p1 fails every time, p2 serves
        tasks = [pool.call(chain_id=1, method="eth_blockNumber", params=[]) for _ in range(10)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # At least some should succeed via p2 (after p1's circuit opens)
        successes = [r for r in results if r == "0x1"]
        assert len(successes) > 0

    def test_get_best_provider_returns_highest_score(self):
        """get_best_provider() returns the non-circuit-open provider with best score."""
        best = make_provider(name="best", priority=0, health_score=1.0, current_block=100)
        worse = make_provider(name="worse", priority=1, health_score=0.5, current_block=90)
        pool = EVMProviderPool(providers=[best, worse])
        result = pool.get_best_provider(chain_id=1)
        assert result is not None
        assert result.name == "best"

    def test_get_best_provider_skips_circuit_open(self):
        """get_best_provider() skips open-circuit providers."""
        broken = make_provider(name="broken", priority=0, is_circuit_open=True)
        healthy = make_provider(name="healthy", priority=1)
        pool = EVMProviderPool(providers=[broken, healthy])
        result = pool.get_best_provider(chain_id=1)
        assert result is not None
        assert result.name == "healthy"

    def test_get_best_provider_returns_none_when_all_circuits_open(self):
        """get_best_provider() returns None when every provider has open circuit."""
        p = make_provider(name="broken", is_circuit_open=True)
        pool = EVMProviderPool(providers=[p])
        assert pool.get_best_provider(chain_id=1) is None


# ---------------------------------------------------------------------------
# EVMProviderPoolStats correctness
# ---------------------------------------------------------------------------

class TestEVMProviderPoolStats:
    def test_block_lag_computed_correctly(self):
        """block_lag = reference_block - current_block for each provider."""
        p1 = make_provider(name="p1", current_block=200)
        p2 = make_provider(name="p2", current_block=190)
        pool = EVMProviderPool(providers=[p1, p2])
        stats = pool.stats

        p1_stats = next(s for s in stats.providers if s.provider_name == "p1")
        p2_stats = next(s for s in stats.providers if s.provider_name == "p2")

        assert p1_stats.block_lag == 0      # reference is 200
        assert p2_stats.block_lag == 10     # 200 - 190

    def test_error_rate_zero_when_no_requests(self):
        """error_rate defaults to 0.0 when requests_total == 0."""
        p = make_provider(name="fresh")
        pool = EVMProviderPool(providers=[p])
        stats = pool.stats
        assert stats.providers[0].error_rate == 0.0

    def test_chains_with_lag_alert_empty_by_default(self):
        p = make_provider()
        pool = EVMProviderPool(providers=[p])
        assert pool.stats.chains_with_lag_alert == []

    def test_multi_chain_stats(self):
        """Stats correctly groups providers across multiple chains."""
        eth = make_provider(chain_id=1, name="eth-node", current_block=1000)
        poly = make_provider(chain_id=137, name="poly-node", current_block=5000,
                             rpc_url="https://polygon.example.com/rpc")
        pool = EVMProviderPool(providers=[eth, poly])
        stats = pool.stats
        chain_ids = {s.chain_id for s in stats.providers}
        assert chain_ids == {1, 137}


# ---------------------------------------------------------------------------
# Performance benchmark
# ---------------------------------------------------------------------------

class TestPerformanceBenchmark:
    @pytest.mark.asyncio
    async def test_1000_concurrent_calls_complete_under_10s(self):
        """1,000 concurrent RPC calls through a 3-provider pool (1 failing) < 10s."""
        providers = [
            make_provider(name="p0", priority=0, rpc_url="https://node0.example.com/rpc"),
            make_provider(name="p1", priority=1, rpc_url="https://node1.example.com/rpc"),
            make_provider(name="p2", priority=2, rpc_url="https://node2.example.com/rpc"),
        ]
        # p0 always fails; p1 always succeeds
        pool = EVMProviderPool(providers=providers, circuit_breaker_threshold=10)

        async def mock_rpc(provider, method, params, timeout):
            if provider.name == "p0":
                raise asyncio.TimeoutError()
            return "0x1"

        pool._rpc_call = mock_rpc
        start = time.monotonic()
        tasks = [pool.call(chain_id=1, method="eth_blockNumber", params=[]) for _ in range(1000)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.monotonic() - start

        successes = [r for r in results if r == "0x1"]
        assert len(successes) == 1000, f"Expected 1000 successes, got {len(successes)}"
        assert elapsed < 10.0, f"Benchmark took {elapsed:.2f}s (limit: 10s)"


# ---------------------------------------------------------------------------
# Settings validators for EVM_PROVIDERS
# ---------------------------------------------------------------------------

class TestSettingsEVMProviders:
    """Validate that config/settings.py correctly validates EVM_PROVIDERS."""

    def _make_settings(self, evm_providers: str, **overrides):
        """Construct a Settings instance with EVM_PROVIDERS overridden."""
        from config.settings import Settings
        env = {
            "evm_providers": evm_providers,
            # Provide enough defaults to pass other validators
            "evm_rpc_ethereum": "https://eth.llamarpc.com",
            "evm_rpc_base": "https://mainnet.base.org",
            "evm_rpc_polygon": "https://polygon-rpc.com",
            **overrides,
        }
        return Settings(**env)

    def test_empty_list_accepted(self):
        s = self._make_settings("[]")
        assert s.evm_providers == "[]"

    def test_valid_single_provider(self):
        providers = json.dumps([
            {"chain_id": 1, "rpc_url": "https://eth.llamarpc.com", "name": "llama", "priority": 0}
        ])
        s = self._make_settings(providers)
        assert s.evm_providers == providers

    def test_valid_multi_provider_multi_chain(self):
        providers = json.dumps([
            {"chain_id": 1, "rpc_url": "https://eth.llamarpc.com", "name": "eth-llama"},
            {"chain_id": 137, "rpc_url": "https://polygon-rpc.com", "name": "poly"},
        ])
        s = self._make_settings(providers)
        assert s.evm_providers == providers

    def test_http_url_rejected(self):
        from pydantic import ValidationError
        providers = json.dumps([
            {"chain_id": 1, "rpc_url": "http://eth.example.com/rpc", "name": "bad"}
        ])
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings(providers)

    def test_missing_rpc_url_rejected(self):
        from pydantic import ValidationError
        providers = json.dumps([{"chain_id": 1, "name": "missing-url"}])
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings(providers)

    def test_missing_name_rejected(self):
        from pydantic import ValidationError
        providers = json.dumps([{"chain_id": 1, "rpc_url": "https://eth.llamarpc.com"}])
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings(providers)

    def test_invalid_chain_id_rejected(self):
        from pydantic import ValidationError
        providers = json.dumps([
            {"chain_id": -1, "rpc_url": "https://eth.llamarpc.com", "name": "bad-chain"}
        ])
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings(providers)

    def test_invalid_json_rejected(self):
        from pydantic import ValidationError
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings("{not valid json}")

    def test_not_a_list_rejected(self):
        from pydantic import ValidationError
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings('{"chain_id": 1}')

    def test_negative_max_requests_rejected(self):
        from pydantic import ValidationError
        providers = json.dumps([
            {"chain_id": 1, "rpc_url": "https://eth.llamarpc.com",
             "name": "bad-rate", "max_requests_per_second": -5}
        ])
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings(providers)

    def test_optional_fields_accepted(self):
        providers = json.dumps([
            {"chain_id": 1, "rpc_url": "https://eth.llamarpc.com", "name": "full",
             "priority": 2, "max_requests_per_second": 5.0}
        ])
        s = self._make_settings(providers)
        assert s.evm_providers == providers

    def test_evm_max_block_lag_must_be_positive(self):
        from pydantic import ValidationError
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings("[]", evm_max_block_lag=0)

    def test_evm_probe_interval_must_be_positive(self):
        from pydantic import ValidationError
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings("[]", evm_probe_interval_seconds=0.0)

    def test_evm_circuit_breaker_threshold_must_be_positive(self):
        from pydantic import ValidationError
        with pytest.raises((ValidationError, ValueError)):
            self._make_settings("[]", evm_circuit_breaker_threshold=0)
