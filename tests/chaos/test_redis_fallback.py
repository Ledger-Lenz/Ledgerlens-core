"""Chaos scenario #2: Redis connection refused.

Disables the Redis proxy entirely and verifies that the feature store falls
back to the in-process cold tier without raising exceptions.  After the proxy
is re-enabled the feature store resumes writing to Redis.

Run with::

    docker compose --profile chaos up -d
    pytest tests/chaos/test_redis_fallback.py -m chaos -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.chaos

PROXY_NAME = "redis_proxy"
REDIS_LISTEN = "0.0.0.0:16379"
REDIS_UPSTREAM = "localhost:6379"

# Port derived once; avoids repeated string-split across tests.
_REDIS_PROXY_PORT = int(REDIS_LISTEN.split(":")[1])
_REDIS_PROXY_URL = f"redis://localhost:{_REDIS_PROXY_PORT}/0"
_REDIS_DIRECT_URL = "redis://localhost:6379/0"


def _make_store(redis_url: str):
    """Return a fresh ``FeatureStore`` pointed at *redis_url*.

    Constructs the store directly via its public constructor — no module
    reloading or global-settings mutation.
    """
    from detection.feature_store import FeatureStore

    return FeatureStore(redis_url=redis_url)


@pytest.fixture(scope="module")
def redis_proxy(toxiproxy):
    """Create the Redis proxy for this module and clean up on teardown."""
    toxiproxy.create_proxy(PROXY_NAME, REDIS_LISTEN, REDIS_UPSTREAM)
    yield PROXY_NAME
    toxiproxy.enable_proxy(PROXY_NAME)
    toxiproxy.reset_proxy(PROXY_NAME)
    toxiproxy.delete_proxy(PROXY_NAME)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_redis_refused_falls_back_to_cold_tier(toxiproxy, redis_proxy):
    """Feature store falls back to cold tier without error when Redis is unavailable.

    Verifies:
    - ``set_state`` does not raise when Redis is down.
    - The stored state is still retrievable from the in-process fallback dict.
    - ``is_using_redis()`` returns ``False`` in fallback mode.
    """
    from datetime import datetime, timezone
    from detection.feature_store import WalletFeatureState

    toxiproxy.disable_proxy(redis_proxy)

    try:
        store = _make_store(_REDIS_PROXY_URL)

        state = WalletFeatureState(
            wallet="GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            asset_pair="XLM/USDC",
            last_updated=datetime.now(timezone.utc),
        )
        store.set_state(state)  # must not raise

        result = store.get_state(
            "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "XLM/USDC",
        )

        assert result is not None, (
            "State not accessible after Redis refusal — fallback failed"
        )
        assert not store.is_using_redis(), (
            "Expected fallback mode when Redis is unavailable"
        )
    finally:
        toxiproxy.enable_proxy(redis_proxy)


def test_redis_fallback_no_data_loss_on_get(toxiproxy, redis_proxy):
    """``get_state`` returns ``None`` gracefully when Redis is down and state was never stored."""
    toxiproxy.disable_proxy(redis_proxy)
    try:
        store = _make_store(_REDIS_PROXY_URL)
        result = store.get_state(
            "GNEVEREXISTS111111111111111111111111111111111111111111111",
            "XLM/USDC",
        )
        # Must return None, not raise
        assert result is None
    finally:
        toxiproxy.enable_proxy(redis_proxy)


def test_redis_recovery_resumes_hot_writes(toxiproxy, redis_proxy):
    """After Redis recovers, subsequent stores reach the hot Redis layer again."""
    from datetime import datetime, timezone
    from detection.feature_store import WalletFeatureState

    toxiproxy.disable_proxy(redis_proxy)

    # Build a store in fallback mode
    store = _make_store(_REDIS_PROXY_URL)
    state = WalletFeatureState(
        wallet="GBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        asset_pair="XLM/USDC",
        last_updated=datetime.now(timezone.utc),
    )
    store.set_state(state)
    assert not store.is_using_redis(), "Store should be in fallback mode while proxy is down"

    # Re-enable Redis
    toxiproxy.enable_proxy(redis_proxy)

    def _redis_available() -> bool:
        """Create a fresh store pointing directly at Redis (not the proxy)
        to confirm the hot layer is reachable again."""
        fresh = _make_store(_REDIS_DIRECT_URL)
        return fresh.is_using_redis()

    recovered = toxiproxy.wait_for_recovery(_redis_available, timeout_s=60)
    assert recovered, "Redis hot layer did not recover within 60 s"
