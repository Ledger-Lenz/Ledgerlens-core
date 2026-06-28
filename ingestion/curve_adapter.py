"""Curve Finance TokenExchange event ingestion for cross-chain wash-trade detection.

Consumes TokenExchange(address,int128,uint256,int128,uint256) events from
major Curve pools on EVM chains and maps them to canonical Trade records.
Only processes events for wallets linked to Stellar accounts via the bridge
event graph.

Feature-flagged via INGEST_CURVE=true.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from web3 import Web3


logger = logging.getLogger("ledgerlens.curve_adapter")

CURVE_TOKEN_EXCHANGE_TOPIC = "0x" + Web3.keccak(
    text="TokenExchange(address,int128,uint256,int128,uint256)"
).hex()


def _is_enabled() -> bool:
    return os.getenv("INGEST_CURVE", "").lower() in ("true", "1", "yes")


@dataclass
class Trade:
    """Canonical trade record from a Curve TokenExchange event."""

    source: str
    chain: str
    tx_hash: str
    block_number: int
    block_timestamp: datetime
    pool_address: str
    buyer: str
    sold_id: int
    tokens_sold: int
    bought_id: int
    tokens_bought: int


class CurveAdapter:
    """Ingest Curve TokenExchange events for wallets linked to Stellar accounts."""

    def __init__(self, rpc_url: str, chain: str, linked_wallets: set[str] | None = None):
        self._rpc_url = rpc_url
        self._chain = chain
        self._linked_wallets: set[str] = {w.lower() for w in (linked_wallets or set())}

    def set_linked_wallets(self, wallets: set[str]) -> None:
        self._linked_wallets = {w.lower() for w in wallets}

    def _fetch_logs(self, pool_addresses: list[str], from_block: int, to_block: int) -> list[dict]:
        """Fetch TokenExchange event logs from the RPC endpoint."""
        import requests

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": pool_addresses,
                "topics": [CURVE_TOKEN_EXCHANGE_TOPIC],
            }],
        }
        resp = requests.post(self._rpc_url, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"RPC error: {result['error']}")
        return result.get("result", [])

    def _parse_exchange_event(self, log: dict) -> Trade | None:
        """Parse a raw TokenExchange log entry into a Trade dataclass."""
        topics = log.get("topics", [])
        if len(topics) < 2:
            return None

        buyer = "0x" + topics[1][-40:]

        if self._linked_wallets:
            if buyer.lower() not in self._linked_wallets:
                return None

        data = bytes.fromhex(log["data"][2:])
        sold_id = int.from_bytes(data[0:32], "big", signed=True)
        tokens_sold = int.from_bytes(data[32:64], "big", signed=False)
        bought_id = int.from_bytes(data[64:96], "big", signed=True)
        tokens_bought = int.from_bytes(data[96:128], "big", signed=False)

        return Trade(
            source="curve",
            chain=self._chain,
            tx_hash=log["transactionHash"],
            block_number=int(log["blockNumber"], 16),
            block_timestamp=datetime.now(timezone.utc),
            pool_address=log["address"],
            buyer=Web3.to_checksum_address(buyer),
            sold_id=sold_id,
            tokens_sold=tokens_sold,
            bought_id=bought_id,
            tokens_bought=tokens_bought,
        )

    def fetch_exchanges(
        self, pool_addresses: list[str], from_block: int, to_block: int
    ) -> list[Trade]:
        """Fetch and parse Curve TokenExchange events, filtered to linked wallets."""
        if not _is_enabled():
            logger.debug("Curve ingestion disabled (INGEST_CURVE != true)")
            return []

        logs = self._fetch_logs(pool_addresses, from_block, to_block)
        trades = []
        for log in logs:
            trade = self._parse_exchange_event(log)
            if trade is not None:
                trades.append(trade)

        logger.info(
            "Fetched %d Curve exchanges (%d linked) on %s [%d..%d]",
            len(logs), len(trades), self._chain, from_block, to_block,
        )
        return trades
