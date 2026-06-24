"""Tests for ingestion/curve_adapter.py."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from ingestion.curve_adapter import CurveAdapter, CURVE_TOKEN_EXCHANGE_TOPIC


def test_curve_topic_is_hex():
    assert CURVE_TOKEN_EXCHANGE_TOPIC.startswith("0x")
    assert len(CURVE_TOKEN_EXCHANGE_TOPIC) == 66


@patch.dict("os.environ", {"INGEST_CURVE": "false"})
def test_fetch_swaps_disabled():
    import importlib
    import ingestion.curve_adapter as mod
    importlib.reload(mod)
    adapter = mod.CurveAdapter.__new__(mod.CurveAdapter)
    adapter.chain = "ethereum"
    adapter._rpc_url = "http://localhost"
    adapter.pool_addresses = []
    result = adapter.fetch_swaps()
    assert result == []


def test_parse_token_exchange_valid():
    adapter = CurveAdapter.__new__(CurveAdapter)
    adapter.chain = "ethereum"

    buyer_topic = "0x" + "00" * 12 + "ab" * 20
    sold_id = (0).to_bytes(32, "big").hex()
    tokens_sold = (1000000000000000000).to_bytes(32, "big").hex()  # 1e18
    bought_id = (1).to_bytes(32, "big").hex()
    tokens_bought = (2000000000000000000).to_bytes(32, "big").hex()  # 2e18
    data = "0x" + sold_id + tokens_sold + bought_id + tokens_bought

    log = {
        "topics": [CURVE_TOKEN_EXCHANGE_TOPIC, buyer_topic],
        "data": data,
        "transactionHash": "0xdeadbeef",
        "blockNumber": hex(100),
    }
    pool = "0x" + "CC" * 20
    ts = datetime(2025, 6, 1, tzinfo=timezone.utc)

    trade = adapter._parse_token_exchange(log, pool, ts)
    assert trade.source == "curve"
    assert trade.token_in == "coin_0"
    assert trade.token_out == "coin_1"
    assert trade.amount_in == 1.0
    assert trade.amount_out == 2.0


def test_fetch_swaps_filters_linked_wallets():
    adapter = CurveAdapter.__new__(CurveAdapter)
    adapter.chain = "ethereum"
    adapter._rpc_url = "http://localhost"
    adapter.pool_addresses = ["0x" + "AA" * 20]

    buyer_1 = "0x" + "00" * 12 + "11" * 20
    buyer_2 = "0x" + "00" * 12 + "22" * 20
    data_word = lambda v: v.to_bytes(32, "big").hex()

    base_data = "0x" + data_word(0) + data_word(10**18) + data_word(1) + data_word(10**18)

    logs = [
        {"topics": [CURVE_TOKEN_EXCHANGE_TOPIC, buyer_1], "data": base_data,
         "transactionHash": "0x1", "blockNumber": hex(10)},
        {"topics": [CURVE_TOKEN_EXCHANGE_TOPIC, buyer_2], "data": base_data,
         "transactionHash": "0x2", "blockNumber": hex(11)},
    ]

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    adapter._get_latest_block = MagicMock(return_value=100)
    adapter._get_logs = MagicMock(return_value=logs)
    adapter._get_block_timestamp = MagicMock(return_value=ts)

    from web3 import Web3
    linked = {Web3.to_checksum_address("0x" + "11" * 20)}

    with patch("ingestion.curve_adapter.INGEST_CURVE", True):
        trades = adapter.fetch_swaps(linked_evm_wallets=linked)

    assert len(trades) == 1
    assert trades[0].wallet_address == Web3.to_checksum_address("0x" + "11" * 20)
