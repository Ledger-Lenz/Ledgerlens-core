"""Build labelled feature datasets for `detection.model_training`.

Turns a `Trade`/`OrderBookEvent`/account-metadata bundle — either from
`ingestion.historical_loader` + `ingestion.account_loader` +
`ingestion.operations_loader`, or from
`ingestion.synthetic_data.generate_synthetic_dataset` for local
development — into a feature matrix with one row per labelled account,
ready for `detection.model_training.train_ensemble`.

Temporal Splitting
------------------
``temporal_train_val_split()`` replaces random ``train_test_split`` with a
strict chronological split plus a purge gap to prevent data leakage from
overlapping feature windows. ``walk_forward_cv()`` yields walk-forward
(rolling-origin) cross-validation folds with configurable gap and minimum
training duration. ``data_leakage_audit()`` raises ``DataLeakageError`` if
any validation sample's feature window overlaps training data.
"""

from __future__ import annotations

import logging
from typing import Generator, Tuple

import numpy as np
import pandas as pd

from detection.feature_engineering import FEATURE_NAMES, build_feature_vector
from detection.graph_engine import build_ring_membership_index, build_transaction_graph, find_wash_rings

logger = logging.getLogger("ledgerlens.dataset")


def build_training_dataset(
    trades: pd.DataFrame,
    labels: dict[str, int],
    account_metadata: dict[str, dict] | None = None,
    order_book_events: pd.DataFrame | None = None,
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build a `FEATURE_NAMES + ["wallet", "label"]` DataFrame, one row per account in `labels`.

    `as_of` defaults to the latest `ledger_close_time` in `trades`.
    """
    if trades.empty:
        return pd.DataFrame(columns=[*FEATURE_NAMES, "wallet", "label"])

    as_of = as_of or pd.Timestamp(trades["ledger_close_time"].max())
    account_metadata = account_metadata or {}
    graph = build_transaction_graph(trades)
    rings = find_wash_rings(graph)
    ring_membership = build_ring_membership_index(rings, trades=trades)

    rows = []
    for account, label in labels.items():
        account_events = (
            order_book_events[order_book_events["account"] == account]
            if order_book_events is not None
            else None
        )
        features = build_feature_vector(
            trades,
            account,
            as_of,
            order_book_events=account_events,
            account_metadata=account_metadata,
            ring_membership=ring_membership,
        )
        features["wallet"] = account
        features["label"] = label
        rows.append(features)

    return pd.DataFrame(rows, columns=[*FEATURE_NAMES, "wallet", "label"])


# ---------------------------------------------------------------------------
# Temporal splitting
# ---------------------------------------------------------------------------


class DataLeakageError(Exception):
    pass


def temporal_train_val_split(
    X: np.ndarray,
    y: np.ndarray,
    timestamps: np.ndarray,
    val_ratio: float = 0.20,
    gap_days: float = 7.0,
    max_window_days: float = 30.0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Split data chronologically with a purge gap to prevent leakage.

    Sorts by timestamp, uses the earliest (1-val_ratio) fraction for training,
    then skips a purge gap of ``gap_days`` plus ``max_window_days`` to ensure
    no validation sample's feature window overlaps any training timestamp.
    Returns (X_train, X_val, y_train, y_val).
    """
    sort_idx = np.argsort(timestamps)
    X = X[sort_idx]
    y = y[sort_idx]
    timestamps = timestamps[sort_idx]

    cutoff_ts = timestamps[int(len(timestamps) * (1 - val_ratio))]
    purge_start_ts = cutoff_ts - max_window_days * 86400
    purge_end_ts = cutoff_ts + gap_days * 86400

    train_mask = timestamps < purge_start_ts
    val_mask = timestamps >= purge_end_ts

    X_train, X_val = X[train_mask], X[val_mask]
    y_train, y_val = y[train_mask], y[val_mask]

    if len(X_val) == 0:
        logger.warning(
            "Temporal split produced empty validation set (gap_days=%.1f, "
            "max_window_days=%.1f, dataset span=%.1f days). "
            "Falling back to random split.",
            gap_days,
            max_window_days,
            (timestamps[-1] - timestamps[0]) / 86400,
        )
        from sklearn.model_selection import train_test_split
        return train_test_split(X, y, test_size=val_ratio, random_state=42)

    return X_train, X_val, y_train, y_val


def walk_forward_cv(
    X: np.ndarray,
    y: np.ndarray,
    timestamps: np.ndarray,
    n_splits: int = 5,
    gap_days: float = 7.0,
    min_train_days: float = 60.0,
) -> Generator[Tuple[np.ndarray, np.ndarray], None, None]:
    """Walk-forward (rolling-origin) cross-validation respecting chronological order.

    Yields ``(train_indices, val_indices)`` tuples. Each fold uses an expanding
    training window and a fixed-duration validation window, with a ``gap_days``
    purge gap between them.
    """
    sort_idx = np.argsort(timestamps)
    ts = timestamps[sort_idx]

    total_span = ts[-1] - ts[0]
    fold_duration = total_span / (n_splits + 1)

    for i in range(1, n_splits + 1):
        train_end = ts[0] + fold_duration * i
        val_start = train_end + gap_days * 86400
        val_end = val_start + fold_duration

        train_idx = sort_idx[ts < train_end]
        val_idx = sort_idx[(ts >= val_start) & (ts < val_end)]

        if len(train_idx) > 0 and len(val_idx) > 0:
            yield train_idx, val_idx


def data_leakage_audit(
    train_timestamps: np.ndarray,
    val_timestamps: np.ndarray,
    max_window_seconds: float,
) -> None:
    """Raise DataLeakageError if any validation sample's feature window overlaps training data."""
    if len(train_timestamps) == 0 or len(val_timestamps) == 0:
        return
    val_window_start = val_timestamps.min() - max_window_seconds
    if val_window_start < train_timestamps.max():
        raise DataLeakageError(
            f"Leakage detected: earliest val feature window ({val_window_start:.0f}) "
            f"overlaps train data (latest: {train_timestamps.max():.0f})"
        )
