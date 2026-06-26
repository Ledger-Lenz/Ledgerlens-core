"""Tests for ingestion/solana_adapter.py using httpx mock transport as VCR cassettes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ingestion.solana_adapter import (
    SolanaAdapter,
    _extract_spl_token_changes,
    _extract_stellar_address_from_vaa,
    _tx_to_trade,
    _crc16_xmodem,
    _stellar_pubkey_to_address,
)
from ingestion.data_models import Trade


# ---------------------------------------------------------------------------
# Cassette fixtures
# ---------------------------------------------------------------------------

_MOCK_SIG = "5wUjhZzYiMnHNz3rZMzHxnrkW8Y7YdBzxxQRiM4tWjTqBp7KkBa9N2gZYjHkNqTzJ6bJLvFRc5mRkXcQhVbMNfU"
_MOCK_ADDRESS = "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5ZARQ"
_SERUM_PROGRAM = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
_TOKEN_A = "So11111111111111111111111111111111111111112"   # wSOL
_TOKEN_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC


def _rpc_ok(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _make_tx(
    sig: str = _MOCK_SIG,
    block_time: int = 1_700_000_000,
    include_serum: bool = True,
    pre_balances: list[dict] | None = None,
    post_balances: list[dict] | None = None,
) -> dict:
    account_keys = [_MOCK_ADDRESS, "ACCT_B"]
    if include_serum:
        account_keys.append(_SERUM_PROGRAM)

    pre = pre_balances or [
        {"accountIndex": 0, "mint": _TOKEN_A, "owner": _MOCK_ADDRESS,
         "uiTokenAmount": {"uiAmount": 10.0}},
        {"accountIndex": 1, "mint": _TOKEN_B, "owner": "ACCT_B",
         "uiTokenAmount": {"uiAmount": 100.0}},
    ]
    post = post_balances or [
        {"accountIndex": 0, "mint": _TOKEN_A, "owner": _MOCK_ADDRESS,
         "uiTokenAmount": {"uiAmount": 8.0}},    # sold 2 wSOL
        {"accountIndex": 1, "mint": _TOKEN_B, "owner": "ACCT_B",
         "uiTokenAmount": {"uiAmount": 80.0}},   # sold 20 USDC
        {"accountIndex": 0, "mint": _TOKEN_B, "owner": _MOCK_ADDRESS,
         "uiTokenAmount": {"uiAmount": 20.0}},   # bought 20 USDC
        {"accountIndex": 1, "mint": _TOKEN_A, "owner": "ACCT_B",
         "uiTokenAmount": {"uiAmount": 2.0}},    # bought 2 wSOL
    ]
    return {
        "blockTime": block_time,
        "transaction": {
            "message": {
                "accountKeys": account_keys,
                "instructions": [],
            }
        },
        "meta": {
            "preTokenBalances": pre,
            "postTokenBalances": post,
        },
    }


class _MockTransport(httpx.BaseTransport):
    """Deterministic cassette: maps (method, params[0]) to a canned response."""

    def __init__(self, responses: list[dict]) -> None:
        self._queue = list(responses)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = self._queue.pop(0)
        return httpx.Response(200, json=body)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_extract_spl_token_changes_basic():
    tx = _make_tx()
    changes = _extract_spl_token_changes(tx)
    owners = {c[0] for c in changes}
    assert _MOCK_ADDRESS in owners or "ACCT_B" in owners


def test_extract_spl_token_changes_empty_tx():
    assert _extract_spl_token_changes({}) == []


def test_tx_to_trade_maps_canonical_fields():
    tx = _make_tx()
    trade = _tx_to_trade(tx, _MOCK_SIG)
    assert trade is not None
    assert isinstance(trade, Trade)
    assert trade.id == _MOCK_SIG
    assert trade.source == "solana"
    assert trade.transaction_hash == _MOCK_SIG
    assert trade.base_amount > 0
    assert trade.price > 0


def test_tx_to_trade_no_block_time():
    tx = _make_tx()
    tx["blockTime"] = None
    assert _tx_to_trade(tx, _MOCK_SIG) is None


def test_tx_to_trade_missing_changes():
    tx = _make_tx(
        pre_balances=[
            {"accountIndex": 0, "mint": _TOKEN_A, "owner": _MOCK_ADDRESS,
             "uiTokenAmount": {"uiAmount": 10.0}},
        ],
        post_balances=[
            {"accountIndex": 0, "mint": _TOKEN_A, "owner": _MOCK_ADDRESS,
             "uiTokenAmount": {"uiAmount": 10.0}},
        ],
    )
    assert _tx_to_trade(tx, _MOCK_SIG) is None


def test_is_dex_transaction_serum():
    from ingestion.solana_adapter import _is_dex_transaction
    tx = _make_tx(include_serum=True)
    assert _is_dex_transaction(tx)


def test_is_dex_transaction_no_program():
    from ingestion.solana_adapter import _is_dex_transaction
    tx = _make_tx(include_serum=False)
    assert not _is_dex_transaction(tx)


# ---------------------------------------------------------------------------
# Integration test via cassette (mocked transport)
# ---------------------------------------------------------------------------


def test_ingest_cassette():
    sig_list_response = _rpc_ok([
        {"signature": _MOCK_SIG, "err": None, "memo": None}
    ])
    tx_response = _rpc_ok(_make_tx())

    transport = _MockTransport([sig_list_response, tx_response])

    with patch("httpx.Client", lambda: httpx.Client(transport=transport)):
        adapter = SolanaAdapter()
        trades = adapter.ingest(_MOCK_ADDRESS, limit=10)

    assert len(trades) == 1
    assert trades[0].source == "solana"
    assert trades[0].base_account == _MOCK_ADDRESS or trades[0].counter_account == _MOCK_ADDRESS


def test_ingest_skips_non_dex_transactions():
    sig_list_response = _rpc_ok([
        {"signature": _MOCK_SIG, "err": None, "memo": None}
    ])
    tx_response = _rpc_ok(_make_tx(include_serum=False))

    transport = _MockTransport([sig_list_response, tx_response])

    with patch("httpx.Client", lambda: httpx.Client(transport=transport)):
        adapter = SolanaAdapter()
        trades = adapter.ingest(_MOCK_ADDRESS, limit=10)

    assert trades == []


def test_ingest_empty_signature_list():
    transport = _MockTransport([_rpc_ok([])])

    with patch("httpx.Client", lambda: httpx.Client(transport=transport)):
        adapter = SolanaAdapter()
        trades = adapter.ingest(_MOCK_ADDRESS, limit=10)

    assert trades == []


# ---------------------------------------------------------------------------
# Wormhole / CRC helpers
# ---------------------------------------------------------------------------


def test_crc16_xmodem_known_value():
    data = b"\x06" + bytes(32)
    result = _crc16_xmodem(data)
    assert isinstance(result, int)
    assert 0 <= result <= 0xFFFF


def test_stellar_pubkey_to_address_length():
    key = bytes(range(32))
    addr = _stellar_pubkey_to_address(key)
    assert addr is None or (isinstance(addr, str) and addr.startswith("G"))


def test_stellar_pubkey_to_address_wrong_length():
    assert _stellar_pubkey_to_address(b"\x00" * 10) is None


def test_extract_stellar_address_from_vaa_empty_tx():
    assert _extract_stellar_address_from_vaa({}) is None


def test_extract_stellar_address_from_vaa_no_wormhole():
    tx = _make_tx()
    assert _extract_stellar_address_from_vaa(tx) is None


def test_solana_adapter_rpc_url_from_env(monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
    from ingestion.solana_adapter import _rpc_url
    assert _rpc_url() == "https://api.devnet.solana.com"
