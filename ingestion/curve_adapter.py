"""Curve Finance TokenExchange event adapter for cross-chain wash-trade detection.

Ingests ``TokenExchange(address,int128,uint256,int128,uint256)`` events from
major Curve pools on EVM chains, filtering to wallets linked to Stellar
accounts via the bridge event graph.  Mapped events are emitted as canonical
``Trade`` dataclass instances with ``source="curve"``.

Enabled via ``INGEST_CURVE=true`` environment variable.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from web3 import Web3

from config.settings import settings
from ingestion.uniswap_adapter import Trade

logger = logging.getLogger("ledgerlens.curve_adapter")

INGEST_CURVE = os.getenv("INGEST_CURVE", "false").lower() in ("true", "1", "yes")

# Curve StableSwap: TokenExchange(address indexed buyer, int128 sold_id,
#   uint256 tokens_sold, int128 bought_id, uint256 tokens_bought)
CURVE_TOKEN_EXCHANGE_TOPIC = "0x" + Web3.keccak(
    text="TokenExchange(address,int128,uint256,int128,uint256)"
).hex()

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _decode_address_from_topic(topic: str) -> str:
    if topic.startswith("0x"):
        topic = topic[2:]
    return Web3.to_checksum_address("0x" + topic[-40:])


class CurveAdapter:
    """Fetch and filter Curve TokenExchange events for bridge-linked wallets."""

    def __init__(
        self,
        chain: str = "ethereum",
        rpc_url: Optional[str] = None,
        pool_addresses: Optional[list[str]] = None,
    ) -> None:
        self.chain = chain
        self._rpc_url = rpc_url or self._default_rpc(chain)
        self.pool_addresses = [
            Web3.to_checksum_address(a) for a in (pool_addresses or [])
        ]

    @staticmethod
    def _default_rpc(chain: str) -> str:
        return {
            "ethereum": settings.evm_rpc_ethereum,
            "base": settings.evm_rpc_base,
            "polygon": settings.evm_rpc_polygon,
        }.get(chain, settings.evm_rpc_ethereum)

    def _rpc_call(self, method: str, params: list, max_retries: int = 3) -> dict:
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = requests.post(self._rpc_url, json=payload, timeout=30)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < max_retries:
                    time.sleep(2**attempt)
                continue

            if response.status_code in _RETRYABLE_STATUS_CODES:
                time.sleep(2**attempt)
                last_exc = requests.HTTPError(
                    f"HTTP {response.status_code}", response=response
                )
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                last_exc = exc
                if attempt < max_retries:
                    time.sleep(2**attempt)
                continue

            result = response.json()
            if "error" in result:
                raise ValueError(f"JSON-RPC error from {method}: {result['error']}")
            return result

        assert last_exc is not None
        raise last_exc

    def _get_latest_block(self) -> int:
        return int(self._rpc_call("eth_blockNumber", [])["result"], 16)

    def _get_block_timestamp(self, block_number: int) -> datetime:
        result = self._rpc_call("eth_getBlockByNumber", [hex(block_number), False])
        ts = int(result["result"]["timestamp"], 16)
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _get_logs(self, from_block: int, to_block: int, address: str) -> list[dict]:
        params = [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": address,
                "topics": [CURVE_TOKEN_EXCHANGE_TOPIC],
            }
        ]
        return self._rpc_call("eth_getLogs", params)["result"]

    def _parse_token_exchange(
        self, log: dict, pool_address: str, block_timestamp: datetime
    ) -> Trade:
        """Parse a Curve TokenExchange event into a canonical Trade."""
        try:
            topics = log["topics"]
            buyer = _decode_address_from_topic(topics[1])

            data_hex = log["data"]
            if data_hex.startswith("0x"):
                data_hex = data_hex[2:]
            if len(data_hex) < 256:
                raise ValueError(
                    f"TokenExchange data too short ({len(data_hex)} chars)"
                )
            sold_id = int.from_bytes(bytes.fromhex(data_hex[0:64]), "big", signed=True)
            tokens_sold = int.from_bytes(bytes.fromhex(data_hex[64:128]), "big")
            bought_id = int.from_bytes(bytes.fromhex(data_hex[128:192]), "big", signed=True)
            tokens_bought = int.from_bytes(bytes.fromhex(data_hex[192:256]), "big")
        except (KeyError, IndexError) as exc:
            raise ValueError(
                f"Failed to parse TokenExchange event — missing field {exc}"
            ) from exc

        block_num = (
            int(log["blockNumber"], 16)
            if isinstance(log.get("blockNumber"), str)
            else log.get("blockNumber", 0)
        )

        return Trade(
            source="curve",
            chain=self.chain,
            tx_hash=log["transactionHash"],
            block_number=block_num,
            block_timestamp=block_timestamp,
            pool_address=pool_address,
            wallet_address=buyer,
            token_in=f"coin_{sold_id}",
            token_out=f"coin_{bought_id}",
            amount_in=tokens_sold / 1e18,
            amount_out=tokens_bought / 1e18,
        )

    def fetch_swaps(
        self,
        lookback_blocks: Optional[int] = None,
        linked_evm_wallets: Optional[set[str]] = None,
    ) -> list[Trade]:
        """Fetch Curve TokenExchange events, optionally filtering to linked wallets.

        Args:
            lookback_blocks: Number of blocks to scan back (defaults to settings).
            linked_evm_wallets: Set of EIP-55 checksummed EVM addresses linked
                to Stellar wallets.  When provided, only swaps from these
                wallets are returned.

        Returns:
            List of canonical Trade objects with source="curve".
        """
        if not INGEST_CURVE:
            logger.debug("INGEST_CURVE is disabled; skipping Curve ingestion")
            return []

        lookback = lookback_blocks or settings.evm_lookback_blocks
        latest = self._get_latest_block()
        from_block = max(0, latest - lookback)

        all_trades: list[Trade] = []
        for pool_address in self.pool_addresses:
            try:
                logs = self._get_logs(from_block, latest, pool_address)
                block_ts_cache: dict[int, datetime] = {}
                for log in logs:
                    block_num = int(log["blockNumber"], 16)
                    if block_num not in block_ts_cache:
                        block_ts_cache[block_num] = self._get_block_timestamp(block_num)
                    trade = self._parse_token_exchange(
                        log, pool_address, block_ts_cache[block_num]
                    )
                    if linked_evm_wallets and trade.wallet_address not in linked_evm_wallets:
                        continue
                    all_trades.append(trade)
            except Exception as exc:
                logger.warning(
                    "Failed to fetch Curve logs for pool %s on %s: %s",
                    pool_address,
                    self.chain,
                    exc,
                )

        logger.info(
            "Fetched %d Curve swaps on %s",
            len(all_trades),
            self.chain,
        )
        return all_trades
