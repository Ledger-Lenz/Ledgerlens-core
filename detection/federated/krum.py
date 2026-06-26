"""Byzantine-Fault-Tolerant Krum / Multi-Krum aggregation (Issue #146).

Reference: Blanchard et al. (2017) "Machine Learning with Adversaries:
Byzantine Tolerant Gradient Descent".

Krum selects the gradient vector g_i that minimises the sum of squared
Euclidean distances to its (n - f - 2) nearest neighbours, where f is the
number of Byzantine clients to tolerate.  The algorithm is valid as long as
2f + 2 < n.

Multi-Krum extends this by returning the m lowest-scoring indices and
averaging their gradient vectors instead of using a single selected one,
offering a bias-variance tradeoff between pure Krum (m=1, lower bias) and
FedAvg (m=n, lower variance).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import List, Tuple

import numpy as np

logger = logging.getLogger("ledgerlens.federated.krum")


class KrumAggregator:
    """Krum / Multi-Krum Byzantine-fault-tolerant aggregator.

    Args:
        f: Number of Byzantine clients to tolerate.  Krum requires
           2f + 2 < n (validated at selection time, not construction time,
           because n is not known until ``select`` is called).
    """

    def __init__(self, f: int) -> None:
        if f < 0:
            raise ValueError(f"f must be non-negative, got {f}")
        self.f = f

    # ------------------------------------------------------------------

    def krum_scores(self, gradients: List[np.ndarray]) -> np.ndarray:
        """Compute Krum score for each gradient vector.

        score_i = sum of squared L2 distances to the (n - f - 2) nearest
        neighbours (self is excluded).

        Args:
            gradients: list of n flattened 1-D gradient vectors, each shape (D,).

        Returns:
            scores: shape (n,), lower means more central / trustworthy.
        """
        n = len(gradients)
        if 2 * self.f + 2 >= n:
            raise ValueError(
                f"Krum requires 2f+2 < n, got f={self.f}, n={n}. "
                f"Either reduce f or add more clients."
            )
        neighbours_to_sum = n - self.f - 2

        G = np.stack(gradients)  # (n, D)

        # Vectorised pairwise squared distances — O(n^2 * D) but no Python loop over D.
        # For very large n*D, fall back to chunked computation to avoid OOM.
        try:
            diffs = G[:, None, :] - G[None, :, :]  # (n, n, D)
            dists = np.sum(diffs ** 2, axis=-1)      # (n, n)
        except MemoryError:
            # Chunked fallback: compute row-by-row.
            dists = np.empty((n, n), dtype=np.float64)
            for i in range(n):
                dists[i] = np.sum((G - G[i]) ** 2, axis=1)

        scores = np.empty(n, dtype=np.float64)
        for i in range(n):
            row = np.sort(dists[i])
            # row[0] == 0 (self-distance); take indices 1 .. neighbours_to_sum (inclusive)
            scores[i] = row[1 : neighbours_to_sum + 1].sum()
        return scores

    # ------------------------------------------------------------------

    def select(
        self,
        gradients: List[np.ndarray],
        m: int = 1,
    ) -> Tuple[List[int], List[int], np.ndarray]:
        """Select the m most central gradients via Krum.

        Args:
            gradients: list of n flattened 1-D gradient vectors.
            m: number of gradients to select (m=1 → standard Krum,
               m>1 → Multi-Krum).  Must satisfy m <= n - f.

        Returns:
            (selected_indices, excluded_indices, scores)
        """
        n = len(gradients)
        if m > n - self.f:
            raise ValueError(
                f"m={m} must satisfy m <= n - f = {n - self.f}"
            )
        scores = self.krum_scores(gradients)
        ranked = np.argsort(scores)
        selected = ranked[:m].tolist()
        excluded = ranked[m:].tolist()
        return selected, excluded, scores


class KrumStrategy:
    """Byzantine-fault-tolerant federated aggregation using Krum / Multi-Krum.

    Validates ``2f+2 < min_clients`` at construction; raises ``ValueError`` if not.

    Args:
        f: Byzantine tolerance.  Default: ``floor(min_clients / 3)``.
        multi_krum_m: If None (default), use standard Krum (m=1).
                      If set, average the top-m selected updates (Multi-Krum).
        min_clients: Minimum clients per round; used for the ``2f+2 < n`` check.
        db_path: SQLite path for aggregation log.  Uses settings default if None.
    """

    def __init__(
        self,
        f: int | None = None,
        multi_krum_m: int | None = None,
        min_clients: int = 3,
        db_path: str | None = None,
    ) -> None:
        effective_f = f if f is not None else math.floor(min_clients / 3)
        if 2 * effective_f + 2 >= min_clients:
            raise ValueError(
                f"Byzantine tolerance f={effective_f} invalid: "
                f"need 2f+2 < min_clients={min_clients}"
            )
        self.f = effective_f
        self.m = multi_krum_m if multi_krum_m is not None else 1
        self.min_clients = min_clients
        self.db_path = db_path
        self._aggregator = KrumAggregator(f=self.f)
        self._round_number: int = 0
        # Track per-client exclusion counts for persistent-exclusion warning.
        self._exclusion_counts: dict[int, int] = {}
        self._round_counts: dict[int, int] = {}

    def aggregate(
        self,
        gradients: list[np.ndarray],
        client_ids: list[str] | None = None,
    ) -> np.ndarray:
        """Run Krum / Multi-Krum aggregation over ``gradients``.

        Args:
            gradients: list of n flattened gradient vectors, each shape (D,).
            client_ids: optional list of opaque client identifiers for logging.

        Returns:
            Aggregated gradient array of shape (D,).
        """
        n = len(gradients)
        selected, excluded, scores = self._aggregator.select(gradients, m=self.m)
        self._round_number += 1

        ids = client_ids or list(range(n))
        logger.info(
            "Krum round %d: selected=%s excluded=%s scores=%s",
            self._round_number,
            [ids[i] for i in selected],
            [ids[i] for i in excluded],
            scores.tolist(),
        )

        # Persistent Byzantine-actor warning: >50% exclusion rate.
        for idx in range(n):
            self._round_counts[idx] = self._round_counts.get(idx, 0) + 1
            if idx in excluded:
                self._exclusion_counts[idx] = self._exclusion_counts.get(idx, 0) + 1
            excl_rate = self._exclusion_counts.get(idx, 0) / self._round_counts[idx]
            if self._round_counts[idx] >= 2 and excl_rate > 0.5:
                client_label = ids[idx] if client_ids else idx
                logger.warning(
                    "Client %s has been excluded in %.0f%% of rounds "
                    "— possible persistent Byzantine actor",
                    client_label,
                    excl_rate * 100,
                )

        # Persist aggregation decision (import deferred to avoid circular deps).
        try:
            from detection.storage import log_krum_aggregation
            log_krum_aggregation(
                round_number=self._round_number,
                n_clients=n,
                f_tolerance=self.f,
                m_selected=self.m,
                selected_indices=selected,
                excluded_indices=excluded,
                krum_scores=scores.tolist(),
                db_path=self.db_path,
            )
        except Exception:
            logger.debug("Could not persist Krum aggregation log", exc_info=True)

        selected_grads = [gradients[i] for i in selected]
        return np.mean(selected_grads, axis=0)
