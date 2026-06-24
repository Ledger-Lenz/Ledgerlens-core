"""Tests for ingestion/uniswap_adapter.py."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from ingestion.uniswap_adapter import Trade, UniswapV3Adapter


def test_trade_dataclass_fields():
    t = Trade(
        source="uniswap_v3",
        chain="ethereum",
        tx_hash="0xabc",
        block_number=100,
        block_timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        pool_address="0x" + "A" * 40,
        wallet_address="0x" + "B" * 40,
        token_in="token0",
        token_out="token1",
        amount_in=1.0,
        amount_out=2.0,
    )
    assert t.source == "uniswap_v3"
    assert t.chain == "ethereum"


@patch.dict("os.environ", {"INGEST_UNISWAP": "false"})
def test_fetch_swaps_disabled():
    """When INGEST_UNISWAP is false, fetch_swaps returns empty."""
    import importlib
    import ingestion.uniswap_adapter as mod
    importlib.reload(mod)
    adapter = mod.UniswapV3Adapter.__new__(mod.UniswapV3Adapter)
    adapter.chain = "ethereum"
    adapter._loader = MagicMock()
    result = adapter.fetch_swaps()
    assert result == []
    adapter._loader.load_trades.assert_not_called()


def test_fetch_swaps_filters_linked_wallets():
    """Only trades from linked EVM wallets are returned."""
    from ingestion.evm_loader import CrossChainTrade

    mock_loader = MagicMock()
    mock_loader.load_trades.return_value = [
        CrossChainTrade(
            chain="ethereum",
            tx_hash="0x1",
            block_number=1,
            block_timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            pool_address="0x" + "A" * 40,
            wallet_address="0x" + "1" * 40,
            token_in="token0",
            token_out="token1",
            amount_in=1.0,
            amount_out=2.0,
        ),
        CrossChainTrade(
            chain="ethereum",
            tx_hash="0x2",
            block_number=2,
            block_timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            pool_address="0x" + "A" * 40,
            wallet_address="0x" + "2" * 40,
            token_in="token0",
            token_out="token1",
            amount_in=3.0,
            amount_out=4.0,
        ),
    ]

    adapter = UniswapV3Adapter.__new__(UniswapV3Adapter)
    adapter.chain = "ethereum"
    adapter._loader = mock_loader

    linked = {"0x" + "1" * 40}
    with patch("ingestion.uniswap_adapter.INGEST_UNISWAP", True):
        trades = adapter.fetch_swaps(linked_evm_wallets=linked)

    assert len(trades) == 1
    assert trades[0].wallet_address == "0x" + "1" * 40
    assert trades[0].source == "uniswap_v3"
