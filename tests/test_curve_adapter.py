"""Tests for Curve TokenExchange event adapter."""

import os
from unittest.mock import patch

import pytest

from ingestion.curve_adapter import CurveAdapter, Trade, _is_enabled


class TestFeatureFlag:
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("INGEST_CURVE", None)
            assert not _is_enabled()

    def test_enabled_when_true(self):
        with patch.dict(os.environ, {"INGEST_CURVE": "true"}):
            assert _is_enabled()

    def test_fetch_returns_empty_when_disabled(self):
        adapter = CurveAdapter("http://localhost:8545", "ethereum")
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("INGEST_CURVE", None)
            result = adapter.fetch_exchanges(["0x" + "0" * 40], 100, 200)
            assert result == []


class TestLinkedWalletFiltering:
    def test_set_linked_wallets_lowercases(self):
        adapter = CurveAdapter("http://localhost:8545", "ethereum")
        adapter.set_linked_wallets({"0xABCDEF1234567890abcdef1234567890ABCDEF12"})
        assert "0xabcdef1234567890abcdef1234567890abcdef12" in adapter._linked_wallets

    def test_parse_filters_unlinked_wallets(self):
        adapter = CurveAdapter(
            "http://localhost:8545", "ethereum",
            linked_wallets={"0x1111111111111111111111111111111111111111"},
        )
        buyer = "2222222222222222222222222222222222222222"
        log = {
            "topics": [
                "0x" + "00" * 32,
                "0x" + "00" * 12 + buyer,
            ],
            "data": "0x" + "00" * 128,
            "transactionHash": "0x" + "aa" * 32,
            "blockNumber": "0x64",
            "address": "0x" + "00" * 20,
        }
        result = adapter._parse_exchange_event(log)
        assert result is None


class TestParseExchangeEvent:
    def test_parses_valid_log(self):
        adapter = CurveAdapter("http://localhost:8545", "ethereum")
        buyer = "1111111111111111111111111111111111111111"
        log = {
            "topics": [
                "0x" + "00" * 32,
                "0x" + "00" * 12 + buyer,
            ],
            "data": "0x" + "00" * 128,
            "transactionHash": "0xabcd",
            "blockNumber": "0x100",
            "address": "0x" + "44" * 20,
        }
        trade = adapter._parse_exchange_event(log)
        assert trade is not None
        assert trade.source == "curve"
        assert trade.chain == "ethereum"

    def test_returns_none_for_short_topics(self):
        adapter = CurveAdapter("http://localhost:8545", "ethereum")
        log = {"topics": ["0x" + "00" * 32], "data": "0x" + "00" * 128}
        assert adapter._parse_exchange_event(log) is None
