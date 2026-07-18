import sqlite3
import time
import json
from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import MagicMock, patch

from config.settings import settings
from ingestion.dedup import IdempotencyKeyStore, BridgeEventDeduplicator, DedupResult, DeduplicationStats
from ingestion.data_models import Trade, Asset, TradeType
from detection.rolling_window import RollingWindowState
from detection.storage import RiskScoreStore
from ingestion.historical_loader import ParallelHistoricalLoader
from ingestion.solana_adapter import SolanaAdapter

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_ledgerlens.db"
    db_path = str(db_file)
    from detection.storage import migrate_db
    with sqlite3.connect(db_path) as conn:
        migrate_db(conn)
    return db_path

def test_compute_key_stable_ordering():
    store = IdempotencyKeyStore(db_path=":memory:")
    # Identical keys/values but different dictionary/argument order
    k1 = store.compute_key("horizon", ledger_sequence=50123456, tx_hash="ABCDEF", operation_index=0)
    k2 = store.compute_key("horizon", operation_index=0, tx_hash="abcdef", ledger_sequence=50123456)
    assert k1 == k2

    # Different values
    k3 = store.compute_key("horizon", ledger_sequence=50123456, tx_hash="ABCDEF", operation_index=1)
    assert k1 != k3

def test_is_duplicate_and_replay_window(temp_db):
    # Set replay window to 2 seconds
    store = IdempotencyKeyStore(db_path=temp_db, replay_window_seconds=2.0)
    key = "test_key"
    
    # 1. New event within window
    now = datetime.now(timezone.utc)
    assert store.is_duplicate(key, timestamp=now) == DedupResult.NEW
    
    # 2. Mark seen and check duplicate
    store.mark_seen(key, source="horizon")
    assert store.is_duplicate(key, timestamp=now) == DedupResult.DUPLICATE
    
    # 3. Old event (far outside 2s window) -> REPLAY_REJECTED
    old_time = now - timedelta(seconds=10)
    other_key = "other_key"
    assert store.is_duplicate(other_key, timestamp=old_time) == DedupResult.REPLAY_REJECTED

def test_bridge_event_deduplicator_compat(temp_db):
    conn = sqlite3.connect(temp_db)
    try:
        # Wrap the connection in the deduplicator
        dedup = BridgeEventDeduplicator(db_conn=conn, replay_window_blocks=100)
        
        # 1. New event
        res1 = dedup.is_duplicate(chain_id=1, tx_hash="0xABC", log_index=0, block_number=1050, current_chain_head=1100)
        assert res1 == DedupResult.NEW
        
        dedup.mark_seen(chain_id=1, tx_hash="0xABC", log_index=0, block_number=1050)
        
        # 2. Duplicate event
        res2 = dedup.is_duplicate(chain_id=1, tx_hash="0xABC", log_index=0, block_number=1050, current_chain_head=1100)
        assert res2 == DedupResult.DUPLICATE
        
        # 3. Replay rejection (block_number 950 < 1100 - 100)
        res3 = dedup.is_duplicate(chain_id=1, tx_hash="0xDEF", log_index=0, block_number=950, current_chain_head=1100)
        assert res3 == DedupResult.REPLAY_REJECTED
        
        # 4. handle_reorg
        invalidated = dedup.handle_reorg(chain_id=1, reorg_from_block=1050)
        assert invalidated == 1
        
        # Checking if it's NEW again after reorg invalidation
        res4 = dedup.is_duplicate(chain_id=1, tx_hash="0xABC", log_index=0, block_number=1050, current_chain_head=1100)
        assert res4 == DedupResult.NEW
        
        # 5. prune_old_entries
        dedup.mark_seen(chain_id=1, tx_hash="0xABC", log_index=0, block_number=1050)
        pruned = dedup.prune_old_entries(current_chain_head=1200, keep_blocks=100)
        assert pruned == 1
    finally:
        conn.close()

