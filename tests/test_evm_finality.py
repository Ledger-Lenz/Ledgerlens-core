"""Reorg-safe finality tracking for EVM ingestion (ingestion/evm_finality.py)."""
from unittest.mock import patch

import pytest

pytest.importorskip("web3")

from ingestion.bridge_loader import BridgeTransferLoader
from ingestion.evm_finality import FinalityTracker

BRIDGE_CONTRACT = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
SENDER_TOPIC = "0x000000000000000000000000ab5801a7d398351b8be11c439e05c5b3259aec9b"


class FakeChain:
    """Minimal JSON-RPC chain: block number -> hash, plus bridge logs."""

    def __init__(self, head: int) -> None:
        self.head = head
        self.hashes = {n: f"0xa{n:063x}" for n in range(head + 1)}
        self.logs: list[dict] = []

    def add_log(self, block: int, tx: str, recipient_hex: str) -> None:
        self.logs.append({
            "address": BRIDGE_CONTRACT,
            "topics": ["0xtopic", SENDER_TOPIC],
            "data": "0x" + recipient_hex + "0" * 64,
            "blockNumber": hex(block),
            "blockHash": self.hashes[block],
            "transactionHash": tx,
            "logIndex": "0x0",
        })

    def reorg(self, from_block: int) -> None:
        """Replace every block >= from_block with a new fork, dropping its logs."""
        for n in range(from_block, self.head + 1):
            self.hashes[n] = f"0xb{n:063x}"
        self.logs = [log for log in self.logs if int(log["blockNumber"], 16) < from_block]

    def rpc(self, method: str, params: list, max_retries: int = 3) -> dict:
        if method == "eth_blockNumber":
            return {"result": hex(self.head)}
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return {"result": {"hash": self.hashes.get(n), "timestamp": hex(1_700_000_000 + n)}}
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            return {"result": [log for log in self.logs if lo <= int(log["blockNumber"], 16) <= hi]}
        raise AssertionError(method)


def _recipient_hex() -> str:
    from stellar_sdk import Keypair
    return Keypair.random().raw_public_key().hex()


def test_finalized_head_and_is_final():
    tracker = FinalityTracker(confirmation_depth=12)
    assert tracker.finalized_head(100) == 88
    assert tracker.is_final(88, 100)
    assert not tracker.is_final(89, 100)
    assert tracker.finalized_head(5) == 0


def test_unconfirmed_blocks_are_not_ingested():
    chain = FakeChain(head=100)
    chain.add_log(95, "0xunconfirmed", _recipient_hex())
    chain.add_log(80, "0xconfirmed", _recipient_hex())
    loader = BridgeTransferLoader("ethereum", "http://rpc", BRIDGE_CONTRACT,
                                  finality=FinalityTracker(confirmation_depth=12))
    with patch.object(loader, "_rpc_call", side_effect=chain.rpc), \
         patch("ingestion.bridge_loader.settings") as s:
        s.bridge_verify_sample_rate = 0.0
        s.db_path = ""
        transfers = loader.load_transfers(lookback_blocks=50, db_path=None)
    assert [t.tx_hash_evm for t in transfers] == ["0xconfirmed"]


def test_reorg_past_confirmation_depth_retracts_orphaned_data(tmp_path):
    from detection.storage import get_bridge_transfers, init_db

    db = str(tmp_path / "reorg.db")
    init_db(db)
    chain = FakeChain(head=100)
    chain.add_log(70, "0xsafe", _recipient_hex())
    chain.add_log(85, "0xorphan", _recipient_hex())
    loader = BridgeTransferLoader("ethereum", "http://rpc", BRIDGE_CONTRACT,
                                  finality=FinalityTracker(confirmation_depth=12))

    with patch.object(loader, "_rpc_call", side_effect=chain.rpc), \
         patch("ingestion.bridge_loader.settings") as s:
        s.bridge_verify_sample_rate = 0.0
        s.db_path = db
        first = loader.load_transfers(lookback_blocks=50, db_path=db)
        assert {t.tx_hash_evm for t in first} == {"0xsafe", "0xorphan"}
        stored = {t.tx_hash_evm for t in get_bridge_transfers(since_days=100000, db_path=db)}
        assert stored == {"0xsafe", "0xorphan"}

        # A deep reorg from block 80 — 20 blocks below head, deeper than the
        # 12-block confirmation depth — orphans block 85, which was already final.
        chain.reorg(80)
        second = loader.load_transfers(lookback_blocks=50, db_path=db)

    assert loader.retracted_tx_hashes == {"0xorphan"}
    stored = {t.tx_hash_evm for t in get_bridge_transfers(since_days=100000, db_path=db)}
    assert stored == {"0xsafe"}
    assert "0xorphan" not in {t.tx_hash_evm for t in second}


def test_tracker_prunes_blocks_outside_check_window():
    tracker = FinalityTracker(confirmation_depth=2, reorg_check_blocks=5)
    tracker.record(10, "0xold", "0xtx")
    calls = []
    orphaned = tracker.check_reorgs(100, lambda n: calls.append(n) or "0xdifferent")
    assert orphaned == {}
    assert calls == []
