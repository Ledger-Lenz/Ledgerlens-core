"""Tests for the streaming feature store (incremental updates, ring buffers, equivalence)."""

import pytest
from datetime import datetime, timezone, timedelta

from detection.feature_store import (
    WalletFeatureState,
    update_feature_state,
    derive_feature_vector,
    _get_first_significant_digit,
    _hash_counterparty,
    RING_BUFFER_CAPS,
)
from ingestion.data_models import Asset, TradeType
from tests.factories import TradeFactory


@pytest.fixture
def sample_trade():
    """Create a sample trade for testing."""
    return TradeFactory.trade(
        id="trade_123",
        ledger_close_time=datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc),
        base_account="GA123",
        counter_account="GA456",
        base_asset=Asset(code="USDC", issuer="GBUQWP3BOUZX34LOCALHOSTED"),
        counter_asset=Asset(code="XLM", issuer=None),
        base_amount=100.5,
        counter_amount=500.0,
        price=5.0,
        base_is_seller=True,
        trade_type=TradeType.ORDERBOOK,
    )


@pytest.fixture
def initial_state():
    """Create an initial feature state."""
    return WalletFeatureState(
        wallet="GA123",
        asset_pair="USDC:GBUQWP3BOUZX34LOCALHOSTED/XLM",
        last_updated=datetime.now(timezone.utc),
    )


def test_benford_digit_extraction():
    """Test first significant digit extraction for Benford's Law."""
    assert _get_first_significant_digit(123.45) == 1
    assert _get_first_significant_digit(0.00456) == 4
    assert _get_first_significant_digit(987.0) == 9
    assert _get_first_significant_digit(0.0) == 0
    assert _get_first_significant_digit(-50.5) == 5


def test_hash_counterparty():
    """Test counterparty hashing."""
    hash1 = _hash_counterparty("GA123")
    hash2 = _hash_counterparty("GA456")
    
    # Should be deterministic
    assert hash1 == _hash_counterparty("GA123")
    
    # Different inputs should produce different hashes (with high probability)
    assert hash1 != hash2
    
    # Should be a 32-bit integer
    assert 0 <= hash1 <= 0xFFFFFFFF
    assert 0 <= hash2 <= 0xFFFFFFFF


def test_update_feature_state_adds_trade(sample_trade, initial_state):
    """Test that update_feature_state adds trade to ring buffers."""
    import time
    time.sleep(0.01)  # Ensure timestamp difference
    updated = update_feature_state(initial_state, sample_trade)
    
    # Trade should be in all rings
    assert len(updated.trade_ring_1h) == 1
    assert len(updated.trade_ring_4h) == 1
    assert len(updated.trade_ring_24h) == 1
    assert len(updated.trade_ring_7d) == 1
    assert len(updated.trade_ring_30d) == 1
    
    # Metadata should be updated
    assert updated.trade_count == 1
    assert updated.last_updated >= initial_state.last_updated


def test_update_feature_state_benford_update(sample_trade, initial_state):
    """Test that Benford digit counts are updated correctly."""
    updated = update_feature_state(initial_state, sample_trade)
    
    # base_amount = 100.5, first digit = 1
    assert updated.benford_digit_counts_30d[0] == 1  # digit 1 is at index 0
    assert sum(updated.benford_digit_counts_30d) == 1


def test_update_feature_state_counterparty_update(sample_trade, initial_state):
    """Test that counterparty hashes are updated correctly."""
    updated = update_feature_state(initial_state, sample_trade)
    
    # Should have one counterparty
    assert len(updated.counterparty_hashes_30d) == 1
    
    # Second trade with same counterparty should not add duplicate
    sample_trade2 = sample_trade.model_copy(
        update={"id": "trade_456", "ledger_close_time": sample_trade.ledger_close_time + timedelta(seconds=1)}
    )
    updated2 = update_feature_state(updated, sample_trade2)
    assert len(updated2.counterparty_hashes_30d) == 1


def test_ring_buffer_overflow(initial_state):
    """Test that ring buffer overflow evicts oldest entries (FIFO)."""
    # Test with small number of trades to verify FIFO eviction works
    state = initial_state
    base_time = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    
    # Add trades (200 trades spanning 20 seconds, all within 1h window)
    for i in range(200):
        trade = TradeFactory.trade(
            id=f"trade_{i}",
            ledger_close_time=base_time + timedelta(milliseconds=i * 100),
            base_account="GA123",
            counter_account=f"GA{i % 5}",  # Reuse counterparties
            base_asset=Asset(code="USDC", issuer="GBUQWP3BOUZX34LOCALHOSTED"),
            counter_asset=Asset(code="XLM", issuer=None),
            base_amount=100.0 + i,
            counter_amount=500.0,
            price=5.0,
            base_is_seller=True,
        )
        state = update_feature_state(state, trade)
    
    # Ring should respect the cap
    assert len(state.trade_ring_1h) <= RING_BUFFER_CAPS["1h"]
    # Since all trades are within 1h, should have all 200
    assert len(state.trade_ring_1h) == 200


