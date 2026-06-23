"""Tests for ingestion/bridge_loader.py."""

from datetime import datetime, timezone

import pytest
import responses as responses_lib

from ingestion.bridge_loader import (
    ALLBRIDGE_TOKENS_SENT_TOPIC,
    BridgeTransferLoader,
    decode_bytes32_to_stellar,
)
from ingestion.data_models import BridgeTransfer

MOCK_RPC_URL = "https://mock-bridge-rpc.example.com"
BRIDGE_CONTRACT = "0x7DBF072b9E4E7Eb4B509E5f8afc08f24A61AC67E"


def _rpc_response(result) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _make_tokens_sent_log(recipient_bytes: bytes, amount: int = 1_000_000) -> dict:
    """Build a synthetic Allbridge TokensSent event log."""
    sender = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
    sender_padded = "0x" + "0" * 24 + sender[2:].lower()

    recipient_hex = recipient_bytes.hex().zfill(64)
    amount_hex = hex(amount)[2:].zfill(64)

    return {
        "topics": [
            ALLBRIDGE_TOKENS_SENT_TOPIC,
            sender_padded,
        ],
        "data": "0x" + recipient_hex + amount_hex,
        "transactionHash": "0x" + "ab" * 32,
        "blockNumber": "0x1",
        "address": BRIDGE_CONTRACT,
        "removed": False,
    }


# Unix timestamp for 2026-06-19 (within the 90-day storage window)
_RECENT_TS = 1_781_827_200

MOCK_BLOCK = {
    "result": {
        "timestamp": hex(_RECENT_TS),
        "number": "0x1",
    }
}


# ---------------------------------------------------------------------------
# (a) Allbridge TokensSent event correctly parsed into BridgeTransfer
# ---------------------------------------------------------------------------


@responses_lib.activate
def test_allbridge_tokens_sent_parsed_into_bridge_transfer():
    """A well-formed TokensSent log is parsed into a BridgeTransfer."""
    from stellar_sdk import Keypair
    stellar_kp = Keypair.random()
    recipient_raw = stellar_kp.raw_public_key()
    log = _make_tokens_sent_log(recipient_raw)

    responses_lib.add(responses_lib.POST, MOCK_RPC_URL, json=_rpc_response("0x64"))
    responses_lib.add(responses_lib.POST, MOCK_RPC_URL, json=_rpc_response([log]))
    responses_lib.add(responses_lib.POST, MOCK_RPC_URL, json=MOCK_BLOCK)

    loader = BridgeTransferLoader(
        chain="ethereum",
        rpc_url=MOCK_RPC_URL,
        contract_address=BRIDGE_CONTRACT,
    )
    # Pass db_path=None so we don't try to save during the test
    transfers = loader.load_transfers(lookback_blocks=10, db_path=None)

    assert len(transfers) == 1
    t = transfers[0]
    assert isinstance(t, BridgeTransfer)
    assert t.chain == "ethereum"
    assert t.direction == "evm_to_stellar"
    assert t.evm_wallet == "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
    assert t.stellar_wallet == stellar_kp.public_key
    assert t.tx_hash_evm == log["transactionHash"]


# ---------------------------------------------------------------------------
# (b) recipient bytes32 correctly decoded to Stellar G-address
# ---------------------------------------------------------------------------


def test_decode_bytes32_to_stellar_returns_g_address():
    """decode_bytes32_to_stellar produces a valid Stellar G-address."""
    from stellar_sdk import Keypair
    stellar_kp = Keypair.random()
    raw = stellar_kp.raw_public_key()
    result = decode_bytes32_to_stellar(raw)
    assert result.startswith("G")
    assert len(result) == 56
    assert result == stellar_kp.public_key


def test_decode_bytes32_wrong_length_raises():
    """Non-32-byte input raises ValueError."""
    with pytest.raises(ValueError, match="Expected 32 bytes"):
        decode_bytes32_to_stellar(b"\x00" * 16)


def test_decode_bytes32_known_vector():
    """Deterministic test with a fixed 32-byte input."""
    raw_bytes = bytes.fromhex(
        "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29"
    )
    result = decode_bytes32_to_stellar(raw_bytes)
    assert result.startswith("G")
    # Verify round-trip: the same bytes produce the same address
    assert decode_bytes32_to_stellar(raw_bytes) == result


# ---------------------------------------------------------------------------
# (c) Bridge transfers are stored and retrievable from SQLite
# ---------------------------------------------------------------------------


def test_bridge_transfers_stored_and_retrievable(tmp_path):
    """Saving a BridgeTransfer and retrieving it returns the same record."""
    from datetime import timedelta
    from stellar_sdk import Keypair
    from detection.storage import get_bridge_transfers, save_bridge_transfer

    db = str(tmp_path / "test.db")
    stellar_kp = Keypair.random()
    recent_ts = datetime.now(timezone.utc) - timedelta(days=1)

    transfer = BridgeTransfer(
        chain="ethereum",
        direction="evm_to_stellar",
        evm_wallet="0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B",
        stellar_wallet=stellar_kp.public_key,
        amount_usd=1234.56,
        token="USDC",
        tx_hash_evm="0x" + "ab" * 32,
        tx_hash_stellar=None,
        timestamp=recent_ts,
    )
    save_bridge_transfer(transfer, db_path=db)

    results = get_bridge_transfers(stellar_wallet=stellar_kp.public_key, db_path=db)
    assert len(results) == 1
    r = results[0]
    assert r.chain == "ethereum"
    assert r.evm_wallet == transfer.evm_wallet
    assert r.stellar_wallet == stellar_kp.public_key
    assert r.amount_usd == pytest.approx(1234.56)


def test_bridge_transfers_empty_for_unknown_wallet(tmp_path):
    """No bridge transfers exist for a fresh DB and unknown wallet."""
    from detection.storage import get_bridge_transfers

    db = str(tmp_path / "test.db")
    results = get_bridge_transfers(stellar_wallet="GNOTEXIST", db_path=db)
    assert results == []


def test_bridge_transfers_persist_via_loader(tmp_path):
    """BridgeTransferLoader.load_transfers() saves records to the DB."""
    from stellar_sdk import Keypair
    from detection.storage import get_bridge_transfers

    stellar_kp = Keypair.random()
    log = _make_tokens_sent_log(stellar_kp.raw_public_key())
    db = str(tmp_path / "test.db")

    with responses_lib.RequestsMock() as rsps:
        rsps.add(rsps.POST, MOCK_RPC_URL, json=_rpc_response("0x64"))
        rsps.add(rsps.POST, MOCK_RPC_URL, json=_rpc_response([log]))
        rsps.add(rsps.POST, MOCK_RPC_URL, json=MOCK_BLOCK)

        loader = BridgeTransferLoader(
            chain="ethereum",
            rpc_url=MOCK_RPC_URL,
            contract_address=BRIDGE_CONTRACT,
        )
        loader.load_transfers(lookback_blocks=10, db_path=db)

    results = get_bridge_transfers(stellar_wallet=stellar_kp.public_key, db_path=db)
    assert len(results) == 1
    assert results[0].evm_wallet == "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
