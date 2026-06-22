"""Tests for FeatureStore cold storage (SQLite) and cold-to-hot promotion."""

import pytest
from datetime import datetime, timezone
from pathlib import Path

from detection.feature_store import FeatureStore, WalletFeatureState
from detection.storage import save_feature_state, get_feature_state, promote_cold_to_hot


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


def test_save_feature_state(sample_state, tmp_path):
    """Test saving feature state to SQLite."""
    db_path = str(tmp_path / "test.db")
    
    save_feature_state(sample_state, db_path=db_path)
    
    # Verify it was saved
    retrieved = get_feature_state(sample_state.wallet, sample_state.asset_pair, db_path=db_path)
    assert retrieved is not None
    assert retrieved.wallet == sample_state.wallet
    assert retrieved.asset_pair == sample_state.asset_pair
    assert retrieved.trade_count == sample_state.trade_count


def test_get_feature_state_not_found(tmp_path):
    """Test getting non-existent feature state returns None."""
    db_path = str(tmp_path / "test.db")
    
    result = get_feature_state("UNKNOWN", "USDC/XLM", db_path=db_path)
    assert result is None


def test_save_feature_state_update(sample_state, tmp_path):
    """Test that saving updates existing state (INSERT OR REPLACE)."""
    db_path = str(tmp_path / "test.db")
    
    # Save initial state
    save_feature_state(sample_state, db_path=db_path)
    
    # Update with new values
    updated_state = sample_state.model_copy(
        update={
            "trade_count": 20,
            "trade_ring_1h": [(1000000, 100.0), (2000000, 200.0), (3000000, 300.0)],
        }
    )
    save_feature_state(updated_state, db_path=db_path)
    
    # Retrieve and verify
    retrieved = get_feature_state(sample_state.wallet, sample_state.asset_pair, db_path=db_path)
    assert retrieved.trade_count == 20
    assert len(retrieved.trade_ring_1h) == 3


def test_promote_cold_to_hot_single_state(sample_state, tmp_path):
    """Test promoting a single state from cold to hot storage."""
    db_path = str(tmp_path / "test.db")
    
    # Save state to cold storage
    save_feature_state(sample_state, db_path=db_path)
    
    # Create feature store (uses fallback for simplicity)
    fs = FeatureStore(redis_url=None)
    
    # Promote
    count = promote_cold_to_hot(fs, batch_size=100, db_path=db_path)
    assert count == 1
    
    # Verify it's in hot storage
    retrieved = fs.get_state(sample_state.wallet, sample_state.asset_pair)
    assert retrieved is not None
    assert retrieved.wallet == sample_state.wallet


def test_promote_cold_to_hot_batch_limit(sample_state, tmp_path):
    """Test that promote_cold_to_hot respects batch_size limit."""
    db_path = str(tmp_path / "test.db")
    
    # Save multiple states
    for i in range(10):
        state = sample_state.model_copy(
            update={"wallet": f"GA{i:03d}", "asset_pair": f"USDC/XLM"}
        )
        save_feature_state(state, db_path=db_path)
    
    fs = FeatureStore(redis_url=None)
    
    # Promote with batch_size=5
    count = promote_cold_to_hot(fs, batch_size=5, db_path=db_path)
    assert count == 5


def test_promote_cold_to_hot_empty(tmp_path):
    """Test promoting when cold storage is empty."""
    db_path = str(tmp_path / "test.db")
    
    fs = FeatureStore(redis_url=None)
    count = promote_cold_to_hot(fs, batch_size=100, db_path=db_path)
    assert count == 0


def test_feature_state_serialization_roundtrip(sample_state, tmp_path):
    """Test that feature state survives full serialization roundtrip."""
    db_path = str(tmp_path / "test.db")
    
    # Save to cold storage
    save_feature_state(sample_state, db_path=db_path)
    
    # Retrieve from cold storage
    retrieved = get_feature_state(sample_state.wallet, sample_state.asset_pair, db_path=db_path)
    
    # Compare fields
    assert retrieved.wallet == sample_state.wallet
    assert retrieved.asset_pair == sample_state.asset_pair
    assert retrieved.trade_count == sample_state.trade_count
    assert retrieved.trade_ring_1h == sample_state.trade_ring_1h
    assert retrieved.benford_digit_counts_30d == sample_state.benford_digit_counts_30d
    assert retrieved.counterparty_hashes_30d == sample_state.counterparty_hashes_30d


def test_multiple_wallets_cold_storage(tmp_path):
    """Test saving and retrieving multiple wallet states."""
    db_path = str(tmp_path / "test.db")
    
    states = []
    for i in range(5):
        state = WalletFeatureState(
            wallet=f"GA{i:03d}",
            asset_pair="USDC/XLM",
            last_updated=datetime.now(timezone.utc),
            trade_count=i * 10,
        )
        states.append(state)
        save_feature_state(state, db_path=db_path)
    
    # Verify all can be retrieved
    for state in states:
        retrieved = get_feature_state(state.wallet, state.asset_pair, db_path=db_path)
        assert retrieved is not None
        assert retrieved.trade_count == state.trade_count


def test_promote_cold_to_hot_with_redis_mock(sample_state, tmp_path):
    """Test cold-to-hot promotion writes to Redis (mocked)."""
    try:
        import fakeredis
    except ImportError:
        pytest.skip("fakeredis not installed")
    
    from unittest.mock import patch
    
    db_path = str(tmp_path / "test.db")
    save_feature_state(sample_state, db_path=db_path)
    
    with patch("detection.feature_store.redis.from_url") as mock_redis:
        mock_client = fakeredis.FakeStrictRedis()
        mock_redis.return_value = mock_client
        
        fs = FeatureStore(redis_url="redis://localhost:6379/0")
        assert fs._using_redis
        
        # Promote
        count = promote_cold_to_hot(fs, batch_size=100, db_path=db_path)
        assert count == 1
        
        # Verify it's in Redis
        retrieved = fs.get_state(sample_state.wallet, sample_state.asset_pair)
        assert retrieved is not None
        assert retrieved.wallet == sample_state.wallet
