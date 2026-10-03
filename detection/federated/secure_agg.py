"""Secure aggregation via pairwise masking (Issue #1034).

Bonawitz-style secure aggregation: every pair of participants derives a shared
seed with X25519 + HKDF, expands it into a pseudorandom mask, and one side adds
the mask while the other subtracts it. Each participant uploads only its masked
update; the masks cancel exactly in the sum, so the coordinator learns the
aggregate and nothing about any single participant's raw update.

Arithmetic is fixed-point over the ring Z/2^64 (numpy ``uint64`` wraps), so
cancellation is exact rather than subject to float rounding.

Usage::

    participants = {pid: SecureAggParticipant(pid) for pid in ids}
    public_keys = {pid: p.public_key_bytes() for pid, p in participants.items()}
    coordinator = SecureAggregator(expected_participants=ids, round_id="r1")
    for pid, p in participants.items():
        coordinator.submit(pid, p.mask_update(update[pid], public_keys, "r1"))
    total = coordinator.aggregate()  # == sum of raw updates

See ``docs/federated_learning.md`` ("Secure Aggregation") for the privacy
guarantee and its assumptions.
"""

from __future__ import annotations

import numpy as np
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# With two participants, either one can subtract its own update from the
# aggregate and recover the other's, so masking protects nothing.
MIN_PARTICIPANTS = 3

# Fixed-point scale: values are encoded as round(x * 2**FRAC_BITS).
FRAC_BITS = 24


def encode(update: np.ndarray) -> np.ndarray:
    """Encode a float vector as fixed-point elements of Z/2^64."""
    scaled = np.round(np.asarray(update, dtype=np.float64) * (1 << FRAC_BITS))
    return scaled.astype(np.int64).view(np.uint64)


def decode(encoded: np.ndarray) -> np.ndarray:
    """Decode fixed-point ring elements back to floats."""
    return encoded.view(np.int64).astype(np.float64) / (1 << FRAC_BITS)


def _pair_mask(shared_secret: bytes, round_id: str, size: int) -> np.ndarray:
    seed = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"ledgerlens-secagg:{round_id}".encode(),
    ).derive(shared_secret)
    rng = np.random.default_rng(int.from_bytes(seed, "big"))
    return rng.integers(0, np.iinfo(np.uint64).max, size=size, dtype=np.uint64, endpoint=True)


class SecureAggParticipant:
    """Client side: holds a key pair and masks its own update."""

    def __init__(self, participant_id: str) -> None:
        self.participant_id = participant_id
        self._private_key = X25519PrivateKey.generate()

    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def mask_update(
        self,
        update: np.ndarray,
        public_keys: dict[str, bytes],
        round_id: str,
    ) -> np.ndarray:
        """Return the masked, fixed-point-encoded update for upload.

        ``public_keys`` maps every expected participant id (including this
        one) to its X25519 public key.
        """
        masked = encode(update).ravel().copy()
        for peer_id, peer_key in public_keys.items():
            if peer_id == self.participant_id:
                continue
            secret = self._private_key.exchange(X25519PublicKey.from_public_bytes(peer_key))
            mask = _pair_mask(secret, round_id, masked.size)
            # The lower id adds, the higher id subtracts, so each pair cancels.
            if self.participant_id < peer_id:
                masked += mask
            else:
                masked -= mask
        return masked


class SecureAggregator:
    """Coordinator side: only ever holds masked updates and their sum.

    There is deliberately no accessor for individual submissions; the only
    output is :meth:`aggregate`, which refuses to run until every expected
    participant has reported (partial sums would leave masks uncancelled).
    """

    def __init__(self, expected_participants: list[str], round_id: str) -> None:
        if len(set(expected_participants)) < MIN_PARTICIPANTS:
            raise ValueError(
                f"secure aggregation requires at least {MIN_PARTICIPANTS} participants"
            )
        self.round_id = round_id
        self._expected = frozenset(expected_participants)
        self._reported: set[str] = set()
        self._running_sum: np.ndarray | None = None

    def submit(self, participant_id: str, masked_update: np.ndarray) -> None:
        if participant_id not in self._expected:
            raise ValueError(f"unexpected participant {participant_id!r}")
        if participant_id in self._reported:
            raise ValueError(f"participant {participant_id!r} already reported")
        masked = np.asarray(masked_update, dtype=np.uint64)
        if self._running_sum is None:
            self._running_sum = masked.copy()
        elif masked.shape != self._running_sum.shape:
            raise ValueError("masked update shape mismatch")
        else:
            self._running_sum += masked
        self._reported.add(participant_id)

    def aggregate(self) -> np.ndarray:
        """Return the sum of all raw updates once every participant reported."""
        missing = self._expected - self._reported
        if missing:
            raise RuntimeError(f"cannot unmask aggregate: missing participants {sorted(missing)}")
        return decode(self._running_sum)
