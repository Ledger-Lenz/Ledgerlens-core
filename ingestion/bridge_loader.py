"""Cross-chain bridge transfer ingestion for Allbridge and similar protocols.

Parses on-chain bridge events that link Stellar wallets to EVM wallets.
Currently supports the Allbridge protocol's TokensSent event, which encodes
the Stellar recipient as a 32-byte ed25519 public key in the event data.

Bridge event integrity verification
------------------------------------
Each ingested event receives a deterministic canonical hash (SHA-256 over the
event's immutable fields). Optionally, the event is verified against its on-chain
transaction receipt via ``eth_getTransactionReceipt``. The verification rate is
controlled by ``BRIDGE_VERIFY_SAMPLE_RATE`` (0.0 = disabled, 1.0 = all events).
Tampered events are routed to the dead-letter queue and never written to storage.
"""

import hashlib
import json
import logging
import random
import time
from datetime import datetime, timezone
from enum import Enum

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


# ---------------------------------------------------------------------------
# Integrity verification
# ---------------------------------------------------------------------------


class VerificationResult(str, Enum):
    VERIFIED = "verified"
    TAMPERED = "tampered"
    RECEIPT_NOT_FOUND = "receipt_not_found"
    LOG_INDEX_OUT_OF_RANGE = "log_index_out_of_range"
    SKIPPED = "skipped"    # not selected for sampling
    DISABLED = "disabled"  # verification turned off (sample_rate == 0)


def compute_canonical_event_hash(
    chain_id: int,
    contract_address: str,
    topics: list[str],
    data: str,
    block_number: int,
    tx_hash: str,
    log_index: int,
) -> str:
    """Return a deterministic SHA-256 fingerprint of the event's immutable fields.

    All hex strings are normalised to lowercase before hashing so that
    uppercase and lowercase representations produce the same digest.

    Args:
        chain_id: EVM chain ID (e.g. 1 for Ethereum mainnet).
        contract_address: Contract that emitted the event (hex string).
        topics: List of 32-byte topic hex strings.
        data: ABI-encoded event data (hex string).
        block_number: Block height at which the event was mined.
        tx_hash: Transaction hash (hex string).
        log_index: Index of this log within its transaction receipt.

    Returns:
        Lowercase hex SHA-256 digest (64 characters).
    """
    canonical = {
        "chain_id": chain_id,
        "address": contract_address.lower(),
        "topics": [t.lower() for t in topics],
        "data": data.lower(),
        "block_number": block_number,
        "tx_hash": tx_hash.lower(),
        "log_index": log_index,
    }
    return hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


