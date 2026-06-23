"""Cross-chain bridge transfer ingestion for Allbridge and similar protocols.

Parses on-chain bridge events that link Stellar wallets to EVM wallets.
Currently supports the Allbridge protocol's TokensSent event, which encodes
the Stellar recipient as a 32-byte ed25519 public key in the event data.
"""

import logging
import time
from datetime import datetime, timezone

import requests
from web3 import Web3

from config.settings import settings
from ingestion.data_models import BridgeTransfer

logger = logging.getLogger("ledgerlens.bridge_loader")

# Allbridge TokensSent(address indexed sender, bytes32 recipient, uint256 amount, ...)
ALLBRIDGE_TOKENS_SENT_TOPIC = "0x" + Web3.keccak(
    text="TokensSent(address,bytes32,uint256)"
).hex()

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def decode_bytes32_to_stellar(recipient_bytes: bytes) -> str:
    """Convert a 32-byte ed25519 public key to a Stellar G-address.

    The Allbridge bridge encodes the Stellar recipient as a raw 32-byte
    ed25519 public key in the `recipient` field of TokensSent events.
    """
    if len(recipient_bytes) != 32:
        raise ValueError(
            f"Expected 32 bytes for Stellar public key, got {len(recipient_bytes)}"
        )
    from stellar_sdk import Keypair
    kp = Keypair.from_raw_ed25519_public_key(recipient_bytes)
    return kp.public_key


def _validate_evm_address(address: str) -> str:
    """Return EIP-55 checksummed address or raise ValueError."""
    if not isinstance(address, str) or len(address) != 42 or not address.startswith("0x"):
        raise ValueError(
            f"Malformed EVM address (must be 42-char hex starting with 0x): {address!r}"
        )
    try:
        return Web3.to_checksum_address(address)
    except Exception as exc:
        raise ValueError(f"Invalid EVM address {address!r}: {exc}") from exc


class BridgeTransferLoader:
    """Fetch and parse Allbridge TokensSent events from EVM chains."""

    def __init__(
        self,
        chain: str,
        rpc_url: str,
        contract_address: str,
    ) -> None:
        self.chain = chain
        self._rpc_url = rpc_url
        self.contract_address = _validate_evm_address(contract_address)

    def _rpc_call(self, method: str, params: list, max_retries: int = 3) -> dict:
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            logger.debug("Bridge RPC %s attempt %d/%d", method, attempt + 1, max_retries + 1)
            try:
                response = requests.post(self._rpc_url, json=payload, timeout=30)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < max_retries:
                    time.sleep(2**attempt)
                continue

            if response.status_code in _RETRYABLE_STATUS_CODES:
                wait = 2**attempt
                logger.debug("Retryable HTTP %d; waiting %ss", response.status_code, wait)
                time.sleep(wait)
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

    def _get_logs(self, from_block: int, to_block: int) -> list[dict]:
        params = [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": self.contract_address,
                "topics": [ALLBRIDGE_TOKENS_SENT_TOPIC],
            }
        ]
        return self._rpc_call("eth_getLogs", params)["result"]

    def _get_latest_block(self) -> int:
        return int(self._rpc_call("eth_blockNumber", [])["result"], 16)

    def _get_block_timestamp(self, block_number: int) -> datetime:
        result = self._rpc_call("eth_getBlockByNumber", [hex(block_number), False])
        ts = int(result["result"]["timestamp"], 16)
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _parse_tokens_sent(self, log: dict) -> BridgeTransfer:
        """Parse an Allbridge TokensSent event log into a BridgeTransfer.

        TokensSent(address indexed sender, bytes32 recipient, uint256 amount, ...)
        - topics[0]: event signature hash
        - topics[1]: sender address (indexed)
        - data: ABI-encoded (recipient_bytes32, amount_uint256, ...)
        """
        try:
            topics = log["topics"]
            # sender is indexed — padded to 32 bytes in topics[1]
            sender_raw = topics[1]
            if sender_raw.startswith("0x"):
                sender_raw = sender_raw[2:]
            sender = Web3.to_checksum_address("0x" + sender_raw[-40:])

            data_hex = log["data"]
            if data_hex.startswith("0x"):
                data_hex = data_hex[2:]
            if len(data_hex) < 128:
                raise ValueError(
                    f"TokensSent data too short ({len(data_hex)} chars): {data_hex!r}"
                )
            recipient_bytes = bytes.fromhex(data_hex[0:64])
            amount_raw = int.from_bytes(bytes.fromhex(data_hex[64:128]), "big")  # noqa: F841
        except (KeyError, IndexError) as exc:
            raise ValueError(
                f"Failed to parse TokensSent event — missing field {exc}: {log!r}"
            ) from exc

        stellar_wallet = decode_bytes32_to_stellar(recipient_bytes)

        block_num = int(log["blockNumber"], 16) if isinstance(log.get("blockNumber"), str) else log.get("blockNumber", 0)
        block_ts = self._get_block_timestamp(block_num) if block_num else datetime.now(timezone.utc)

        return BridgeTransfer(
            chain=self.chain,
            direction="evm_to_stellar",
            evm_wallet=sender,
            stellar_wallet=stellar_wallet,
            amount_usd=None,
            token="USDC",
            tx_hash_evm=log["transactionHash"],
            tx_hash_stellar=None,
            timestamp=block_ts,
        )

    def load_transfers(
        self,
        lookback_blocks: int | None = None,
        db_path: str | None = None,
    ) -> list[BridgeTransfer]:
        """Fetch TokensSent events and optionally persist them to storage.

        Returns the parsed BridgeTransfer list.
        """
        lookback_blocks = lookback_blocks if lookback_blocks is not None else settings.evm_lookback_blocks
        latest = self._get_latest_block()
        from_block = max(0, latest - lookback_blocks)

        logs = self._get_logs(from_block, latest)
        transfers: list[BridgeTransfer] = []
        for log in logs:
            try:
                transfer = self._parse_tokens_sent(log)
                transfers.append(transfer)
            except ValueError as exc:
                logger.warning("Skipping malformed bridge event: %s", exc)

        if db_path is not None or settings.db_path:
            from detection.storage import save_bridge_transfers
            save_bridge_transfers(transfers, db_path=db_path)

        return transfers
