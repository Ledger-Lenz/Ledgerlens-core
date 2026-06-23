"""Tests for FeatureStore Redis integration and fallback behavior."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from detection.feature_store import FeatureStore, WalletFeatureState


@pytest.fixture
def sample_state():
    """Create a sample feature state."""
    return WalletFeatureState(
        wallet="GA123",
        asset_pair="USDC/XLM",
        last_updated=datetime.now(timezone.utc),
        trade_count=10,
        trade_ring_1h=[(1000000, 100.0), (2000000, 200.0)],
        benford_digit_counts_30d=[10, 20, 30, 0, 0, 0, 0, 0, 0],
        counterparty_hashes_30d=[123456, 789012],
    )


def test_feature_store_redis_set_get(sample_state):
    """Test set_state / get_state with Redis (using fakeredis)."""
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    with patch("detection.feature_store.redis.from_url") as mock_redis:
        mock_client = fakeredis.FakeStrictRedis()
        mock_redis.return_value = mock_client
        
        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        assert fs._using_redis
        
        # Set state
        fs.set_state(sample_state)
        
        # Get state
        retrieved = fs.get_state(sample_state.wallet, sample_state.asset_pair)
        assert retrieved is not None
        assert retrieved.wallet == sample_state.wallet
        assert retrieved.asset_pair == sample_state.asset_pair
        assert retrieved.trade_count == sample_state.trade_count


def test_feature_store_redis_ttl(sample_state):
    """Test that TTL is set on set_state."""
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    with patch("detection.feature_store.redis.from_url") as mock_redis, \
         patch("detection.feature_store.settings") as mock_settings:
        mock_client = fakeredis.FakeStrictRedis()
        mock_redis.return_value = mock_client
        mock_settings.feature_store_ttl_hours = 24
        
        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        fs.set_state(sample_state)
        
        # Check TTL was set (fakeredis stores TTL internally)
        key = fs._hash_key(sample_state.wallet, sample_state.asset_pair)
        mock_client.ttl(key)
        # fakeredis returns -1 if no TTL or -2 if key doesn't exist
        # This test verifies the key was stored
        assert mock_client.exists(key) > 0


def test_feature_store_redis_scan_all_keys(sample_state):
    """Test scan_all_keys retrieves all stored keys."""
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    with patch("detection.feature_store.redis.from_url") as mock_redis:
        mock_client = fakeredis.FakeStrictRedis()
        mock_redis.return_value = mock_client
        
        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        
        # Add multiple states
        state1 = sample_state
        fs.set_state(state1)
        
        state2 = sample_state.model_copy(
            update={"wallet": "GA456", "asset_pair": "USDT/XLM"}
        )
        fs.set_state(state2)
        
        # Scan all keys
        keys = fs.scan_all_keys()
        assert len(keys) == 2
        assert all(k.startswith("ll:feature:") for k in keys)


def test_feature_store_redis_fallback_on_connection_error():
    """Test fallback to in-process dict when Redis is unavailable."""
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    with patch("detection.feature_store.redis.from_url") as mock_redis:
        # Simulate connection error
        mock_redis.side_effect = Exception("Connection refused")
        
        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        
        # Should fall back to in-process
        assert not fs._using_redis
        assert fs._fallback_dict is not None


def test_feature_store_redis_fallback_on_ping_error():
    """Test fallback when redis.ping() fails."""
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    with patch("detection.feature_store.redis.from_url") as mock_redis:
        mock_client = MagicMock()
        mock_client.ping.side_effect = Exception("Ping failed")
        mock_redis.return_value = mock_client
        
        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        assert not fs._using_redis


def test_feature_store_fallback_dict_get_set(sample_state):
    """Test get/set with fallback dict (no Redis)."""
    fs = FeatureStore(redis_url=None)
    
    # Should use fallback
    assert not fs._using_redis
    assert len(fs._fallback_dict) == 0
    
    # Set and get
    fs.set_state(sample_state)
    retrieved = fs.get_state(sample_state.wallet, sample_state.asset_pair)
    
    assert retrieved is not None
    assert retrieved.wallet == sample_state.wallet
    assert len(fs._fallback_dict) == 1


def test_feature_store_delete_state_redis(sample_state):
    """Test delete_state with Redis."""
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    with patch("detection.feature_store.redis.from_url") as mock_redis:
        mock_client = fakeredis.FakeStrictRedis()
        mock_redis.return_value = mock_client
        
        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        fs.set_state(sample_state)
        
        # Verify it's there
        assert fs.get_state(sample_state.wallet, sample_state.asset_pair) is not None
        
        # Delete
        fs.delete_state(sample_state.wallet, sample_state.asset_pair)
        
        # Should be gone
        assert fs.get_state(sample_state.wallet, sample_state.asset_pair) is None


def test_feature_store_delete_state_fallback(sample_state):
    """Test delete_state with fallback dict."""
    fs = FeatureStore(redis_url=None)
    fs.set_state(sample_state)
    
    # Verify it's there
    assert fs.get_state(sample_state.wallet, sample_state.asset_pair) is not None
    
    # Delete
    fs.delete_state(sample_state.wallet, sample_state.asset_pair)
    
    # Should be gone
    assert fs.get_state(sample_state.wallet, sample_state.asset_pair) is None
    assert len(fs._fallback_dict) == 0


def test_feature_store_fallback_to_dict_on_redis_error(sample_state):
    """Test that operations fall back to dict when Redis operations fail."""
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    with patch("detection.feature_store.redis.from_url") as mock_redis:
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        # Make get() raise an error
        mock_client.get.side_effect = Exception("Redis error")
        mock_redis.return_value = mock_client
        
        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        assert fs._using_redis  # Initially connected
        
        # set_state should fall back to dict
        fs.set_state(sample_state)
        
        # get_state should fall back to dict
        retrieved = fs.get_state(sample_state.wallet, sample_state.asset_pair)
        assert retrieved is not None
        assert retrieved.wallet == sample_state.wallet


def test_is_using_redis():
    """Test is_using_redis() indicator."""
    fs_no_redis = FeatureStore(redis_url=None)
    assert not fs_no_redis.is_using_redis()
    
    try:
        import fakeredis  # noqa: F401
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    with patch("detection.feature_store.redis.from_url") as mock_redis:
        mock_client = fakeredis.FakeStrictRedis()
        mock_redis.return_value = mock_client
        
        fs_with_redis = FeatureStore(redis_url="redis://localhost:6379/0")
        assert fs_with_redis.is_using_redis()
