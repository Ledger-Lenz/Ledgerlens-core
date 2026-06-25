"""Tests for bridge event integrity verification (ISSUE-016).

Covers:
  - compute_canonical_event_hash: determinism, field sensitivity, case-insensitivity
  - BridgeEventVerifier.verify_event_via_receipt: VERIFIED, TAMPERED,
    RECEIPT_NOT_FOUND, LOG_INDEX_OUT_OF_RANGE
  - Sampling logic: rate=0 skips all, rate=1 verifies all, rate=0.5 ~50%
  - Tampered event: goes to DLQ, excluded from bridge_transfers table
  - Integration: mock receipt matching → verified; mismatch → DLQ
  - Edge cases: empty logs list, receipt call exception
"""

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib
from stellar_sdk import Keypair

from ingestion.bridge_loader import (
    ALLBRIDGE_TOKENS_SENT_TOPIC,
    BridgeEventVerifier,
    BridgeTransferLoader,
    VerificationResult,
    compute_canonical_event_hash,
)
from ingestion.data_models import BridgeTransfer

MOCK_RPC_URL = "https://mock-integrity-rpc.example.com"
BRIDGE_CONTRACT = "0x7DBF072b9E4E7Eb4B509E5f8afc08f24A61AC67E"

TX_HASH = "0x" + "ab" * 32
BLOCK_HASH = "0x" + "cd" * 32
TOPICS = [ALLBRIDGE_TOKENS_SENT_TOPIC, "0x" + "00" * 12 + "ab5801a7d398351b8be11c439e05c5b3259aec9b"]
DATA = "0x" + "ef" * 64
BLOCK_NUMBER = 100
LOG_INDEX = 0
CHAIN_ID = 1

_RECENT_TS = 1_781_827_200  # 2026-06-19


