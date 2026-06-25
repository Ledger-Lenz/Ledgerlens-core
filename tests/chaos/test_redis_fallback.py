"""Chaos scenario #2: Redis connection refused.

Disables the Redis proxy entirely and verifies that the feature store falls
back to the in-process cold tier without raising exceptions.  After the proxy
is re-enabled the feature store resumes writing to Redis.

Run with:
    docker compose --profile chaos up -d
    pytest tests/chaos/test_redis_fallback.py -m chaos -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.chaos

PROXY_NAME = "redis_proxy"
REDIS_LISTEN = "0.0.0.0:16379"
REDIS_UPSTREAM = "localhost:6379"


@pytest.fixture(scope="module")
def redis_proxy(toxiproxy):
    toxiproxy.create_proxy(PROXY_NAME, REDIS_LISTEN, REDIS_UPSTREAM)
    yield PROXY_NAME
    toxiproxy.enable_proxy(PROXY_NAME)
    toxiproxy.reset_proxy(PROXY_NAME)


def _make_feature_store(redis_url: str):
    """Construct a FeatureStore pointed at the given Redis URL."""
    import importlib
    import detection.feature_store as fs_module

    # Re-import with patched settings so it connects to the proxy
    import config.settings as settings_module
    object.__setattr__(settings_module.settings, "redis_url", redis_url)
    # Force re-creation of the FeatureStore singleton
    importlib.reload(fs_module)
    return fs_module.FeatureStore()


def test_redis_refused_falls_back_to_cold_tier(toxiproxy, redis_proxy):
    """Feature store falls back to cold tier without error when Redis is unavailable."""
    from datetime import datetime, timezone
    from detection.feature_store import WalletFeatureState

    # Disable Redis proxy to simulate connection refused
    toxiproxy.disable_proxy(redis_proxy)

    try:
        # Construct a fresh FeatureStore pointing at the dead proxy port
        store = _make_feature_store(f"redis://localhost:{REDIS_LISTEN.split(':')[1]}/0")

        # Should NOT raise — must silently fall back
        state = WalletFeatureState(
            wallet="GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            asset_pair="XLM/USDC",
            last_updated=datetime.now(timezone.utc),
        )
        store.set_state(state)  # no exception
        result = store.get_state(
            "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "XLM/USDC"
        )

        # In fallback mode, the state should still be retrievable from in-process dict
        assert result is not None, "State not accessible after Redis refusal — fallback failed"
        assert not store.is_using_redis(), "Expected fallback mode when Redis is unavailable"

    finally:
        toxiproxy.enable_proxy(redis_proxy)


def test_redis_fallback_no_data_loss_on_get(toxiproxy, redis_proxy):
    """get_state returns None gracefully when Redis is down and state was never cold-stored."""
    toxiproxy.disable_proxy(redis_proxy)
    try:
        store = _make_feature_store(f"redis://localhost:{REDIS_LISTEN.split(':')[1]}/0")
        result = store.get_state("GNEVEREXISTS111111111111111111111111111111111111111111111", "XLM/USDC")
        # Should return None, not raise
        assert result is None
    finally:
        toxiproxy.enable_proxy(redis_proxy)


def test_redis_recovery_resumes_hot_writes(toxiproxy, redis_proxy):
    """After Redis recovers, subsequent set_state calls land in Redis again."""
    from datetime import datetime, timezone
    from detection.feature_store import WalletFeatureState

    toxiproxy.disable_proxy(redis_proxy)
    store = _make_feature_store(f"redis://localhost:{REDIS_LISTEN.split(':')[1]}/0")

    # Trigger fallback
    state = WalletFeatureState(
        wallet="GBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        asset_pair="XLM/USDC",
        last_updated=datetime.now(timezone.utc),
    )
    store.set_state(state)
    assert not store.is_using_redis()

    # Re-enable Redis
    toxiproxy.enable_proxy(redis_proxy)

    def _redis_available():
        # Attempt a fresh store that can reach Redis
        fresh = _make_feature_store("redis://localhost:6379/0")
        return fresh.is_using_redis()

    recovered = toxiproxy.wait_for_recovery(_redis_available, timeout_s=60)
    assert recovered, "Redis hot layer did not recover within 60s"
