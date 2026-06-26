"""Differential privacy utilities for federated learning (Issue #145).

Implements DP-SGD privacy accounting via the RDP accountant and noise
multiplier calibration. Used by the FL client to bound the privacy cost
of gradient updates across training rounds.

Key design decisions:
- PrivacyAccountant wraps opacus.accountants.RDPAccountant for tight RDP bounds.
- calibrate_noise_multiplier uses opacus.accountants.utils.get_noise_multiplier.
- delta must be << 1/n_training_samples (validated at calibration time).
- Gradient tensors are never logged; only scalar norms and epsilon values.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("ledgerlens.federated.privacy")


class PrivacyBudgetExhaustedError(Exception):
    """Raised when the DP privacy budget (ε) has been exhausted."""


class PrivacyAccountant:
    """Tracks cumulative (ε, δ) privacy cost across DP-SGD training steps.

    Wraps opacus.accountants.RDPAccountant for tight Rényi DP bounds.
    Call step() after each batch, get_epsilon() to read the current cost,
    and budget_exhausted() to check whether training should halt.
    """

    def __init__(self, noise_multiplier: float, sample_rate: float, delta: float):
        """
        Args:
            noise_multiplier: σ / clip_norm ratio for Gaussian mechanism.
            sample_rate: Fraction of dataset sampled per batch (Poisson).
            delta: Target δ for (ε, δ)-DP guarantee.
        """
        from opacus.accountants import RDPAccountant

        self._accountant = RDPAccountant()
        self.noise_multiplier = noise_multiplier
        self.sample_rate = sample_rate
        self.delta = delta
        self._total_steps = 0

    def step(self, num_steps: int = 1) -> None:
        """Record num_steps of DP-SGD; call after each optimizer step."""
        self._accountant.step(
            noise_multiplier=self.noise_multiplier,
            sample_rate=self.sample_rate,
            num_steps=num_steps,
        )
        self._total_steps += num_steps

    def get_epsilon(self) -> float:
        """Return the current cumulative ε for self.delta."""
        if self._total_steps == 0:
            return 0.0
        return float(self._accountant.get_epsilon(self.delta))

    def budget_exhausted(self, target_epsilon: float) -> bool:
        """Return True if the current ε >= target_epsilon."""
        return self.get_epsilon() >= target_epsilon


def calibrate_noise_multiplier(
    target_epsilon: float,
    delta: float,
    sample_rate: float,
    epochs: int,
    steps_per_epoch: int,
    n_samples: Optional[int] = None,
) -> float:
    """Compute the noise_multiplier needed to achieve (target_epsilon, delta)-DP.

    Args:
        target_epsilon: Target ε privacy budget.
        delta: Target δ. Must be << 1/n_samples if n_samples is provided.
        sample_rate: Fraction of dataset per batch (Poisson subsampling rate).
        epochs: Number of local training epochs per FL round.
        steps_per_epoch: Number of optimizer steps per epoch.
        n_samples: Optional dataset size for delta validation.

    Returns:
        Calibrated noise_multiplier (positive float).

    Raises:
        ValueError: If delta >= 1/n_samples (trivially weak DP guarantee).
    """
    from opacus.accountants.utils import get_noise_multiplier

    if n_samples is not None and delta >= 1.0 / n_samples:
        raise ValueError(
            f"delta={delta} must be << 1/n_samples={1.0/n_samples:.2e}. "
            f"A delta this large makes the DP guarantee trivially weak."
        )

    nm = get_noise_multiplier(
        target_epsilon=target_epsilon,
        target_delta=delta,
        sample_rate=sample_rate,
        epochs=epochs,
        accountant="rdp",
    )
    logger.info(
        "Calibrated noise_multiplier=%.4f for (ε=%.2f, δ=%.2e, sample_rate=%.4f, epochs=%d)",
        nm, target_epsilon, delta, sample_rate, epochs,
    )
    return float(nm)


# ---------------------------------------------------------------------------
# FL privacy log (SQLite persistence)
# ---------------------------------------------------------------------------

_FL_PRIVACY_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS fl_privacy_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    epsilon         REAL NOT NULL,
    delta           REAL NOT NULL,
    noise_multiplier REAL NOT NULL,
    clip_norm       REAL NOT NULL,
    budget_exhausted INTEGER NOT NULL DEFAULT 0,
    recorded_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def init_fl_privacy_log(db_path: str) -> None:
    """Ensure the fl_privacy_log table exists."""
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_FL_PRIVACY_LOG_SCHEMA)
        conn.commit()


def record_privacy_round(
    round_number: int,
    epsilon: float,
    delta: float,
    noise_multiplier: float,
    clip_norm: float,
    budget_exhausted: bool,
    db_path: str,
) -> None:
    """Persist per-round privacy accounting to fl_privacy_log."""
    init_fl_privacy_log(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO fl_privacy_log "
            "(round_number, epsilon, delta, noise_multiplier, clip_norm, budget_exhausted) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (round_number, epsilon, delta, noise_multiplier, clip_norm, int(budget_exhausted)),
        )
        conn.commit()


def get_privacy_log(db_path: str) -> list[dict]:
    """Return all fl_privacy_log rows ordered by round_number."""
    init_fl_privacy_log(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT round_number, epsilon, delta, noise_multiplier, clip_norm, "
            "budget_exhausted, recorded_at FROM fl_privacy_log ORDER BY round_number ASC"
        ).fetchall()
    return [
        {
            "round_number": r[0],
            "epsilon": r[1],
            "delta": r[2],
            "noise_multiplier": r[3],
            "clip_norm": r[4],
            "budget_exhausted": bool(r[5]),
            "recorded_at": r[6],
        }
        for r in rows
    ]
