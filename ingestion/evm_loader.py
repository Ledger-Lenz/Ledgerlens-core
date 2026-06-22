"""EVM chain DEX trade ingestion via public JSON-RPC endpoints.

Fetches Uniswap V2/V3 Swap events from configured pool addresses and
parses them into CrossChainTrade records.  The token-bucket rate limiter
caps outbound RPC calls at 10 req/s per chain to avoid accidental DoS
against public endpoints.
"""

import logging
import time
from datetime import datetime, timezone
from threading import Lock

import requests
from pydantic import BaseModel
from web3 import Web3

from config.settings import settings

logger = logging.getLogger("ledgerlens.evm_loader")

SUPPORTED_CHAINS = ["ethereum", "base", "polygon"]

# Uniswap V3: Swap(address indexed sender, address indexed recipient,
#                  int256 amount0, int256 amount1, uint160 sqrtPriceX96,
#                  uint128 liquidity, int24 tick)
UNISWAP_V3_SWAP_TOPIC = "0x" + Web3.keccak(
    text="Swap(address,address,int256,int256,uint160,uint128,int24)"
).hex()

# Uniswap V2: Swap(address indexed sender, uint256 amount0In, uint256 amount1In,
#                  uint256 amount0Out, uint256 amount1Out, address indexed to)
UNISWAP_V2_SWAP_TOPIC = "0x" + Web3.keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
).hex()


class CrossChainTrade(BaseModel):
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


