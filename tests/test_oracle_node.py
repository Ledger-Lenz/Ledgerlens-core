import os
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from detection.oracle_node import OracleNode

def test_canonical_message():
    # Known test vector
    wallet = "GBS2...ABCD"
    asset_pair = "XLM-USDC"
    score = 85
    timestamp = 1672531200
    
    # Must match the expected output
    # Prefix: LedgerLens-Oracle-v1
    msg = OracleNode._canonical_message(wallet, asset_pair, score, timestamp)
    assert len(msg) == 32  # SHA-256 digest size
    
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

        sig = node.sign_score_submission("wallet", "XLM-USDC", 90, 1672531200)
        assert len(sig) == 64
        assert node.last_seen is not None
