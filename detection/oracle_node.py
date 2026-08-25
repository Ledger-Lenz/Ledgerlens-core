from __future__ import annotations

import hashlib
import os
import struct
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ORACLE_DOMAIN_SEPARATOR = b"LedgerLens-Oracle-v2"
SOROBAN_SYMBOL_SCVAL_TYPE = 15
MAX_SYMBOL_LENGTH = 32


class OracleNode:
    """
    Oracle node encapsulating an ED25519 keypair for threshold signing.
    """

    def __init__(self, name: str, private_key_env_var: str):
        """
        Load ED25519 private key from environment variable (32 hex-encoded bytes).
        Raises EnvironmentError if the variable is not set.
        """
        raw = os.environ.get(private_key_env_var)
        if not raw:
            raise EnvironmentError(f"Oracle key not set: {private_key_env_var}")
        
        try:
            key_bytes = bytes.fromhex(raw)
            if len(key_bytes) != 32:
                raise ValueError("Key must be 32 bytes")
            self._private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
        except Exception as e:
            raise EnvironmentError(f"Invalid oracle key format in {private_key_env_var}: {e}")
            
        self.name = name
        self.last_seen: float | None = None

    @property
    def public_key_hex(self) -> str:
        pub = self._private_key.public_key()
        return pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def sign_score_submission(
        self,
        wallet: str,
        asset_pair: str,
        score: int,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: int,
        confidence: int,
        model_version: int,
    ) -> bytes:
        """
        Sign every caller-controlled field forwarded to ledgerlens-score.

        Returns 64-byte ED25519 signature.
        """
        message = self._canonical_message(
            wallet,
            asset_pair,
            score,
            benford_flag,
            ml_flag,
            timestamp,
            confidence,
            model_version,
        )
        sig = self._private_key.sign(message)
        self.last_seen = time.time()
        return sig

    @staticmethod
    def _canonical_message(
        wallet: str,
        asset_pair: str,
        score: int,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: int,
        confidence: int,
        model_version: int,
    ) -> bytes:
        OracleNode._validate_payload(score, confidence, timestamp, model_version)
        body = (
            ORACLE_DOMAIN_SEPARATOR
            + wallet.encode("utf-8")
            + b"|"
            + OracleNode._symbol_xdr(asset_pair)
            + b"|"
            + struct.pack(">I", score)
            + struct.pack(">?", benford_flag)
            + struct.pack(">?", ml_flag)
            + struct.pack(">Q", timestamp)
            + struct.pack(">I", confidence)
            + struct.pack(">I", model_version)
        )
        return hashlib.sha256(body).digest()

    @staticmethod
    def _symbol_xdr(value: str) -> bytes:
        encoded = value.encode("ascii")
        if (
            len(encoded) > MAX_SYMBOL_LENGTH
            or not encoded
            or any(not (chr(byte).isalnum() or byte == ord("_")) for byte in encoded)
        ):
            raise ValueError(
                "asset_pair must be a non-empty Soroban Symbol "
                "(ASCII alphanumeric/underscore, at most 32 bytes)"
            )
        padding = b"\x00" * ((-len(encoded)) % 4)
        return struct.pack(">iI", SOROBAN_SYMBOL_SCVAL_TYPE, len(encoded)) + encoded + padding

    @staticmethod
    def _validate_payload(score: int, confidence: int, timestamp: int, model_version: int) -> None:
        if not 0 <= score <= 100:
            raise ValueError("score must be between 0 and 100")
        if not 0 <= confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        if not 0 <= timestamp <= 2**64 - 1:
            raise ValueError("timestamp must fit u64")
        if not 0 <= model_version <= 2**32 - 1:
            raise ValueError("model_version must fit u32")
