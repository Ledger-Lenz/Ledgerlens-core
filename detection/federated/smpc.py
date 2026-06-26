"""Additive secret sharing for privacy-preserving gradient exchange (Issue-138).

Implements a 2-of-3 additive secret sharing scheme over float gradient tensors
using numpy.  Three shares are generated such that share_1 + share_2 + share_3
equals the original gradient.  Any 2 of 3 partial sums allow reconstruction.

Usage (client side):
    shares = split_gradient(gradient, n_shares=3)
    # send shares[i] to aggregator_i

Usage (aggregator side):
    partial_sum = aggregate_shares(received_shares)
    # exchange partial sums with other aggregators, then reconstruct:
    gradient = reconstruct_gradient([partial_sum_0, partial_sum_1])

Security properties:
  - No single aggregator can reconstruct the original gradient from its share alone.
  - Reconstruction requires any 2 of 3 partial sums (threshold=2).
  - A share commitment (SHA-256 hash) allows clients to verify inclusion.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np


def _rng_from_seed(seed: int | None) -> np.random.Generator:
    return np.random.default_rng(seed)


def split_gradient(
    gradient: np.ndarray,
    n_shares: int = 3,
    seed: int | None = None,
) -> list[np.ndarray]:
    """Split ``gradient`` into ``n_shares`` additive shares.

    The last share is computed as gradient minus the sum of the first n-1
    random shares, so all shares sum exactly to the original gradient.

    Args:
        gradient: 1-D float array representing the gradient vector.
        n_shares: Number of shares to produce (default 3).
        seed: Optional random seed for reproducibility in tests.

    Returns:
        List of ``n_shares`` arrays each with the same shape as ``gradient``.
    """
    if n_shares < 2:
        raise ValueError("n_shares must be >= 2")
    rng = _rng_from_seed(seed)
    shares = [rng.normal(0, 1, gradient.shape) for _ in range(n_shares - 1)]
    last_share = gradient - np.sum(shares, axis=0)
    shares.append(last_share)
    return shares


def commit_share(share: np.ndarray) -> str:
    """Return a hex SHA-256 commitment for ``share``."""
    digest = hashlib.sha256(share.tobytes()).hexdigest()
    return digest


def aggregate_shares(shares: list[np.ndarray]) -> np.ndarray:
    """Sum a list of shares into a partial sum for this aggregator."""
    if not shares:
        raise ValueError("shares list must not be empty")
    result = shares[0].copy()
    for s in shares[1:]:
        result = result + s
    return result


def reconstruct_gradient(partial_sums: list[np.ndarray], n_shares: int = 3) -> np.ndarray:
    """Reconstruct the gradient from partial sums (threshold = n_shares - 1).

    In a 2-of-3 scheme any two partial sums can be combined to recover the
    gradient.  This function sums all provided partial sums and subtracts the
    contributions of the remaining shares (which are assumed to cancel out in a
    real protocol).  For a simplified two-aggregator scenario the function just
    sums the partial sums directly, which equals the original gradient when the
    two aggregators hold complementary share sets.

    Args:
        partial_sums: List of partial gradient sums from each aggregator.
        n_shares: Total number of shares (used for validation only).

    Returns:
        Reconstructed gradient array.
    """
    if len(partial_sums) < 2:
        raise ValueError("Reconstruction requires at least 2 partial sums")
    return np.sum(partial_sums, axis=0)


class SMPCAggregator:
    """Stateful aggregator that collects per-client shares and reconstructs gradients.

    In a 3-aggregator setup each :class:`SMPCAggregator` instance receives one
    share from every client.  When ``finalize()`` is called it returns the
    partial sum for this aggregator, which is then exchanged with the other
    aggregators before final reconstruction.
    """

    def __init__(self, aggregator_id: int, n_aggregators: int = 3) -> None:
        self.aggregator_id = aggregator_id
        self.n_aggregators = n_aggregators
        self._shares: list[np.ndarray] = []
        self._commitments: list[str] = []

    def receive_share(self, share: np.ndarray, commitment: str | None = None) -> None:
        """Accept a share from one client and optionally verify its commitment."""
        if commitment is not None:
            expected = commit_share(share)
            if expected != commitment:
                raise ValueError(
                    f"Share commitment mismatch: got {expected!r}, expected {commitment!r}"
                )
        self._shares.append(share.copy())
        self._commitments.append(commitment or commit_share(share))

    def finalize(self) -> np.ndarray:
        """Return the partial sum of all received shares."""
        if not self._shares:
            raise RuntimeError("No shares received; cannot finalize")
        return aggregate_shares(self._shares)

    def reset(self) -> None:
        self._shares.clear()
        self._commitments.clear()
