import hashlib
import os
import struct
import time
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


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
        self.last_seen: Optional[float] = None

    @property
    def public_key_hex(self) -> str:
        pub = self._private_key.public_key()
        return pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def sign_score_submission(
        self, wallet: str, asset_pair: str, score: int, timestamp: int
    ) -> bytes:
        """
        Sign canonical message: SHA-256("LedgerLens-Oracle-v1" || wallet || asset_pair || score_u32_be || timestamp_u64_be)
        Returns 64-byte ED25519 signature.
        """
        message = self._canonical_message(wallet, asset_pair, score, timestamp)
        sig = self._private_key.sign(message)
        self.last_seen = time.time()
        return sig

    @staticmethod
    def _canonical_message(wallet: str, asset_pair: str, score: int, timestamp: int) -> bytes:
        prefix = b"LedgerLens-Oracle-v1"
        body = (
            prefix
            + wallet.encode("utf-8")
            + b"|"
            + asset_pair.encode("utf-8")
            + b"|"
            + struct.pack(">I", score)
            + struct.pack(">Q", timestamp)
        )
        return hashlib.sha256(body).digest()
