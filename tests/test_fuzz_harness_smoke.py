"""Smoke tests for Atheris fuzz harnesses.

These tests verify that each parser targeted by a fuzz harness handles a
variety of fixed sample inputs without unexpected crashes. They call the
real parser functions directly — no Atheris import, no FuzzedDataProvider —
so they run in standard CI without requiring atheris to be installed.

For each crash discovered by the nightly fuzzer, add the minimised reproducer
as a parametrize case in the relevant section below with a comment referencing
the harness that found it (see "Regression cases" at the bottom).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ingestion.data_models import Asset, OrderBookEvent, Trade
from ingestion.solana_adapter import (
    WORMHOLE_CORE,
    _crc16_xmodem,
    _extract_stellar_address_from_vaa,
    _stellar_pubkey_to_address,
)
from ingestion.uniswap_adapter import UniswapV3Adapter
from ingestion.curve_adapter import CurveAdapter


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _call_trade_parser(raw: bytes) -> None:
    """Mirror the logic of fuzz_trade_parser.TestOneInput without Atheris."""
    try:
        text = raw.decode("utf-8", errors="replace")
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return
    try:
        Trade.model_validate(payload)
    except ValidationError:
        pass


def _call_asset_parser(raw: bytes) -> None:
    """Mirror the logic of fuzz_asset_parser.TestOneInput without Atheris."""
    try:
        text = raw.decode("utf-8", errors="replace")
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return
    try:
        Asset.model_validate(payload)
    except ValidationError:
        pass


def _call_orderbook_parser(raw: bytes) -> None:
    """Mirror the logic of fuzz_orderbook_event_parser.TestOneInput without Atheris."""
    try:
        text = raw.decode("utf-8", errors="replace")
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return
    try:
        OrderBookEvent.model_validate(payload)
    except ValidationError:
        pass


# Stub adapters for EVM parser smoke tests — no real RPC URL needed.
_uniswap = UniswapV3Adapter.__new__(UniswapV3Adapter)
_uniswap._rpc_url = "https://stub.invalid"
_uniswap._chain = "ethereum"
_uniswap._linked_wallets = set()

_curve = CurveAdapter.__new__(CurveAdapter)
_curve._rpc_url = "https://stub.invalid"
_curve._chain = "ethereum"
_curve._linked_wallets = set()


def _call_evm_parser(raw: bytes) -> None:
    """Mirror the logic of fuzz_evm_rpc_parser.TestOneInput without Atheris."""
    try:
        text = raw.decode("utf-8", errors="replace")
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    try:
        _uniswap._parse_swap_event(payload)
    except (ValueError, KeyError, IndexError, TypeError, ValidationError):
        pass
    try:
        _curve._parse_exchange_event(payload)
    except (ValueError, KeyError, IndexError, TypeError, ValidationError):
        pass


def _make_vaa_tx(raw_bytes: bytes) -> dict:
    """Wrap bytes in a minimal Solana transaction dict for VAA parsing."""
    import base64
    return {
        "transaction": {
            "message": {
                "accountKeys": [WORMHOLE_CORE],
                "instructions": [
                    {
                        "programIdIndex": 0,
                        "data": base64.b64encode(raw_bytes).decode("ascii"),
                    }
                ],
            }
        }
    }


def _call_solana_parser(raw: bytes) -> None:
    """Mirror the logic of fuzz_solana_vaa_parser.TestOneInput without Atheris."""
    import struct
    # Sub-harness 1: VAA transaction parser
    vaa_bytes = raw[:256]
    tx = _make_vaa_tx(vaa_bytes)
    try:
        _extract_stellar_address_from_vaa(tx)
    except (ValueError, KeyError, IndexError, struct.error):
        pass
    # Sub-harness 2: raw pubkey encoder + CRC
    try:
        _stellar_pubkey_to_address(raw)
    except (ValueError, struct.error):
        pass
    try:
        _crc16_xmodem(raw)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sample inputs
# ---------------------------------------------------------------------------

_VALID_TRADE = json.dumps({
    "id": "1",
    "paging_token": "1-0",
    "ledger_close_time": "2026-06-25T12:30:00Z",
    "base_account": "GBASE",
    "counter_account": "GCOUNTER",
    "base_asset": {"code": "XLM"},
    "counter_asset": {"code": "USDC", "issuer": "GISSUER"},
    "base_amount": "1.5",
    "counter_amount": "3.0",
    "price": "2.0",
    "base_is_seller": True,
}).encode()

_SHARED_INPUTS: list[bytes] = [
    b"",
    b"{}",
    b"[]",
    b"null",
    b'{"id": 123}',
    b'{"base_amount": "inf"}',
    b'{"base_amount": "1e400"}',
    b'{"base_amount": true}',
    b"\xff\xfe",
    b'{"nested": {"a": {"b": {"c": {"d": {}}}}}}',
    _VALID_TRADE,
]

# ---------------------------------------------------------------------------
# fuzz_trade_parser smoke tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sample", _SHARED_INPUTS)
def test_fuzz_trade_parser_smoke(sample: bytes) -> None:
    _call_trade_parser(sample)  # must not raise


# ---------------------------------------------------------------------------
# fuzz_asset_parser smoke tests
# ---------------------------------------------------------------------------

_ASSET_INPUTS: list[bytes] = [
    b'{"code": "XLM"}',
    b'{"code": "USDC", "issuer": "GISSUER"}',
    b'{"code": 123}',
    b'{"code": ""}',
    b'{"code": "USDC"}',
    b"{}",
    b"",
    b"\xff",
] + _SHARED_INPUTS[:4]


@pytest.mark.parametrize("sample", _ASSET_INPUTS)
def test_fuzz_asset_parser_smoke(sample: bytes) -> None:
    _call_asset_parser(sample)  # must not raise


# ---------------------------------------------------------------------------
# fuzz_orderbook_event_parser smoke tests
# ---------------------------------------------------------------------------

_ORDERBOOK_INPUTS: list[bytes] = [
    json.dumps({
        "id": "10",
        "timestamp": "2026-06-25T12:30:00Z",
        "account": "GACCOUNT",
        "asset_pair": "XLM/USDC:GISSUER",
        "side": "sell",
        "amount": "0",
        "price": "0.25",
        "event_type": "cancelled",
    }).encode(),
    b'{"side": "hold"}',
    b'{"offer_id": 0}',
    b'{"price": "nan"}',
    b"{}",
    b"",
]


@pytest.mark.parametrize("sample", _ORDERBOOK_INPUTS)
def test_fuzz_orderbook_event_parser_smoke(sample: bytes) -> None:
    _call_orderbook_parser(sample)  # must not raise


# ---------------------------------------------------------------------------
# fuzz_evm_rpc_parser smoke tests
# ---------------------------------------------------------------------------

_EVM_INPUTS: list[bytes] = [
    b"{}",
    b'{"topics": [], "data": "0x"}',
    # well-formed swap log with 5 topics and 160 zero bytes of data
    json.dumps({
        "topics": [
            "0xabc",
            "0x" + "0" * 64,
            "0x" + "0" * 64,
        ],
        "data": "0x" + "0" * 320,
        "transactionHash": "0x" + "a" * 64,
        "blockNumber": "0x1",
        "address": "0x" + "b" * 40,
    }).encode(),
    b'{"topics": ["0xinvalid"]}',
    b'{"data": "not_hex"}',
    b"",
    b"null",
]


@pytest.mark.parametrize("sample", _EVM_INPUTS)
def test_fuzz_evm_rpc_parser_smoke(sample: bytes) -> None:
    _call_evm_parser(sample)  # must not raise


# ---------------------------------------------------------------------------
# fuzz_solana_vaa_parser smoke tests
# ---------------------------------------------------------------------------

_SOLANA_INPUTS: list[bytes] = [
    b"",
    b"\x00" * 32,
    b"\xff" * 32,
    # minimal VAA skeleton: discriminator(1) + version(1) + guardian_set(4) + num_sigs(1) + body(26)
    b"\x01\x00\x00\x00\x00\x06\x01" + b"\x00" * 66 + b"\x00" * 26,
    bytes(range(256)),
    b"GSTELLARADDRESSTEST",
]


@pytest.mark.parametrize("sample", _SOLANA_INPUTS)
def test_fuzz_solana_vaa_parser_smoke(sample: bytes) -> None:
    _call_solana_parser(sample)  # must not raise


# ---------------------------------------------------------------------------
# Regression cases — add minimised crash reproducers here
# ---------------------------------------------------------------------------
# Pattern:
#   def test_fuzz_trade_parser_regression_issue_NNN() -> None:
#       """Regression: crash found by fuzz_trade_parser, issue #NNN."""
#       _call_trade_parser(b"<minimised bytes>")  # must not raise
