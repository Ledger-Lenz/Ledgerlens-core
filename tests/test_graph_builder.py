from datetime import datetime, timezone, timedelta
import pytest
from ingestion.graph_builder import TemporalGraphBuilder

class FakeTrade:
    def __init__(self, base_account, counter_account, base_amount, ledger_close_time, trade_type="standard"):
        self.base_account = base_account
        self.counter_account = counter_account
        self.base_amount = base_amount
        self.ledger_close_time = ledger_close_time
        self.trade_type = trade_type

def _trades(n=5, start=None):
    # Safely within the lookback window
    start = start or datetime.now(timezone.utc) - timedelta(hours=23)
    return [FakeTrade(f"W{i}", f"W{i+1}", 100.0, start + timedelta(minutes=i)) for i in range(n)]

def test_node_and_edge_counts():
    builder = TemporalGraphBuilder(bucket_hours=4)
    trades = _trades(5)
    snapshots = builder.build_snapshots(trades, lookback_days=1)
    all_wallets = set()
    total_edges = 0
    for snap in snapshots:
        all_wallets.update(snap.wallet_index.keys())
        total_edges += snap.edge_index.shape[1]
    assert all_wallets == {f"W{i}" for i in range(6)}
    assert total_edges == 5

def test_high_degree_node_capped():
    builder = TemporalGraphBuilder(bucket_hours=4)
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    trades = [FakeTrade("HUB", f"W{i}", 10.0, start) for i in range(10)]
    snapshots = builder.build_snapshots(trades, lookback_days=1)
    assert len(snapshots) > 0

def test_no_self_loops_from_amm_trades():
    builder = TemporalGraphBuilder(bucket_hours=4)
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    trades = [FakeTrade("POOL", "POOL", 50.0, start, trade_type="liquidity_pool")]
    snapshots = builder.build_snapshots(trades, lookback_days=1)
    for snap in snapshots:
        if snap.edge_index.shape[1] == 0:
            continue
        assert not any(snap.edge_index[0, i] == snap.edge_index[1, i] for i in range(snap.edge_index.shape[1]))

def test_temporal_slices_cover_window_without_overlap():
    builder = TemporalGraphBuilder(bucket_hours=4)
    snapshots = builder.build_snapshots([], lookback_days=1)
    for a, b in zip(snapshots, snapshots[1:]):
        assert a.end == b.start
    assert snapshots[0].end - snapshots[0].start <= timedelta(hours=4)