def test_prune_expired_entries(initial_state):
    """Test that entries older than window are pruned from ring buffers."""
    base_time = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    
    # Add a trade at the beginning
    trade1 = TradeFactory.trade(
        id="trade_1",
        ledger_close_time=base_time,
        base_account="GA123",
        counter_account="GA456",
        base_asset=Asset(code="USDC", issuer="GBUQWP3BOUZX34LOCALHOSTED"),
        counter_asset=Asset(code="XLM", issuer=None),
        base_amount=100.0,
        counter_amount=500.0,
        price=5.0,
        base_is_seller=True,
    )
    state = update_feature_state(initial_state, trade1)
    assert len(state.trade_ring_1h) == 1
    
    # Add a trade 2 hours later
    trade2 = TradeFactory.trade(
        id="trade_2",
        ledger_close_time=base_time + timedelta(hours=2),
        base_account="GA123",
        counter_account="GA456",
        base_asset=Asset(code="USDC", issuer="GBUQWP3BOUZX34LOCALHOSTED"),
        counter_asset=Asset(code="XLM", issuer=None),
        base_amount=100.0,
        counter_amount=500.0,
        price=5.0,
        base_is_seller=True,
    )
    state = update_feature_state(state, trade2)
    
    # The first trade should have been pruned from 1h ring (> 1h old)
    assert len(state.trade_ring_1h) == 1
    assert state.trade_ring_1h[0][1] == 100.0  # Only trade2 remains
    
    # But the 4h ring should have both
    assert len(state.trade_ring_4h) == 2


def test_derive_feature_vector_benford(initial_state):
    """Test that derive_feature_vector computes Benford features correctly."""
    base_time = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    
    # Create states with known Benford distributions
    # Benford's Law: digit 1 should appear ~30%, digit 2 ~17%, etc.
    state = initial_state
    
    # Add 100 trades with amounts starting with 1-9 (cycling)
    for i in range(100):
        digit = (i % 9) + 1  # digits 1-9
        amount = float(f"{digit}00.0")  # 100, 200, ..., 900
        
        trade = TradeFactory.trade(
            id=f"trade_{i}",
            ledger_close_time=base_time + timedelta(seconds=i),
            base_account="GA123",
            counter_account=f"GA{i % 10}",
            base_asset=Asset(code="USDC", issuer="GBUQWP3BOUZX34LOCALHOSTED"),
            counter_asset=Asset(code="XLM", issuer=None),
            base_amount=amount,
            counter_amount=500.0,
            price=5.0,
            base_is_seller=True,
        )
        state = update_feature_state(state, trade)
    
    features = derive_feature_vector(state)
    
    # Should have Benford features for all windows
    assert "benford_chi_square_1h" in features
    assert "benford_mad_1h" in features
    assert "benford_max_zscore_1h" in features
    assert "benford_chi_square_30d" in features
    
    # Features should be numeric
    for key in ["benford_chi_square_1h", "benford_mad_1h", "benford_max_zscore_1h"]:
        assert isinstance(features[key], float)
        assert features[key] >= 0


def test_derive_feature_vector_volume_ratio(initial_state):
    """Test volume_to_unique_counterparty_ratio calculation."""
    base_time = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    
    state = initial_state
    
    # Add 10 trades with 5 unique counterparties
    for i in range(10):
        trade = TradeFactory.trade(
            id=f"trade_{i}",
            ledger_close_time=base_time + timedelta(seconds=i),
            base_account="GA123",
            counter_account=f"GA{i % 5}",
            base_asset=Asset(code="USDC", issuer="GBUQWP3BOUZX34LOCALHOSTED"),
            counter_asset=Asset(code="XLM", issuer=None),
            base_amount=100.0,
            counter_amount=500.0,
            price=5.0,
            base_is_seller=True,
        )
        state = update_feature_state(state, trade)
    
    features = derive_feature_vector(state)
    
    # volume_to_unique_counterparty_ratio = 1000 / 5 = 200
    assert features["volume_to_unique_counterparty_ratio"] == 200.0


def test_serialize_deserialize_state(initial_state):
    """Test JSON serialization/deserialization of WalletFeatureState."""
    json_str = initial_state.model_dump_json_compat()
    deserialized = WalletFeatureState.model_validate_json_compat(json_str)
    
    assert deserialized.wallet == initial_state.wallet
    assert deserialized.asset_pair == initial_state.asset_pair
    assert deserialized.trade_count == initial_state.trade_count
    assert deserialized.trade_ring_1h == initial_state.trade_ring_1h


def test_get_state_unknown_wallet(initial_state):
    """Test that FeatureStore.get_state returns None for unknown wallet."""
    from detection.feature_store import FeatureStore
    
    fs = FeatureStore()
    result = fs.get_state("UNKNOWN", "USDC/XLM")
    assert result is None


def test_set_get_state_roundtrip(initial_state):
    """Test set_state / get_state roundtrip."""
    from detection.feature_store import FeatureStore
    
    fs = FeatureStore()
    fs.set_state(initial_state)
    
    retrieved = fs.get_state(initial_state.wallet, initial_state.asset_pair)
    assert retrieved is not None
    assert retrieved.wallet == initial_state.wallet
    assert retrieved.asset_pair == initial_state.asset_pair


def test_fallback_dict_lru_eviction():
    """Test that fallback dict evicts LRU entries when at capacity."""
    from detection.feature_store import FeatureStore
    
    max_entries = 10
    fs = FeatureStore(redis_url=None, max_fallback_entries=max_entries)
    
    # Add more than max entries
    for i in range(max_entries + 5):
        state = WalletFeatureState(
            wallet=f"GA{i}",
            asset_pair="USDC/XLM",
            last_updated=datetime.now(timezone.utc),
        )
        fs.set_state(state)
    
    # Should only have max_entries in fallback dict
    assert len(fs._fallback_dict) == max_entries