@pytest.mark.asyncio
async def test_concurrent_historical_loader_dedup(temp_db):
    settings.ingestion_dedup_enabled = True
    
    storage = RiskScoreStore(temp_db)
    client = MagicMock()
    
    trade_json = {
        "id": "123-0",
        "paging_token": "123-0",
        "ledger_close_time": datetime.now(timezone.utc).isoformat(),
        "base_account": "GBASE",
        "counter_account": "GCOUNTER",
        "base_amount": "10.0",
        "counter_amount": "2.0",
        "price": {"n": 1, "d": 5},
        "base_is_seller": True,
    }
    
    async def mock_get(path, params=None):
        return {
            "_embedded": {"records": [trade_json]},
            "_links": {"next": {"href": ""}},
        }
    client.get = mock_get
    
    with patch("ingestion.historical_loader.settings") as mock_settings:
        import os
        from pathlib import Path
        mock_settings.data_dir = os.path.dirname(temp_db)
        mock_settings.db_path = temp_db
        mock_settings.ingestion_dedup_enabled = True
        mock_settings.idempotency_replay_window_seconds = 3600.0
        
        loader = ParallelHistoricalLoader(
            client=client,
            storage=storage,
            concurrency=2,
            progress_path=Path(os.path.dirname(temp_db)) / "historical_progress.json",
        )
        loader.dedup_store = IdempotencyKeyStore(db_path=temp_db)
        
        import asyncio
        sem = asyncio.Semaphore(2)
        chunk = (datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc) + timedelta(hours=1))
        
        task1 = loader._fetch_chunk(chunk, None, sem)
        task2 = loader._fetch_chunk(chunk, None, sem)
        
        await asyncio.gather(task1, task2)
        
        conn = sqlite3.connect(temp_db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

def test_horizon_streamer_checkpoint_replay_dedup(temp_db):
    settings.ingestion_dedup_enabled = True
    
    trade = Trade(
        id="999-0",
        paging_token="999-0",
        ledger_close_time=datetime.now(timezone.utc),
        base_account="GBASE",
        counter_account="GCOUNTER",
        base_asset=Asset(code="XLM"),
        counter_asset=Asset(code="USDC", issuer="GISSUER"),
        base_amount=10.0,
        counter_amount=2.0,
        price=0.2,
        base_is_seller=True,
        trade_type=TradeType.ORDERBOOK,
        transaction_hash="TXHASH",
    )
    
    event_mock = MagicMock()
    event_mock.data = '{"id":"999-0","paging_token":"999-0","ledger_close_time":"' + trade.ledger_close_time.isoformat() + '","base_account":"GBASE","counter_account":"GCOUNTER","base_asset_code":"XLM","counter_asset_code":"USDC","counter_asset_issuer":"GISSUER","base_amount":"10.0","counter_amount":"2.0","price":{"n":1,"d":5},"base_is_seller":true}'
    event_mock.id = "999-0"
    
    with patch("ingestion.horizon_streamer.settings") as mock_settings:
        mock_settings.db_path = temp_db
        mock_settings.ingestion_dedup_enabled = True
        mock_settings.idempotency_replay_window_seconds = 3600.0
        
        with patch("sseclient.SSEClient", return_value=[event_mock, event_mock]):
            from ingestion.horizon_streamer import stream_trades_with_cursor
            
            trades = []
            for t, cursor in stream_trades_with_cursor(cursor="998-0"):
                trades.append(t)
                if len(trades) >= 2:
                    break
            
            assert len(trades) == 1

def test_solana_adapter_restart_dedup(temp_db):
    settings.ingestion_dedup_enabled = True
    
    store = IdempotencyKeyStore(db_path=temp_db)
    adapter = SolanaAdapter(dedup_store=store)
    
    tx = {
        "blockTime": int(time.time()),
        "transaction": {
            "message": {
                "accountKeys": ["DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5ZARQ", "ACCT_B", "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"],
                "instructions": []
            }
        },
        "meta": {
            "preTokenBalances": [
                {"accountIndex": 0, "mint": "So11111111111111111111111111111111111111112", "owner": "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5ZARQ", "uiTokenAmount": {"uiAmount": 10.0}}
            ],
            "postTokenBalances": [
                {"accountIndex": 0, "mint": "So11111111111111111111111111111111111111112", "owner": "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5ZARQ", "uiTokenAmount": {"uiAmount": 8.0}},
                {"accountIndex": 1, "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "owner": "ACCT_B", "uiTokenAmount": {"uiAmount": 20.0}}
            ]
        }
    }
    
    with patch("ingestion.solana_adapter._get_signatures", return_value=[{"signature": "SIG_1"}]):
        with patch("ingestion.solana_adapter._get_transaction", return_value=tx):
            trades1 = adapter.ingest("DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5ZARQ")
            assert len(trades1) == 1
            
            trades2 = adapter.ingest("DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5ZARQ")
            assert len(trades2) == 0

def test_dedup_audit_cli(temp_db, monkeypatch):
    store = IdempotencyKeyStore(db_path=temp_db)
    
    k1 = store.compute_key("horizon", ledger_sequence=1, tx_hash="hash1", operation_index=1)
    
    store.is_duplicate(k1, timestamp=datetime.now(timezone.utc), source="horizon", metadata={"wallet": "GABC1234567890123456789012345678901234567890123456789012"})
    store.mark_seen(k1, source="horizon", metadata={"wallet": "GABC1234567890123456789012345678901234567890123456789012"})
    store.is_duplicate(k1, timestamp=datetime.now(timezone.utc), source="horizon", metadata={"wallet": "GABC1234567890123456789012345678901234567890123456789012"})
    
    from typer.testing import CliRunner
    from cli import app
    
    runner = CliRunner()
    monkeypatch.setattr(settings, "db_path", temp_db)
    
    since_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    result = runner.invoke(app, ["dedup-audit", "--source", "horizon", "--since", since_time])
    
    assert result.exit_code == 0
    assert "DeduplicationStats" in result.stdout
    assert "seen_total=2" in result.stdout
    assert "duplicate_total=1" in result.stdout
    # Check wallet address masking
    assert "GABC1234567890123456789012345678901234567890123456789012" not in result.stdout
    assert "GABC1234...9012" in result.stdout
