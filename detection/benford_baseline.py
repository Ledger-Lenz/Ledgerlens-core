"""Benford baseline calibration against market-wide digit distributions.

Computes and persists a per-asset-pair empirical baseline of leading-digit
frequencies from stored trades, allowing ``compute_benford_metrics`` to compare
a wallet's digit distribution against the market norm rather than the theoretical
Benford distribution.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.settings import settings


@dataclass
class BenfordBaseline:
    asset_pair: str
    digit_freqs: list[float]  # 9-element observed frequency array (digits 1-9)
    trade_count: int
    computed_at: datetime
    window_days: int


def _first_digit(value: float) -> int | None:
    """Return the leading decimal digit using the log10 method."""
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return int(10 ** (math.log10(value) % 1)) or 1


class BenfordBaselineCalibrator:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.db_path

    def calibrate(self, asset_pair: str, window_days: int = 30) -> BenfordBaseline:
        """Compute digit frequencies from stored trades and persist to ``benford_baselines``.

        Reads ``base_amount`` values from the ``trades`` table (falling back to
        the ``feature_vectors`` table when the ``trades`` table is unavailable),
        filtered to the given ``asset_pair`` and the last ``window_days`` days.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

        amounts: list[float] = []
        with sqlite3.connect(self._db_path) as conn:
            # Try the trades table first
            try:
                rows = conn.execute(
                    """
                    SELECT base_amount FROM trades
                    WHERE (base_asset_code || '/' || counter_asset_code) = ?
                       OR (counter_asset_code || '/' || base_asset_code) = ?
                    AND ledger_close_time >= ?
                    """,
                    (asset_pair, asset_pair, cutoff),
                ).fetchall()
                amounts = [r[0] for r in rows if r[0] is not None]
            except sqlite3.OperationalError:
                pass

            if not amounts:
                # Fall back to feature_vectors table
                try:
                    rows = conn.execute(
                        """
                        SELECT features_json FROM feature_vectors
                        WHERE asset_pair = ? AND timestamp >= ?
                        """,
                        (asset_pair, cutoff),
                    ).fetchall()
                    for (features_json,) in rows:
                        try:
                            features = json.loads(features_json)
                            val = features.get("base_amount") or features.get("volume_to_unique_counterparty_ratio")
                            if val is not None:
                                amounts.append(float(val))
                        except (json.JSONDecodeError, TypeError, ValueError):
                            continue
                except sqlite3.OperationalError:
                    pass

        # Compute digit frequency histogram
        counts = [0] * 9
        n = 0
        for amount in amounts:
            d = _first_digit(amount)
            if d is not None:
                counts[d - 1] += 1
                n += 1

        if n > 0:
            digit_freqs = [c / n for c in counts]
        else:
            # Fall back to theoretical Benford distribution
            digit_freqs = [math.log10(1 + 1 / d) for d in range(1, 10)]

        computed_at = datetime.now(timezone.utc)

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO benford_baselines
                    (asset_pair, digit_freqs_json, trade_count, computed_at, window_days)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(asset_pair) DO UPDATE SET
                    digit_freqs_json = excluded.digit_freqs_json,
                    trade_count = excluded.trade_count,
                    computed_at = excluded.computed_at,
                    window_days = excluded.window_days
                """,
                (
                    asset_pair,
                    json.dumps(digit_freqs),
                    n,
                    computed_at.isoformat(),
                    window_days,
                ),
            )
            conn.commit()

        return BenfordBaseline(
            asset_pair=asset_pair,
            digit_freqs=digit_freqs,
            trade_count=n,
            computed_at=computed_at,
            window_days=window_days,
        )

    def load(self, asset_pair: str) -> Optional[BenfordBaseline]:
        """Load a persisted baseline for ``asset_pair`` from ``benford_baselines``."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    """
                    SELECT digit_freqs_json, trade_count, computed_at, window_days
                    FROM benford_baselines
                    WHERE asset_pair = ?
                    """,
                    (asset_pair,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None

        if row is None:
            return None

        return BenfordBaseline(
            asset_pair=asset_pair,
            digit_freqs=json.loads(row[0]),
            trade_count=row[1],
            computed_at=datetime.fromisoformat(row[2]),
            window_days=row[3],
        )
