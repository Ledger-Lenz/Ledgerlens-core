"""Tests for ingestion/evm_loader.py."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest
import responses as responses_lib

from ingestion.evm_loader import (
    UNISWAP_V3_SWAP_TOPIC,
    CrossChainTrade,
    EVMTradeLoader,
    _TokenBucket,
    _validate_evm_address,
)

MOCK_RPC_URL = "https://mock-rpc.example.com"
VALID_POOL = "0xCBCdF9626bC03E24f779434178A73a0B4bad62eD"

# Pre-built Uniswap V3 Swap log for pool 0xCBCdF9626...
# amount0 = -1000000000000000000 (negative, so token0 is out, token1 is in)
# amount1 = +500000000000000000
_AMOUNT0 = (-1_000_000_000_000_000_000).to_bytes(32, "big", signed=True).hex()
_AMOUNT1 = (500_000_000_000_000_000).to_bytes(32, "big", signed=True).hex()
_SQRT_PRICE = (0).to_bytes(32, "big").hex()
_LIQUIDITY = (0).to_bytes(32, "big").hex()
_TICK = (0).to_bytes(32, "big", signed=True).hex()

MOCK_SWAP_LOG = {
    "topics": [
        UNISWAP_V3_SWAP_TOPIC,
        "0x000000000000000000000000ab5801a7d398351b8be11c439e05c5b3259aec9b",
        "0x000000000000000000000000ab5801a7d398351b8be11c439e05c5b3259aec9b",
    ],
    "data": "0x" + _AMOUNT0 + _AMOUNT1 + _SQRT_PRICE + _LIQUIDITY + _TICK,
    "transactionHash": "0xdeadbeef00000000000000000000000000000000000000000000000000000001",
    "blockNumber": "0x1",
    "address": VALID_POOL,
    "logIndex": "0x0",
    "transactionIndex": "0x0",
    "blockHash": "0xblockhash",
    "removed": False,
}

# 2026-06-19 in Unix epoch seconds
_RECENT_TS = 1_781_827_200

MOCK_BLOCK = {
    "result": {
        "timestamp": hex(_RECENT_TS),
        "number": "0x1",
    }
}


def _rpc_response(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


# ---------------------------------------------------------------------------
# (a) EVMTradeLoader correctly parses a mocked Swap event into CrossChainTrade
# ---------------------------------------------------------------------------


@responses_lib.activate
def test_parse_v3_swap_event_into_cross_chain_trade():
    """A well-formed V3 Swap log is parsed into a CrossChainTrade."""
    responses_lib.add(
        responses_lib.POST,
        MOCK_RPC_URL,
        json=_rpc_response("0x64"),  # eth_blockNumber → 100
        match_querystring=False,
    )
    responses_lib.add(
        responses_lib.POST,
        MOCK_RPC_URL,
        json=_rpc_response([MOCK_SWAP_LOG]),  # eth_getLogs
        match_querystring=False,
    )
    responses_lib.add(
        responses_lib.POST,
        MOCK_RPC_URL,
        json=MOCK_BLOCK,  # eth_getBlockByNumber
        match_querystring=False,
    )

    loader = EVMTradeLoader(
        chain="ethereum",
        rpc_url=MOCK_RPC_URL,
        pool_addresses=[VALID_POOL],
    )
    trades = loader.load_trades(lookback_blocks=10)

    assert len(trades) == 1
    trade = trades[0]
    assert isinstance(trade, CrossChainTrade)
    assert trade.chain == "ethereum"
    assert trade.tx_hash == MOCK_SWAP_LOG["transactionHash"]
    assert trade.block_number == 1
    assert trade.pool_address == VALID_POOL
    assert trade.wallet_address == "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
    # amount0 < 0 → token0 is out, token1 in
    assert trade.token_in == "token1"
    assert trade.token_out == "token0"
    assert abs(trade.amount_in - 0.5) < 1e-9
    assert abs(trade.amount_out - 1.0) < 1e-9
    assert trade.block_timestamp == datetime.fromtimestamp(_RECENT_TS, tz=timezone.utc)


# ---------------------------------------------------------------------------
# (b) 429 response triggers retry with exponential backoff
# ---------------------------------------------------------------------------


@responses_lib.activate
def test_429_triggers_retry_with_exponential_backoff():
    """Two 429 responses followed by success; loader retries and succeeds."""
    responses_lib.add(responses_lib.POST, MOCK_RPC_URL, status=429)
    responses_lib.add(responses_lib.POST, MOCK_RPC_URL, status=429)
    responses_lib.add(
        responses_lib.POST,
        MOCK_RPC_URL,
        json=_rpc_response("0x64"),
    )

    sleep_calls = []
    with patch("ingestion.evm_loader.time.sleep", side_effect=sleep_calls.append):
        loader = EVMTradeLoader(chain="ethereum", rpc_url=MOCK_RPC_URL)
        result = loader._rpc_call("eth_blockNumber", [])

    assert result["result"] == "0x64"
    # Should have slept twice: after 1st 429 (1s) and 2nd 429 (2s)
    assert len(sleep_calls) == 2
    assert sleep_calls[0] == 1  # 2**0
    assert sleep_calls[1] == 2  # 2**1


def test_429_exhausts_retries_raises():
    """Three consecutive 429 responses raise after max retries."""
    with responses_lib.RequestsMock() as rsps:
        for _ in range(4):
            rsps.add(rsps.POST, MOCK_RPC_URL, status=429)

        with patch("ingestion.evm_loader.time.sleep"):
            loader = EVMTradeLoader(chain="ethereum", rpc_url=MOCK_RPC_URL)
            with pytest.raises(Exception):
                loader._rpc_call("eth_blockNumber", [], max_retries=3)


# ---------------------------------------------------------------------------
# (c) Malformed event data raises a descriptive error, not a KeyError
# ---------------------------------------------------------------------------


def test_malformed_swap_event_raises_descriptive_error():
    """Missing or short data field raises ValueError with a helpful message."""
    loader = EVMTradeLoader(chain="ethereum", rpc_url=MOCK_RPC_URL)
    block_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)

    # data field too short (less than 128 hex chars for amount0/amount1)
    bad_log = {
        **MOCK_SWAP_LOG,
        "data": "0x" + "aa" * 30,  # only 30 bytes, not enough
    }
    with pytest.raises(ValueError, match="too short|Failed to parse"):
        loader._parse_v3_swap(bad_log, VALID_POOL, block_ts)


def test_missing_topics_raises_descriptive_error():
    """A log with no topics raises ValueError, not a KeyError."""
    loader = EVMTradeLoader(chain="ethereum", rpc_url=MOCK_RPC_URL)
    block_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)

    bad_log = {
        "topics": [],  # empty — no sender topic
        "data": "0x" + "00" * 160,
        "transactionHash": "0xdeadbeef",
        "blockNumber": "0x1",
    }
    with pytest.raises(ValueError, match="Failed to parse|missing field"):
        loader._parse_v3_swap(bad_log, VALID_POOL, block_ts)


# ---------------------------------------------------------------------------
# Rate limiting — token bucket
# ---------------------------------------------------------------------------


def test_token_bucket_acquires_without_sleep():
    """Immediate first acquisition never sleeps (tokens start full)."""
    bucket = _TokenBucket(rate=10.0)
    with patch("ingestion.evm_loader.time.sleep") as mock_sleep:
        bucket.acquire()
    mock_sleep.assert_not_called()


def test_token_bucket_sleeps_when_empty():
    """Draining the bucket causes the next acquire to sleep."""
    bucket = _TokenBucket(rate=10.0)
    bucket._tokens = 0.0  # pre-drain
    with patch("ingestion.evm_loader.time.sleep") as mock_sleep:
        bucket.acquire()
    mock_sleep.assert_called_once()
    sleep_time = mock_sleep.call_args[0][0]
    assert sleep_time > 0


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------


def test_validate_evm_address_checksums_non_checksummed():
    result = _validate_evm_address("0xab5801a7d398351b8be11c439e05c5b3259aec9b")
    assert result == "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"


def test_validate_evm_address_rejects_malformed():
    with pytest.raises(ValueError, match="Malformed"):
        _validate_evm_address("not-an-address")


def test_validate_evm_address_rejects_too_short():
    with pytest.raises(ValueError):
        _validate_evm_address("0xABCD")


def test_unsupported_chain_raises():
    with pytest.raises(ValueError, match="Unsupported chain"):
        EVMTradeLoader(chain="solana", rpc_url=MOCK_RPC_URL)