class BridgeEventVerifier:
    """Receipt-based integrity verifier for bridge events.

    For each event, calls ``eth_getTransactionReceipt`` and compares the log
    at ``event.log_index`` against the fields returned by ``eth_getLogs``.
    A mismatch signals that the RPC provider returned fraudulent log data.

    Note: this is not a full Merkle proof — it relies on the receipt call
    reaching a trustworthy RPC node. For maximum security, configure multiple
    independent providers (see EVMProviderPool in ISSUE-013).
    """

    def __init__(self, rpc_call_fn, timeout: float | None = None) -> None:
        """
        Args:
            rpc_call_fn: Synchronous callable ``(method, params) -> dict`` that
                issues a JSON-RPC call and returns the parsed response dict.
            timeout: Maximum seconds to wait for the receipt call (default from
                ``BRIDGE_VERIFY_RECEIPT_TIMEOUT_SECONDS`` setting).
        """
        self._rpc_call = rpc_call_fn
        self._timeout = timeout if timeout is not None else settings.bridge_verify_receipt_timeout_seconds

    def verify_event_via_receipt(
        self,
        tx_hash: str,
        log_index: int,
        contract_address: str,
        topics: list[str],
        data: str,
        block_hash: str,
    ) -> VerificationResult:
        """Verify a single bridge event against its on-chain receipt.

        Algorithm:
        1. Call ``eth_getTransactionReceipt`` for *tx_hash*.
        2. Locate the log at *log_index* in ``receipt["logs"]``.
        3. Compare address, topics, data, and blockHash.
        4. Return ``VERIFIED`` on full match, ``TAMPERED`` on mismatch.

        Args:
            tx_hash: Transaction hash of the event to verify.
            log_index: Position of this log in the receipt's log list.
            contract_address: Expected emitting contract address.
            topics: Expected list of topic hex strings.
            data: Expected ABI-encoded data hex string.
            block_hash: Expected block hash the event was included in.

        Returns:
            ``VerificationResult`` enum value.
        """
        try:
            result = self._rpc_call("eth_getTransactionReceipt", [tx_hash])
        except Exception as exc:
            logger.warning("Receipt fetch failed for tx %s: %s", tx_hash, exc)
            return VerificationResult.RECEIPT_NOT_FOUND

        receipt = result.get("result") if isinstance(result, dict) else result
        if receipt is None:
            return VerificationResult.RECEIPT_NOT_FOUND

        logs = receipt.get("logs", [])
        if log_index >= len(logs):
            return VerificationResult.LOG_INDEX_OUT_OF_RANGE

        receipt_log = logs[log_index]
        if (
            receipt_log.get("address", "").lower() == contract_address.lower()
            and receipt_log.get("topics") == topics
            and receipt_log.get("data") == data
            and receipt_log.get("blockHash") == block_hash
        ):
            return VerificationResult.VERIFIED
        return VerificationResult.TAMPERED


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class BridgeTransferLoader:
    """Fetch and parse Allbridge TokensSent events from EVM chains.

    When ``BRIDGE_VERIFY_SAMPLE_RATE`` is greater than zero, a fraction of
    ingested events are verified against their on-chain receipts.  Tampered
    events are logged at ERROR level, sent to the dead-letter queue, and NOT
    written to the ``bridge_transfers`` table.
    """

    def __init__(
        self,
        chain: str,
        rpc_url: str,
        contract_address: str,
        chain_id: int = 1,
    ) -> None:
        self.chain = chain
        self._rpc_url = rpc_url
        self.contract_address = _validate_evm_address(contract_address)
        self.chain_id = chain_id
        self._verifier = BridgeEventVerifier(rpc_call_fn=self._rpc_call)

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

        # Compute canonical hash over immutable event fields
        log_index = int(log.get("logIndex", "0x0"), 16) if isinstance(log.get("logIndex"), str) else log.get("logIndex", 0)
        canonical_hash = compute_canonical_event_hash(
            chain_id=self.chain_id,
            contract_address=log.get("address", self.contract_address),
            topics=log["topics"],
            data=log["data"],
            block_number=block_num,
            tx_hash=log["transactionHash"],
            log_index=log_index,
        )

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
            canonical_hash=canonical_hash,
            verification_status=VerificationResult.DISABLED,
            verified_at=None,
            # Store raw log fields needed for receipt verification
            _log_index=log_index,
            _topics=log["topics"],
            _data=log["data"],
            _block_hash=log.get("blockHash", ""),
        )

    def load_transfers(
        self,
        lookback_blocks: int | None = None,
        db_path: str | None = None,
    ) -> list[BridgeTransfer]:
        """Fetch TokensSent events and optionally persist them to storage.

        Events are verified against on-chain receipts according to
        ``BRIDGE_VERIFY_SAMPLE_RATE``.  Tampered events are routed to the
        dead-letter queue and excluded from the returned list.

        Returns:
            List of ``BridgeTransfer`` records that passed verification
            (or were not selected for verification).
        """
        sample_rate = settings.bridge_verify_sample_rate

        lookback_blocks = lookback_blocks if lookback_blocks is not None else settings.evm_lookback_blocks
        latest = self._get_latest_block()
        from_block = max(0, latest - lookback_blocks)

        logs = self._get_logs(from_block, latest)
        accepted: list[BridgeTransfer] = []
        tampered_count = 0

        for log in logs:
            try:
                transfer = self._parse_tokens_sent(log)
            except ValueError as exc:
                logger.warning("Skipping malformed bridge event: %s", exc)
                continue

            if sample_rate > 0.0 and random.random() < sample_rate:
                result = self._verifier.verify_event_via_receipt(
                    tx_hash=transfer.tx_hash_evm,
                    log_index=transfer._log_index,  # type: ignore[attr-defined]
                    contract_address=self.contract_address,
                    topics=transfer._topics,  # type: ignore[attr-defined]
                    data=transfer._data,  # type: ignore[attr-defined]
                    block_hash=transfer._block_hash,  # type: ignore[attr-defined]
                )
                transfer.verification_status = result
                transfer.verified_at = datetime.now(timezone.utc)

                if result == VerificationResult.TAMPERED:
                    logger.error(
                        "TAMPERED bridge event detected: tx=%s log_index=%d chain=%d",
                        transfer.tx_hash_evm,
                        transfer._log_index,  # type: ignore[attr-defined]
                        self.chain_id,
                    )
                    self._send_to_dlq(transfer)
                    tampered_count += 1
                    continue
            else:
                transfer.verification_status = (
                    VerificationResult.DISABLED if sample_rate == 0.0
                    else VerificationResult.SKIPPED
                )

            accepted.append(transfer)

        if tampered_count:
            logger.warning(
                "bridge_loader: %d tampered event(s) rejected for chain=%d",
                tampered_count, self.chain_id,
            )

        if db_path is not None or settings.db_path:
            from detection.storage import save_bridge_transfers
            save_bridge_transfers(accepted, db_path=db_path)

        return accepted

    @staticmethod
    def _send_to_dlq(transfer: BridgeTransfer) -> None:
        """Route a tampered event to the dead-letter queue.

        Only the transaction hash, log index, and chain are included — the
        full data field is deliberately excluded to avoid leaking large or
        sensitive encoded payloads into the DLQ.
        """
        try:
            from detection.webhook_queue import enqueue

            enqueue(
                subscriber_id="bridge_loader",
                payload={
                    "error_class": "SCHEMA_ERROR",
                    "reason": "TAMPERED",
                    "tx_hash": transfer.tx_hash_evm,
                    "log_index": getattr(transfer, "_log_index", None),
                    "chain": transfer.chain,
                },
            )
        except Exception as exc:
            logger.warning("Failed to enqueue tampered event to DLQ: %s", exc)
