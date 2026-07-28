"""Tests for FeatureStore Redis integration and fallback behavior.

Each test that requires fakeredis skips gracefully when the package is not
installed.  All Redis clients are injected via ``unittest.mock.patch`` so no
real Redis process is required.  Tests are fully isolated: every FeatureStore
instance is created fresh inside the test and shares no module-level state.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from detection.feature_store import FeatureStore, WalletFeatureState


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _requires_fakeredis():
    """Skip the enclosing test if fakeredis is not installed."""
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")


def _fake_client():
    """Return a fresh FakeStrictRedis instance."""
    import fakeredis
    return fakeredis.FakeStrictRedis()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_state():
    """Minimal WalletFeatureState used across multiple tests."""
    return WalletFeatureState(
        wallet="GA123",
        asset_pair="USDC/XLM",
        last_updated=datetime.now(timezone.utc),
        trade_count=10,
        trade_ring_1h=[(1_000_000, 100.0), (2_000_000, 200.0)],
        benford_digit_counts_30d=[10, 20, 30, 0, 0, 0, 0, 0, 0],
        counterparty_hashes_30d=[123456, 789012],
    )


# ---------------------------------------------------------------------------
# Redis-backed set / get round-trip
# ---------------------------------------------------------------------------

def test_feature_store_redis_set_get(sample_state):
    """set_state followed by get_state round-trips correctly through fakeredis."""
    _requires_fakeredis()

    with patch("redis.from_url") as mock_redis:
        mock_redis.return_value = _fake_client()

        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        assert fs._using_redis

        fs.set_state(sample_state)
        retrieved = fs.get_state(sample_state.wallet, sample_state.asset_pair)

    assert retrieved is not None
    assert retrieved.wallet == sample_state.wallet
    assert retrieved.asset_pair == sample_state.asset_pair
    assert retrieved.trade_count == sample_state.trade_count


# ---------------------------------------------------------------------------
# TTL is applied on set_state
# ---------------------------------------------------------------------------

def test_feature_store_redis_ttl(sample_state):
    """set_state stores the key so that it exists (TTL path exercised)."""
    _requires_fakeredis()

    with patch("redis.from_url") as mock_redis, \
         patch("detection.feature_store.settings") as mock_settings:
        client = _fake_client()
        mock_redis.return_value = client
        mock_settings.feature_store_ttl_hours = 24

        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        fs.set_state(sample_state)

        key = fs._hash_key(sample_state.wallet, sample_state.asset_pair)
        assert client.exists(key) > 0


# ---------------------------------------------------------------------------
# scan_all_keys
# ---------------------------------------------------------------------------

def test_feature_store_redis_scan_all_keys(sample_state):
    """scan_all_keys returns one key per stored state, all prefixed ll:feature:."""
    _requires_fakeredis()

    with patch("redis.from_url") as mock_redis:
        mock_redis.return_value = _fake_client()

        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        fs.set_state(sample_state)

        state2 = sample_state.model_copy(
            update={"wallet": "GA456", "asset_pair": "USDT/XLM"}
        )
        fs.set_state(state2)

        keys = fs.scan_all_keys()

    assert len(keys) == 2
    assert all(k.startswith("ll:feature:") for k in keys)


# ---------------------------------------------------------------------------
# Fallback — connection-level error
# ---------------------------------------------------------------------------

def test_feature_store_fallback_on_connection_error():
    """FeatureStore switches to in-process dict when redis.from_url raises."""
    _requires_fakeredis()

    with patch("redis.from_url") as mock_redis:
        mock_redis.side_effect = Exception("Connection refused")

        fs = FeatureStore(redis_url="redis://localhost:6379/0")

    assert not fs._using_redis
    assert fs._fallback_dict is not None


# ---------------------------------------------------------------------------
# Fallback — ping-level error
# ---------------------------------------------------------------------------

def test_feature_store_fallback_on_ping_error():
    """FeatureStore switches to in-process dict when redis.ping() raises."""
    _requires_fakeredis()

    with patch("redis.from_url") as mock_redis:
        client = MagicMock()
        client.ping.side_effect = Exception("Ping failed")
        mock_redis.return_value = client

        fs = FeatureStore(redis_url="redis://localhost:6379/0")

    assert not fs._using_redis


# ---------------------------------------------------------------------------
# Fallback dict — no Redis configured
# ---------------------------------------------------------------------------

def test_feature_store_fallback_dict_get_set(sample_state):
    """When redis_url is None the store uses the in-process fallback dict."""
    fs = FeatureStore(redis_url=None)

    assert not fs._using_redis
    assert len(fs._fallback_dict) == 0

    fs.set_state(sample_state)
    retrieved = fs.get_state(sample_state.wallet, sample_state.asset_pair)

    assert retrieved is not None
    assert retrieved.wallet == sample_state.wallet
    assert len(fs._fallback_dict) == 1


# ---------------------------------------------------------------------------
# delete_state — Redis path
# ---------------------------------------------------------------------------

def test_feature_store_delete_state_redis(sample_state):
    """delete_state removes the key from Redis."""
    _requires_fakeredis()

    with patch("redis.from_url") as mock_redis:
        mock_redis.return_value = _fake_client()

        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        fs.set_state(sample_state)

        assert fs.get_state(sample_state.wallet, sample_state.asset_pair) is not None

        fs.delete_state(sample_state.wallet, sample_state.asset_pair)

        assert fs.get_state(sample_state.wallet, sample_state.asset_pair) is None


# ---------------------------------------------------------------------------
# delete_state — fallback dict path
# ---------------------------------------------------------------------------

def test_feature_store_delete_state_fallback(sample_state):
    """delete_state removes the entry from the fallback dict."""
    fs = FeatureStore(redis_url=None)
    fs.set_state(sample_state)

    assert fs.get_state(sample_state.wallet, sample_state.asset_pair) is not None

    fs.delete_state(sample_state.wallet, sample_state.asset_pair)

    assert fs.get_state(sample_state.wallet, sample_state.asset_pair) is None
    assert len(fs._fallback_dict) == 0


# ---------------------------------------------------------------------------
# Fallback on mid-operation Redis errors
# ---------------------------------------------------------------------------

def test_feature_store_fallback_to_dict_on_redis_error(sample_state):
    """When setex() and get() both raise, set_state and get_state fall back to
    the in-process dict so callers observe no data loss.

    Behaviour:
    - Ping succeeds → _using_redis is True.
    - setex() raises → set_state writes to _fallback_dict instead.
    - get() raises → get_state reads from _fallback_dict and finds the entry
      written by the fallback set_state path.
    """
    _requires_fakeredis()

    with patch("redis.from_url") as mock_redis:
        client = MagicMock()
        client.ping.return_value = True
        client.get.side_effect = Exception("Redis get error")
        client.setex.side_effect = Exception("Redis setex error")
        mock_redis.return_value = client

        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        assert fs._using_redis

        fs.set_state(sample_state)
        retrieved = fs.get_state(sample_state.wallet, sample_state.asset_pair)

    assert retrieved is not None
    assert retrieved.wallet == sample_state.wallet


# ---------------------------------------------------------------------------
# is_using_redis indicator
# ---------------------------------------------------------------------------

def test_is_using_redis_false_without_redis():
    """is_using_redis() returns False when no redis_url is provided."""
    fs = FeatureStore(redis_url=None)
    assert not fs.is_using_redis()


def test_is_using_redis_true_with_fakeredis():
    """is_using_redis() returns True when Redis is reachable."""
    _requires_fakeredis()

    with patch("redis.from_url") as mock_redis:
        mock_redis.return_value = _fake_client()

        fs = FeatureStore(redis_url="redis://localhost:6379/0")

    assert fs.is_using_redis()