class _TokenBucket:
    """Thread-safe token bucket for rate limiting (tokens/second)."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            sleep_time = (1.0 - self._tokens) / self._rate
            time.sleep(sleep_time)
            self._tokens = 0.0


def _validate_evm_address(address: str) -> str:
    """Return the EIP-55 checksummed form of address, or raise ValueError."""
    if not isinstance(address, str) or len(address) != 42 or not address.startswith("0x"):
        raise ValueError(f"Malformed EVM address (must be 42-char hex starting with 0x): {address!r}")
    try:
        return Web3.to_checksum_address(address)
    except Exception as exc:
        raise ValueError(f"Invalid EVM address {address!r}: {exc}") from exc


def _decode_address_from_topic(topic: str) -> str:
    """Extract and checksum an EVM address from a 32-byte topic field."""
    if topic.startswith("0x"):
        topic = topic[2:]
    raw = "0x" + topic[-40:]
    return Web3.to_checksum_address(raw)


class EVMTradeLoader:
    """Fetch Uniswap V2/V3 Swap events from EVM chains via JSON-RPC."""

    def __init__(
        self,
        chain: str,
        rpc_url: str,
        pool_addresses: list[str] | None = None,
        _rate_limiter: _TokenBucket | None = None,
    ) -> None:
        if chain not in SUPPORTED_CHAINS:
            raise ValueError(
                f"Unsupported chain: {chain!r}. Supported chains: {SUPPORTED_CHAINS}"
            )
        self.chain = chain
        self._rpc_url = rpc_url
        self.pool_addresses = [_validate_evm_address(a) for a in (pool_addresses or [])]
        self._rate_limiter = _rate_limiter or _TokenBucket(rate=10.0)

    def _rpc_call(self, method: str, params: list, max_retries: int = 3) -> dict:
        """Issue a JSON-RPC call with exponential backoff on 429 / transport errors."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            self._rate_limiter.acquire()
            logger.debug("RPC %s attempt %d/%d", method, attempt + 1, max_retries + 1)
            try:
                response = requests.post(self._rpc_url, json=payload, timeout=30)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < max_retries:
                    time.sleep(2**attempt)
                continue

            if response.status_code == 429:
                wait = 2**attempt
                logger.debug("Rate-limited by RPC endpoint; retrying in %ss", wait)
                time.sleep(wait)
                last_exc = requests.HTTPError(
                    f"429 Too Many Requests from RPC endpoint", response=response
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

    def _get_logs(
        self, from_block: int, to_block: int, address: str, topics: list[str]
    ) -> list[dict]:
        params = [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": address,
                "topics": [topics[0]] if topics else [],
            }
        ]
        return self._rpc_call("eth_getLogs", params)["result"]

    def _parse_v3_swap(
        self, log: dict, pool_address: str, block_timestamp: datetime
    ) -> CrossChainTrade:
        """Parse a Uniswap V3 Swap event log into a CrossChainTrade.

        Raises ValueError (not KeyError) when required fields are missing or malformed.
        """
        try:
            topics = log["topics"]
            sender = _decode_address_from_topic(topics[1])
            data_hex = log["data"]
            if data_hex.startswith("0x"):
                data_hex = data_hex[2:]
            if len(data_hex) < 128:
                raise ValueError(
                    f"Swap event data too short ({len(data_hex)} hex chars, expected ≥128): {data_hex!r}"
                )
            # amount0 and amount1 are signed int256 (first two 32-byte words)
            amount0 = int.from_bytes(bytes.fromhex(data_hex[0:64]), "big", signed=True)
            amount1 = int.from_bytes(bytes.fromhex(data_hex[64:128]), "big", signed=True)
        except (KeyError, IndexError) as exc:
            raise ValueError(
                f"Failed to parse V3 Swap event — missing field {exc}: {log!r}"
            ) from exc

        # In V3, amounts are from the pool's perspective:
        # amount0 < 0 means pool sends token0 to user (user receives token0, pays token1)
        # amount0 > 0 means pool receives token0 from user (user pays token0, receives token1)
        if amount0 < 0:
            token_in, token_out = "token1", "token0"
            amount_in = abs(amount1) / 1e18
            amount_out = abs(amount0) / 1e18
        else:
            token_in, token_out = "token0", "token1"
            amount_in = abs(amount0) / 1e18
            amount_out = abs(amount1) / 1e18

        return CrossChainTrade(
            chain=self.chain,
            tx_hash=log["transactionHash"],
            block_number=int(log["blockNumber"], 16),
            block_timestamp=block_timestamp,
            pool_address=Web3.to_checksum_address(pool_address),
            wallet_address=sender,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out=amount_out,
        )

    def _parse_v2_swap(
        self, log: dict, pool_address: str, block_timestamp: datetime
    ) -> CrossChainTrade:
        """Parse a Uniswap V2 Swap event log into a CrossChainTrade.

        Raises ValueError (not KeyError) when required fields are missing or malformed.
        """
        try:
            topics = log["topics"]
            sender = _decode_address_from_topic(topics[1])
            data_hex = log["data"]
            if data_hex.startswith("0x"):
                data_hex = data_hex[2:]
            if len(data_hex) < 256:
                raise ValueError(
                    f"V2 Swap event data too short ({len(data_hex)} chars): {data_hex!r}"
                )
            # V2 data: amount0In, amount1In, amount0Out, amount1Out (each uint256 = 32 bytes)
            amount0_in = int.from_bytes(bytes.fromhex(data_hex[0:64]), "big")
            amount1_in = int.from_bytes(bytes.fromhex(data_hex[64:128]), "big")
            amount0_out = int.from_bytes(bytes.fromhex(data_hex[128:192]), "big")
            amount1_out = int.from_bytes(bytes.fromhex(data_hex[192:256]), "big")
        except (KeyError, IndexError) as exc:
            raise ValueError(
                f"Failed to parse V2 Swap event — missing field {exc}: {log!r}"
            ) from exc

        if amount0_in > 0:
            amount_in = amount0_in / 1e18
            amount_out = amount1_out / 1e18
            token_in, token_out = "token0", "token1"
        else:
            amount_in = amount1_in / 1e18
            amount_out = amount0_out / 1e18
            token_in, token_out = "token1", "token0"

        return CrossChainTrade(
            chain=self.chain,
            tx_hash=log["transactionHash"],
            block_number=int(log["blockNumber"], 16),
            block_timestamp=block_timestamp,
            pool_address=Web3.to_checksum_address(pool_address),
            wallet_address=sender,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out=amount_out,
        )

    def load_trades(self, lookback_blocks: int | None = None) -> list[CrossChainTrade]:
        """Fetch Swap events from all configured pool addresses.

        Paginates over the last `lookback_blocks` blocks (default from settings).
        """
        lookback_blocks = lookback_blocks if lookback_blocks is not None else settings.evm_lookback_blocks
        latest = self._get_latest_block()
        from_block = max(0, latest - lookback_blocks)

        trades: list[CrossChainTrade] = []
        for pool_address in self.pool_addresses:
            # Try V3 first, fall back to V2
            for topic, parser in [
                (UNISWAP_V3_SWAP_TOPIC, self._parse_v3_swap),
                (UNISWAP_V2_SWAP_TOPIC, self._parse_v2_swap),
            ]:
                try:
                    logs = self._get_logs(from_block, latest, pool_address, [topic])
                    block_ts_cache: dict[int, datetime] = {}
                    for log in logs:
                        block_num = int(log["blockNumber"], 16)
                        if block_num not in block_ts_cache:
                            block_ts_cache[block_num] = self._get_block_timestamp(block_num)
                        trade = parser(log, pool_address, block_ts_cache[block_num])
                        trades.append(trade)
                    if logs:
                        break
                except ValueError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch %s logs for pool %s on %s: %s",
                        topic[:10],
                        pool_address,
                        self.chain,
                        exc,
                    )

        return trades
