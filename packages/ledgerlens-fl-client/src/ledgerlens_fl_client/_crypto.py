"""Ed25519 cryptographic operations for FL client authentication.

Implements key generation, payload signing, and DER encoding compatible
with the LedgerLens federated aggregation server protocol.
"""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

import numpy as np


def generate_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    """Generate a new Ed25519 keypair.
    
    Returns
    -------
    tuple
        (private_key, public_key_der_bytes)
    """
    private_key = Ed25519PrivateKey.generate()
    public_key_der = private_key.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    return private_key, public_key_der


def public_key_to_der_b64(private_key: Ed25519PrivateKey) -> str:
    """Encode public key as base64 DER string.
    
    Parameters
    ----------
    private_key : Ed25519PrivateKey
        Private key (used to derive public key).
    
    Returns
    -------
    str
        Base64-encoded DER public key.
    """
    der_bytes = private_key.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der_bytes).decode()


def build_payload_bytes(
    participant_id: str,
    round_id: str,
    soft_labels: np.ndarray,
    n_samples: int,
) -> bytes:
    """Build canonical JSON payload for signing.
    
    Matches the format expected by FederatedAggregationServer.submit_update().
    
    Parameters
    ----------
    participant_id : str
        Operator identifier.
    round_id : str
        Federated round UUID.
    soft_labels : np.ndarray
        Soft label predictions.
    n_samples : int
        Number of training samples.
    
    Returns
    -------
    bytes
        Canonical JSON payload (sort_keys=True).
    """
    payload_dict = {
        "participant_id": participant_id,
        "round_id": round_id,
        "soft_labels": soft_labels.tolist(),
        "n_samples": n_samples,
    }
    return json.dumps(payload_dict, sort_keys=True).encode()


def sign_payload(
    private_key: Ed25519PrivateKey,
    participant_id: str,
    round_id: str,
    soft_labels: np.ndarray,
    n_samples: int,
) -> bytes:
    """Sign an FL update payload with Ed25519.
    
    Parameters
    ----------
    private_key : Ed25519PrivateKey
        Private key for signing.
    participant_id : str
        Operator identifier.
    round_id : str
        Federated round UUID.
    soft_labels : np.ndarray
        Soft label predictions.
    n_samples : int
        Number of training samples.
    
    Returns
    -------
    bytes
        Ed25519 signature.
    """
    payload_bytes = build_payload_bytes(participant_id, round_id, soft_labels, n_samples)
    return private_key.sign(payload_bytes)


def verify_signature(
    signature: bytes,
    payload_bytes: bytes,
    public_key_der: bytes,
) -> bool:
    """Verify an Ed25519 signature.
    
    Parameters
    ----------
    signature : bytes
        Signature to verify.
    payload_bytes : bytes
        Original payload that was signed.
    public_key_der : bytes
        DER-encoded public key.
    
    Returns
    -------
    bool
        True if signature is valid.
    """
    from cryptography.exceptions import InvalidSignature
    
    public_key = Ed25519PublicKey.from_public_bytes(public_key_der)
    try:
        public_key.verify(signature, payload_bytes)
        return True
    except InvalidSignature:
        return False