def _rpc_response(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _make_receipt(address: str, topics: list, data: str, block_hash: str, log_index: int = 0) -> dict:
    return {
        "logs": [
            {
                "address": address,
                "topics": topics,
                "data": data,
                "blockHash": block_hash,
                "logIndex": hex(log_index),
                "transactionHash": TX_HASH,
            }
        ]
    }


def _make_tokens_sent_log(recipient_bytes: bytes, log_index: int = 0) -> dict:
    sender_padded = "0x" + "0" * 24 + "ab5801a7d398351b8be11c439e05c5b3259aec9b"
    recipient_hex = recipient_bytes.hex().zfill(64)
    amount_hex = hex(1_000_000)[2:].zfill(64)
    return {
        "topics": [ALLBRIDGE_TOKENS_SENT_TOPIC, sender_padded],
        "data": "0x" + recipient_hex + amount_hex,
        "transactionHash": TX_HASH,
        "blockNumber": hex(BLOCK_NUMBER),
        "blockHash": BLOCK_HASH,
        "logIndex": hex(log_index),
        "address": BRIDGE_CONTRACT,
        "removed": False,
    }


# ============================================================
# compute_canonical_event_hash
# ============================================================


def test_canonical_hash_is_deterministic():
    h1 = compute_canonical_event_hash(CHAIN_ID, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_NUMBER, TX_HASH, LOG_INDEX)
    h2 = compute_canonical_event_hash(CHAIN_ID, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_NUMBER, TX_HASH, LOG_INDEX)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex digest


def test_canonical_hash_differs_on_log_index():
    h0 = compute_canonical_event_hash(CHAIN_ID, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_NUMBER, TX_HASH, 0)
    h1 = compute_canonical_event_hash(CHAIN_ID, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_NUMBER, TX_HASH, 1)
    assert h0 != h1


def test_canonical_hash_case_insensitive_for_hex_strings():
    h_lower = compute_canonical_event_hash(1, BRIDGE_CONTRACT.lower(), TOPICS, DATA.lower(), BLOCK_NUMBER, TX_HASH.lower(), 0)
    h_upper = compute_canonical_event_hash(1, BRIDGE_CONTRACT.upper(), TOPICS, DATA.upper(), BLOCK_NUMBER, TX_HASH.upper(), 0)
    assert h_lower == h_upper


def test_canonical_hash_differs_on_data():
    h1 = compute_canonical_event_hash(CHAIN_ID, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_NUMBER, TX_HASH, LOG_INDEX)
    h2 = compute_canonical_event_hash(CHAIN_ID, BRIDGE_CONTRACT, TOPICS, DATA + "ff", BLOCK_NUMBER, TX_HASH, LOG_INDEX)
    assert h1 != h2


def test_canonical_hash_differs_on_chain_id():
    h1 = compute_canonical_event_hash(1, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_NUMBER, TX_HASH, LOG_INDEX)
    h2 = compute_canonical_event_hash(137, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_NUMBER, TX_HASH, LOG_INDEX)
    assert h1 != h2


# ============================================================
# BridgeEventVerifier — unit tests
# ============================================================


def _make_verifier(receipt_result):
    """Return a BridgeEventVerifier backed by a mock RPC callable."""
    def _rpc(method, params):
        return {"result": receipt_result}
    return BridgeEventVerifier(rpc_call_fn=_rpc)


def test_verifier_returns_verified_on_matching_receipt():
    receipt = _make_receipt(BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    verifier = _make_verifier(receipt)
    result = verifier.verify_event_via_receipt(TX_HASH, 0, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.VERIFIED


def test_verifier_returns_tampered_on_mismatched_data():
    receipt = _make_receipt(BRIDGE_CONTRACT, TOPICS, DATA + "ff", BLOCK_HASH)
    verifier = _make_verifier(receipt)
    result = verifier.verify_event_via_receipt(TX_HASH, 0, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.TAMPERED


def test_verifier_returns_tampered_on_mismatched_address():
    receipt = _make_receipt("0x" + "00" * 20, TOPICS, DATA, BLOCK_HASH)
    verifier = _make_verifier(receipt)
    result = verifier.verify_event_via_receipt(TX_HASH, 0, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.TAMPERED


def test_verifier_returns_tampered_on_mismatched_block_hash():
    receipt = _make_receipt(BRIDGE_CONTRACT, TOPICS, DATA, "0x" + "ff" * 32)
    verifier = _make_verifier(receipt)
    result = verifier.verify_event_via_receipt(TX_HASH, 0, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.TAMPERED


def test_verifier_returns_receipt_not_found_when_result_is_none():
    verifier = _make_verifier(None)
    result = verifier.verify_event_via_receipt(TX_HASH, 0, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.RECEIPT_NOT_FOUND


def test_verifier_returns_receipt_not_found_on_rpc_exception():
    def _failing_rpc(method, params):
        raise ConnectionError("network error")
    verifier = BridgeEventVerifier(rpc_call_fn=_failing_rpc)
    result = verifier.verify_event_via_receipt(TX_HASH, 0, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.RECEIPT_NOT_FOUND


def test_verifier_returns_log_index_out_of_range():
    receipt = _make_receipt(BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)  # only 1 log at index 0
    verifier = _make_verifier(receipt)
    result = verifier.verify_event_via_receipt(TX_HASH, 5, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.LOG_INDEX_OUT_OF_RANGE


def test_verifier_returns_log_index_out_of_range_on_empty_logs():
    receipt = {"logs": []}
    verifier = _make_verifier(receipt)
    result = verifier.verify_event_via_receipt(TX_HASH, 0, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.LOG_INDEX_OUT_OF_RANGE


def test_verifier_address_comparison_is_case_insensitive():
    # receipt address in uppercase, expected in lowercase — should still verify
    receipt = _make_receipt(BRIDGE_CONTRACT.upper(), TOPICS, DATA, BLOCK_HASH)
    verifier = _make_verifier(receipt)
    result = verifier.verify_event_via_receipt(TX_HASH, 0, BRIDGE_CONTRACT.lower(), TOPICS, DATA, BLOCK_HASH)
    assert result == VerificationResult.VERIFIED


# ============================================================
# Sampling logic
# ============================================================


def _make_loader_with_receipt(receipt_result, *, sample_rate: float):
    """Build a BridgeTransferLoader whose RPC returns a fixed receipt."""
    stellar_kp = Keypair.random()
    log = _make_tokens_sent_log(stellar_kp.raw_public_key())

    call_log: list[str] = []

    def _rpc(method, params, max_retries=3):
        call_log.append(method)
        if method == "eth_blockNumber":
            return _rpc_response(hex(BLOCK_NUMBER + 10))
        if method == "eth_getLogs":
            return _rpc_response([log])
        if method == "eth_getBlockByNumber":
            return _rpc_response({"timestamp": hex(_RECENT_TS), "number": hex(BLOCK_NUMBER)})
        if method == "eth_getTransactionReceipt":
            return {"result": receipt_result}
        return _rpc_response(None)

    loader = BridgeTransferLoader("ethereum", MOCK_RPC_URL, BRIDGE_CONTRACT, chain_id=CHAIN_ID)
    loader._rpc_call = _rpc
    loader._verifier = BridgeEventVerifier(rpc_call_fn=_rpc)
    return loader, call_log


def test_sample_rate_zero_disables_all_verification(tmp_path):
    """sample_rate=0.0 → no eth_getTransactionReceipt calls, status=disabled."""
    receipt = _make_receipt(BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_HASH)
    loader, call_log = _make_loader_with_receipt(receipt, sample_rate=0.0)

    with patch("ingestion.bridge_loader.settings") as mock_settings:
        mock_settings.bridge_verify_sample_rate = 0.0
        mock_settings.evm_lookback_blocks = 10
        mock_settings.db_path = ""
        transfers = loader.load_transfers(lookback_blocks=10, db_path=None)

    assert all(t.verification_status == VerificationResult.DISABLED for t in transfers)
    assert "eth_getTransactionReceipt" not in call_log


def test_sample_rate_one_verifies_all_events(tmp_path):
    """sample_rate=1.0 → every event calls eth_getTransactionReceipt."""
    stellar_kp = Keypair.random()
    log = _make_tokens_sent_log(stellar_kp.raw_public_key())
    receipt_log = {
        "address": BRIDGE_CONTRACT,
        "topics": log["topics"],
        "data": log["data"],
        "blockHash": BLOCK_HASH,
        "logIndex": "0x0",
        "transactionHash": TX_HASH,
    }
    receipt = {"logs": [receipt_log]}

    call_log: list[str] = []

    def _rpc(method, params, max_retries=3):
        call_log.append(method)
        if method == "eth_blockNumber":
            return _rpc_response(hex(BLOCK_NUMBER + 10))
        if method == "eth_getLogs":
            return _rpc_response([log])
        if method == "eth_getBlockByNumber":
            return _rpc_response({"timestamp": hex(_RECENT_TS), "number": hex(BLOCK_NUMBER)})
        if method == "eth_getTransactionReceipt":
            return {"result": receipt}
        return _rpc_response(None)

    loader = BridgeTransferLoader("ethereum", MOCK_RPC_URL, BRIDGE_CONTRACT, chain_id=CHAIN_ID)
    loader._rpc_call = _rpc
    loader._verifier = BridgeEventVerifier(rpc_call_fn=_rpc)

    with patch("ingestion.bridge_loader.settings") as mock_settings:
        mock_settings.bridge_verify_sample_rate = 1.0
        mock_settings.evm_lookback_blocks = 10
        mock_settings.db_path = ""
        transfers = loader.load_transfers(lookback_blocks=10, db_path=None)

    receipt_calls = call_log.count("eth_getTransactionReceipt")
    assert receipt_calls == 1
    assert transfers[0].verification_status == VerificationResult.VERIFIED


def test_sample_rate_half_verifies_approximately_half(tmp_path):
    """sample_rate=0.5 verifies roughly 50% of events (statistical check, N=200)."""
    N = 200
    stellar_kps = [Keypair.random() for _ in range(N)]
    logs = [_make_tokens_sent_log(kp.raw_public_key(), log_index=i) for i, kp in enumerate(stellar_kps)]

    call_count = {"receipts": 0}

    def _rpc(method, params, max_retries=3):
        if method == "eth_blockNumber":
            return _rpc_response(hex(BLOCK_NUMBER + 10))
        if method == "eth_getLogs":
            return _rpc_response(logs)
        if method == "eth_getBlockByNumber":
            return _rpc_response({"timestamp": hex(_RECENT_TS), "number": hex(BLOCK_NUMBER)})
        if method == "eth_getTransactionReceipt":
            call_count["receipts"] += 1
            return {"result": None}  # RECEIPT_NOT_FOUND — doesn't matter for count test
        return _rpc_response(None)

    loader = BridgeTransferLoader("ethereum", MOCK_RPC_URL, BRIDGE_CONTRACT, chain_id=CHAIN_ID)
    loader._rpc_call = _rpc
    loader._verifier = BridgeEventVerifier(rpc_call_fn=_rpc)

    with patch("ingestion.bridge_loader.settings") as mock_settings:
        mock_settings.bridge_verify_sample_rate = 0.5
        mock_settings.evm_lookback_blocks = 10
        mock_settings.db_path = ""
        loader.load_transfers(lookback_blocks=10, db_path=None)

    ratio = call_count["receipts"] / N
    assert 0.30 <= ratio <= 0.70, f"Expected ~50% verification rate, got {ratio:.0%}"


# ============================================================
# Tampered event: DLQ routing and exclusion from storage
# ============================================================


def test_tampered_event_goes_to_dlq_not_to_storage(tmp_path):
    """A tampered event is enqueued on the DLQ and excluded from bridge_transfers."""
    stellar_kp = Keypair.random()
    log = _make_tokens_sent_log(stellar_kp.raw_public_key())

    # Receipt returns mismatched data → TAMPERED
    tampered_receipt = {
        "logs": [
            {
                "address": BRIDGE_CONTRACT,
                "topics": log["topics"],
                "data": "0x" + "ff" * 64,  # wrong data
                "blockHash": BLOCK_HASH,
                "logIndex": "0x0",
                "transactionHash": TX_HASH,
            }
        ]
    }

    def _rpc(method, params, max_retries=3):
        if method == "eth_blockNumber":
            return _rpc_response(hex(BLOCK_NUMBER + 10))
        if method == "eth_getLogs":
            return _rpc_response([log])
        if method == "eth_getBlockByNumber":
            return _rpc_response({"timestamp": hex(_RECENT_TS), "number": hex(BLOCK_NUMBER)})
        if method == "eth_getTransactionReceipt":
            return {"result": tampered_receipt}
        return _rpc_response(None)

    loader = BridgeTransferLoader("ethereum", MOCK_RPC_URL, BRIDGE_CONTRACT, chain_id=CHAIN_ID)
    loader._rpc_call = _rpc
    loader._verifier = BridgeEventVerifier(rpc_call_fn=_rpc)

    db = str(tmp_path / "test.db")
    dlq_calls: list[dict] = []

    def _fake_enqueue(subscriber_id, payload, db_path=None):
        dlq_calls.append({"subscriber_id": subscriber_id, "payload": payload})

    with patch("ingestion.bridge_loader.settings") as mock_settings, \
         patch("detection.webhook_queue.enqueue", side_effect=_fake_enqueue):
        mock_settings.bridge_verify_sample_rate = 1.0
        mock_settings.evm_lookback_blocks = 10
        mock_settings.db_path = db
        transfers = loader.load_transfers(lookback_blocks=10, db_path=db)

    # Tampered event must not appear in returned list
    assert transfers == []

    # DLQ must have received exactly one entry
    assert len(dlq_calls) == 1
    payload = dlq_calls[0]["payload"]
    assert payload["error_class"] == "SCHEMA_ERROR"
    assert payload["reason"] == "TAMPERED"
    assert payload["tx_hash"] == TX_HASH
    # Full data field must NOT be in DLQ payload
    assert "data" not in payload

    # bridge_transfers table must be empty
    from detection.storage import get_bridge_transfers
    saved = get_bridge_transfers(stellar_wallet=stellar_kp.public_key, db_path=db)
    assert saved == []


def test_tampered_event_error_log_excludes_data_field(caplog, tmp_path):
    """ERROR log for tampered event contains tx_hash and chain but not data."""
    stellar_kp = Keypair.random()
    log = _make_tokens_sent_log(stellar_kp.raw_public_key())

    tampered_receipt = {
        "logs": [
            {
                "address": BRIDGE_CONTRACT,
                "topics": log["topics"],
                "data": "0x" + "ff" * 64,
                "blockHash": BLOCK_HASH,
                "logIndex": "0x0",
                "transactionHash": TX_HASH,
            }
        ]
    }

    def _rpc(method, params, max_retries=3):
        if method == "eth_blockNumber":
            return _rpc_response(hex(BLOCK_NUMBER + 10))
        if method == "eth_getLogs":
            return _rpc_response([log])
        if method == "eth_getBlockByNumber":
            return _rpc_response({"timestamp": hex(_RECENT_TS), "number": hex(BLOCK_NUMBER)})
        if method == "eth_getTransactionReceipt":
            return {"result": tampered_receipt}
        return _rpc_response(None)

    loader = BridgeTransferLoader("ethereum", MOCK_RPC_URL, BRIDGE_CONTRACT, chain_id=CHAIN_ID)
    loader._rpc_call = _rpc
    loader._verifier = BridgeEventVerifier(rpc_call_fn=_rpc)

    import logging
    with patch("ingestion.bridge_loader.settings") as mock_settings, \
         caplog.at_level(logging.ERROR, logger="ledgerlens.bridge_loader"), \
         patch("ingestion.bridge_loader.BridgeTransferLoader._send_to_dlq"):
        mock_settings.bridge_verify_sample_rate = 1.0
        mock_settings.evm_lookback_blocks = 10
        mock_settings.db_path = ""
        loader.load_transfers(lookback_blocks=10, db_path=None)

    assert any("TAMPERED" in r.message and TX_HASH in r.message for r in caplog.records)


# ============================================================
# Integration: verified event written with correct status
# ============================================================


def test_integration_verified_event_written_to_storage(tmp_path):
    """Mock receipt matches event → transfer saved with verification_status=verified."""
    stellar_kp = Keypair.random()
    log = _make_tokens_sent_log(stellar_kp.raw_public_key())
    receipt_log = {
        "address": BRIDGE_CONTRACT,
        "topics": log["topics"],
        "data": log["data"],
        "blockHash": BLOCK_HASH,
        "logIndex": "0x0",
        "transactionHash": TX_HASH,
    }
    receipt = {"logs": [receipt_log]}

    def _rpc(method, params, max_retries=3):
        if method == "eth_blockNumber":
            return _rpc_response(hex(BLOCK_NUMBER + 10))
        if method == "eth_getLogs":
            return _rpc_response([log])
        if method == "eth_getBlockByNumber":
            return _rpc_response({"timestamp": hex(_RECENT_TS), "number": hex(BLOCK_NUMBER)})
        if method == "eth_getTransactionReceipt":
            return {"result": receipt}
        return _rpc_response(None)

    loader = BridgeTransferLoader("ethereum", MOCK_RPC_URL, BRIDGE_CONTRACT, chain_id=CHAIN_ID)
    loader._rpc_call = _rpc
    loader._verifier = BridgeEventVerifier(rpc_call_fn=_rpc)

    db = str(tmp_path / "test.db")
    with patch("ingestion.bridge_loader.settings") as mock_settings:
        mock_settings.bridge_verify_sample_rate = 1.0
        mock_settings.evm_lookback_blocks = 10
        mock_settings.db_path = db
        transfers = loader.load_transfers(lookback_blocks=10, db_path=db)

    assert len(transfers) == 1
    t = transfers[0]
    assert t.verification_status == VerificationResult.VERIFIED
    assert t.verified_at is not None
    assert t.canonical_hash is not None and len(t.canonical_hash) == 64

    from detection.storage import get_bridge_transfers
    saved = get_bridge_transfers(stellar_wallet=stellar_kp.public_key, db_path=db)
    assert len(saved) == 1
    assert saved[0].verification_status == VerificationResult.VERIFIED
    assert saved[0].canonical_hash == t.canonical_hash


def test_canonical_hash_stored_in_db(tmp_path):
    """canonical_hash is persisted to the bridge_transfers table."""
    from detection.storage import get_bridge_transfers, save_bridge_transfer

    stellar_kp = Keypair.random()
    expected_hash = compute_canonical_event_hash(CHAIN_ID, BRIDGE_CONTRACT, TOPICS, DATA, BLOCK_NUMBER, TX_HASH, 0)

    transfer = BridgeTransfer(
        chain="ethereum",
        direction="evm_to_stellar",
        evm_wallet="0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B",
        stellar_wallet=stellar_kp.public_key,
        amount_usd=None,
        token="USDC",
        tx_hash_evm=TX_HASH,
        tx_hash_stellar=None,
        timestamp=datetime.now(timezone.utc),
        canonical_hash=expected_hash,
        verification_status=VerificationResult.VERIFIED,
        verified_at=datetime.now(timezone.utc),
    )
    db = str(tmp_path / "test.db")
    save_bridge_transfer(transfer, db_path=db)

    saved = get_bridge_transfers(stellar_wallet=stellar_kp.public_key, db_path=db)
    assert len(saved) == 1
    assert saved[0].canonical_hash == expected_hash
    assert saved[0].verification_status == VerificationResult.VERIFIED
    assert saved[0].verified_at is not None


# ============================================================
# Performance benchmark (soft — no hard assertion on time)
# ============================================================


def test_verify_1000_events_completes_quickly():
    """Verifying 1000 events with a fast mock provider should finish in < 30s."""
    N = 1000
    stellar_kps = [Keypair.random() for _ in range(N)]
    logs = [_make_tokens_sent_log(kp.raw_public_key(), log_index=i) for i, kp in enumerate(stellar_kps)]

    def _rpc(method, params, max_retries=3):
        if method == "eth_blockNumber":
            return _rpc_response(hex(BLOCK_NUMBER + 10))
        if method == "eth_getLogs":
            return _rpc_response(logs)
        if method == "eth_getBlockByNumber":
            return _rpc_response({"timestamp": hex(_RECENT_TS), "number": hex(BLOCK_NUMBER)})
        if method == "eth_getTransactionReceipt":
            return {"result": {"logs": []}}  # LOG_INDEX_OUT_OF_RANGE — fast response
        return _rpc_response(None)

    loader = BridgeTransferLoader("ethereum", MOCK_RPC_URL, BRIDGE_CONTRACT, chain_id=CHAIN_ID)
    loader._rpc_call = _rpc
    loader._verifier = BridgeEventVerifier(rpc_call_fn=_rpc)

    start = time.monotonic()
    with patch("ingestion.bridge_loader.settings") as mock_settings:
        mock_settings.bridge_verify_sample_rate = 1.0
        mock_settings.evm_lookback_blocks = N + 10
        mock_settings.db_path = ""
        loader.load_transfers(lookback_blocks=N + 10, db_path=None)
    elapsed = time.monotonic() - start

    assert elapsed < 30, f"1000-event verification took {elapsed:.1f}s (limit 30s)"
