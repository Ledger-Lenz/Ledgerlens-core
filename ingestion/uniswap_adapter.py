"""Uniswap V3 Swap event ingestion for cross-chain wash-trade detection.

Consumes Swap(address,address,int256,int256,uint160,uint128,int24) events
from Uniswap V3 pools on EVM chains and maps them to canonical Trade records.
Only processes events for wallets linked to Stellar accounts via the bridge
event graph, extending the cross-chain detection surface.

Feature-flagged via INGEST_UNISWAP=true.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from web3 import Web3


logger = logging.getLogger("ledgerlens.uniswap_adapter")

UNISWAP_V3_SWAP_TOPIC = "0x" + Web3.keccak(
    text="Swap(address,address,int256,int256,uint160,uint128,int24)"
).hex()


def _is_enabled() -> bool:
    return os.getenv("INGEST_UNISWAP", "").lower() in ("true", "1", "yes")


@dataclass
class Trade:
    """Canonical trade record from a Uniswap V3 swap event."""

    source: str
    chain: str
    tx_hash: str
    block_number: int
    block_timestamp: datetime
    pool_address: str
    sender: str
    recipient: str
    amount0: int
    amount1: int
    sqrt_price_x96: int
    liquidity: int
    tick: int


class UniswapV3Adapter:
    """Ingest Uniswap V3 Swap events for wallets linked to Stellar accounts."""

    def __init__(self, rpc_url: str, chain: str, linked_wallets: set[str] | None = None):
        self._rpc_url = rpc_url
        self._chain = chain
        self._linked_wallets: set[str] = {w.lower() for w in (linked_wallets or set())}

    def set_linked_wallets(self, wallets: set[str]) -> None:
        self._linked_wallets = {w.lower() for w in wallets}

    def _fetch_logs(self, pool_addresses: list[str], from_block: int, to_block: int) -> list[dict]:
        """Fetch Swap event logs from the RPC endpoint."""
        import requests

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": pool_addresses,
                "topics": [UNISWAP_V3_SWAP_TOPIC],
            }],
        }
        resp = requests.post(self._rpc_url, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"RPC error: {result['error']}")
        return result.get("result", [])

    def _parse_swap_event(self, log: dict) -> Trade | None:
        """Parse a raw Swap log entry into a Trade dataclass."""
        topics = log.get("topics", [])
        if len(topics) < 3:
            return None

        sender = "0x" + topics[1][-40:]
        recipient = "0x" + topics[2][-40:]

        if self._linked_wallets:
            if sender.lower() not in self._linked_wallets and recipient.lower() not in self._linked_wallets:
                return None

        data = bytes.fromhex(log["data"][2:])
        amount0 = int.from_bytes(data[0:32], "big", signed=True)
        amount1 = int.from_bytes(data[32:64], "big", signed=True)
        sqrt_price_x96 = int.from_bytes(data[64:96], "big", signed=False)
        liquidity = int.from_bytes(data[96:128], "big", signed=False)
        tick = int.from_bytes(data[128:160], "big", signed=True)

        return Trade(
            source="uniswap_v3",
            chain=self._chain,
            tx_hash=log["transactionHash"],
            block_number=int(log["blockNumber"], 16),
            block_timestamp=datetime.now(timezone.utc),
            pool_address=log["address"],
            sender=Web3.to_checksum_address(sender),
            recipient=Web3.to_checksum_address(recipient),
            amount0=amount0,
            amount1=amount1,
            sqrt_price_x96=sqrt_price_x96,
            liquidity=liquidity,
            tick=tick,
        )

    def fetch_swaps(
        self, pool_addresses: list[str], from_block: int, to_block: int
    ) -> list[Trade]:
        """Fetch and parse Uniswap V3 Swap events, filtered to linked wallets."""
        if not _is_enabled():
            logger.debug("Uniswap ingestion disabled (INGEST_UNISWAP != true)")
            return []

        logs = self._fetch_logs(pool_addresses, from_block, to_block)
        trades = []
        for log in logs:
            trade = self._parse_swap_event(log)
            if trade is not None:
                trades.append(trade)

        logger.info(
            "Fetched %d Uniswap V3 swaps (%d linked) on %s [%d..%d]",
            len(logs), len(trades), self._chain, from_block, to_block,
        )
        return trades
