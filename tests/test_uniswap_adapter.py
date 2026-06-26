"""Tests for Uniswap V3 Swap event adapter."""

import os
from unittest.mock import patch

import pytest

from ingestion.uniswap_adapter import UniswapV3Adapter, Trade, _is_enabled


class TestFeatureFlag:
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("INGEST_UNISWAP", None)
            assert not _is_enabled()

    def test_enabled_when_true(self):
        with patch.dict(os.environ, {"INGEST_UNISWAP": "true"}):
            assert _is_enabled()

    def test_fetch_returns_empty_when_disabled(self):
        adapter = UniswapV3Adapter("http://localhost:8545", "ethereum")
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("INGEST_UNISWAP", None)
            result = adapter.fetch_swaps(["0x" + "0" * 40], 100, 200)
            assert result == []


class TestLinkedWalletFiltering:
    def test_set_linked_wallets_lowercases(self):
        adapter = UniswapV3Adapter("http://localhost:8545", "ethereum")
        adapter.set_linked_wallets({"0xABCDEF1234567890abcdef1234567890ABCDEF12"})
        assert "0xabcdef1234567890abcdef1234567890abcdef12" in adapter._linked_wallets

    def test_parse_filters_unlinked_wallets(self):
        adapter = UniswapV3Adapter(
            "http://localhost:8545", "ethereum",
            linked_wallets={"0x1111111111111111111111111111111111111111"},
        )
        log = {
            "topics": [
                "0x" + "00" * 32,
                "0x" + "00" * 12 + "2222222222222222222222222222222222222222",
                "0x" + "00" * 12 + "3333333333333333333333333333333333333333",
            ],
            "data": "0x" + "00" * 160,
            "transactionHash": "0x" + "aa" * 32,
            "blockNumber": "0x64",
            "address": "0x" + "00" * 20,
        }
        result = adapter._parse_swap_event(log)
        assert result is None


class TestParseSwapEvent:
    def test_parses_valid_log(self):
        adapter = UniswapV3Adapter("http://localhost:8545", "ethereum")
        sender = "1111111111111111111111111111111111111111"
        recipient = "2222222222222222222222222222222222222222"
        log = {
            "topics": [
                "0x" + "00" * 32,
                "0x" + "00" * 12 + sender,
                "0x" + "00" * 12 + recipient,
            ],
            "data": "0x" + "00" * 160,
            "transactionHash": "0xabcd",
            "blockNumber": "0x100",
            "address": "0x" + "44" * 20,
        }
        trade = adapter._parse_swap_event(log)
        assert trade is not None
        assert trade.source == "uniswap_v3"
        assert trade.chain == "ethereum"

    def test_returns_none_for_short_topics(self):
        adapter = UniswapV3Adapter("http://localhost:8545", "ethereum")
        log = {"topics": ["0x" + "00" * 32], "data": "0x" + "00" * 160}
        assert adapter._parse_swap_event(log) is None
