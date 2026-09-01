import hashlib
import os
import struct
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from detection.oracle_node import OracleNode

def test_canonical_message():
    # Known test vector
    wallet = "GBS2...ABCD"
    asset_pair = "XLM_USDC"
    score = 85
    timestamp = 1672531200
    confidence = 91
    model_version = 7
    
    msg = OracleNode._canonical_message(
        wallet,
        asset_pair,
        score,
        True,
        False,
        timestamp,
        confidence,
        model_version,
    )

    symbol = asset_pair.encode("ascii")
    symbol_xdr = struct.pack(">iI", 15, len(symbol)) + symbol
    symbol_xdr += b"\x00" * ((-len(symbol)) % 4)
    expected = hashlib.sha256(
        b"LedgerLens-Oracle-v2"
        + wallet.encode()
        + b"|"
        + symbol_xdr
        + b"|"
        + struct.pack(">I??QII", score, True, False, timestamp, confidence, model_version)
    ).digest()
    assert msg == expected


def test_canonical_message_covers_destination_metadata():
    base = OracleNode._canonical_message(
        "GBS2...ABCD", "XLM_USDC", 85, True, False, 1672531200, 91, 7
    )
    variants = [
        OracleNode._canonical_message(
            "GBS2...ABCD", "XLM_USDC", 85, False, False, 1672531200, 91, 7
        ),
        OracleNode._canonical_message(
            "GBS2...ABCD", "XLM_USDC", 85, True, True, 1672531200, 91, 7
        ),
        OracleNode._canonical_message(
            "GBS2...ABCD", "XLM_USDC", 85, True, False, 1672531200, 90, 7
        ),
        OracleNode._canonical_message(
            "GBS2...ABCD", "XLM_USDC", 85, True, False, 1672531200, 91, 8
        ),
    ]
    assert all(value != base for value in variants)


def test_canonical_message_rejects_non_symbol_asset_pair():
    with pytest.raises(ValueError, match="Soroban Symbol"):
        OracleNode._canonical_message(
            "GBS2...ABCD", "XLM/USDC", 85, True, False, 1672531200, 91, 7
        )
    
def test_sign_score_submission():
    private_key = Ed25519PrivateKey.generate()
    key_hex = private_key.private_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.Raw,
        format=__import__("cryptography").hazmat.primitives.serialization.PrivateFormat.Raw,
        encryption_algorithm=__import__("cryptography").hazmat.primitives.serialization.NoEncryption()
    ).hex()

    with mock.patch.dict(os.environ, {"ORACLE_NODE_1_KEY": key_hex}):
        node = OracleNode(name="oracle-1", private_key_env_var="ORACLE_NODE_1_KEY")
        assert node.public_key_hex == private_key.public_key().public_bytes(
            encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.Raw,
            format=__import__("cryptography").hazmat.primitives.serialization.PublicFormat.Raw
        ).hex()

        sig = node.sign_score_submission(
            "wallet", "XLM_USDC", 90, True, False, 1672531200, 91, 7
        )
        assert len(sig) == 64
        assert node.last_seen is not None
