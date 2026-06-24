"""Uniswap V3 Swap event adapter for cross-chain wash-trade detection.

Ingests ``Swap(address,address,int256,int256,uint160,uint128,int24)`` events
from Uniswap V3 pools on EVM chains, filtering to wallets linked to Stellar
accounts via the bridge event graph.  Mapped events are emitted as canonical
``Trade`` dataclass instances with ``source="uniswap_v3"``.

Enabled via ``INGEST_UNISWAP=true`` environment variable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from web3 import Web3

from config.settings import settings
from ingestion.evm_loader import (
    UNISWAP_V3_SWAP_TOPIC,
    CrossChainTrade,
    EVMTradeLoader,
)

logger = logging.getLogger("ledgerlens.uniswap_adapter")

INGEST_UNISWAP = os.getenv("INGEST_UNISWAP", "false").lower() in ("true", "1", "yes")


@dataclass
class Trade:
    """Canonical trade representation for cross-chain detection graph."""

    source: str
    chain: str
    tx_hash: str
    block_number: int
    block_timestamp: datetime
    pool_address: str
    wallet_address: str
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float


class UniswapV3Adapter:
    """Fetch and filter Uniswap V3 Swap events for bridge-linked wallets."""

    def __init__(
        self,
        chain: str = "ethereum",
        rpc_url: Optional[str] = None,
        pool_addresses: Optional[list[str]] = None,
    ) -> None:
        self.chain = chain
        rpc_url = rpc_url or self._default_rpc(chain)
        self._loader = EVMTradeLoader(
            chain=chain,
            rpc_url=rpc_url,
            pool_addresses=pool_addresses or list(settings.evm_pool_addresses),
        )

    @staticmethod
    def _default_rpc(chain: str) -> str:
        return {
            "ethereum": settings.evm_rpc_ethereum,
            "base": settings.evm_rpc_base,
            "polygon": settings.evm_rpc_polygon,
        }.get(chain, settings.evm_rpc_ethereum)

    def fetch_swaps(
        self,
        lookback_blocks: Optional[int] = None,
        linked_evm_wallets: Optional[set[str]] = None,
    ) -> list[Trade]:
        """Fetch Uniswap V3 Swap events, optionally filtering to bridge-linked wallets.

        Args:
            lookback_blocks: Number of blocks to scan back (defaults to settings).
            linked_evm_wallets: Set of EIP-55 checksummed EVM addresses linked
                to Stellar wallets via bridge events.  When provided, only
                swaps involving these wallets are returned.

        Returns:
            List of canonical Trade objects with source="uniswap_v3".
        """
        if not INGEST_UNISWAP:
            logger.debug("INGEST_UNISWAP is disabled; skipping Uniswap V3 ingestion")
            return []

        raw_trades = self._loader.load_trades(lookback_blocks=lookback_blocks)
        trades: list[Trade] = []
        for ct in raw_trades:
            if linked_evm_wallets and ct.wallet_address not in linked_evm_wallets:
                continue
            trades.append(
                Trade(
                    source="uniswap_v3",
                    chain=ct.chain,
                    tx_hash=ct.tx_hash,
                    block_number=ct.block_number,
                    block_timestamp=ct.block_timestamp,
                    pool_address=ct.pool_address,
                    wallet_address=ct.wallet_address,
                    token_in=ct.token_in,
                    token_out=ct.token_out,
                    amount_in=ct.amount_in,
                    amount_out=ct.amount_out,
                )
            )

        logger.info(
            "Fetched %d Uniswap V3 swaps on %s (%d after wallet filter)",
            len(raw_trades),
            self.chain,
            len(trades),
        )
        return trades
