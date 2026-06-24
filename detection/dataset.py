"""Build labelled feature datasets for `detection.model_training`.

Turns a `Trade`/`OrderBookEvent`/account-metadata bundle — either from
`ingestion.historical_loader` + `ingestion.account_loader` +
`ingestion.operations_loader`, or from
`ingestion.synthetic_data.generate_synthetic_dataset` for local
development — into a feature matrix with one row per labelled account,
ready for `detection.model_training.train_ensemble`.

The ``timestamp`` column carries the latest trade timestamp (Unix epoch)
within each wallet's feature window.  It is metadata for temporal
splitting and must **not** be used as a model feature.
"""

import logging
from typing import Generator, Tuple

import numpy as np
import pandas as pd

from detection.feature_engineering import FEATURE_NAMES, build_feature_vector
from detection.graph_engine import build_ring_membership_index, build_transaction_graph, find_wash_rings

logger = logging.getLogger("ledgerlens.dataset")


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
    """Split data chronologically with a purge gap to prevent data leakage.

    Sorts by timestamp, uses the earliest portion for training, skips a
    purge gap whose width accounts for the feature look-back window, and
    assigns the remainder to validation.
    """
    sort_idx = np.argsort(timestamps)
    X, y, timestamps = X[sort_idx], y[sort_idx], timestamps[sort_idx]
    cutoff_ts = timestamps[int(len(timestamps) * (1 - val_ratio))]
    purge_end_ts = cutoff_ts + gap_days * 86400
    purge_start_ts = cutoff_ts - max_window_days * 86400
    train_mask = timestamps < purge_start_ts
    val_mask = timestamps >= purge_end_ts
    return X[train_mask], X[val_mask], y[train_mask], y[val_mask]


def walk_forward_cv(
    X: np.ndarray,
    y: np.ndarray,
    timestamps: np.ndarray,
    n_splits: int = 5,
    gap_days: float = 7.0,
    min_train_days: float = 60.0,
) -> Generator[Tuple[np.ndarray, np.ndarray], None, None]:
    """Yield ``(train_indices, val_indices)`` for walk-forward validation."""
    sort_idx = np.argsort(timestamps)
    ts = timestamps[sort_idx]
    fold_duration = (ts[-1] - ts[0]) / (n_splits + 1)
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
    """Raise ``DataLeakageError`` if any val sample's feature window overlaps training data."""
    if len(train_timestamps) == 0 or len(val_timestamps) == 0:
        return
    val_window_start = val_timestamps.min() - max_window_seconds
    if val_window_start < train_timestamps.max():
        raise DataLeakageError(
            f"Leakage detected: earliest val feature window ({val_window_start:.0f}) "
            f"overlaps train data (latest: {train_timestamps.max():.0f})"
        )


def build_training_dataset(
    trades: pd.DataFrame,
    labels: dict[str, int],
    account_metadata: dict[str, dict] | None = None,
    order_book_events: pd.DataFrame | None = None,
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build a ``FEATURE_NAMES + ["wallet", "label", "timestamp"]`` DataFrame.

    One row per account in ``labels``.  The ``timestamp`` column is the
    latest ``ledger_close_time`` for that wallet's trades (Unix epoch
    float), used by ``temporal_train_val_split`` for chronological
    splitting.
    """
    if trades.empty:
        return pd.DataFrame(columns=[*FEATURE_NAMES, "wallet", "label", "timestamp"])

    as_of = as_of or pd.Timestamp(trades["ledger_close_time"].max())
    account_metadata = account_metadata or {}
    graph = build_transaction_graph(trades)
    rings = find_wash_rings(graph)
    ring_membership = build_ring_membership_index(rings, trades=trades)

    trades_dt = trades.copy()
    if not pd.api.types.is_datetime64_any_dtype(trades_dt["ledger_close_time"]):
        trades_dt["ledger_close_time"] = pd.to_datetime(trades_dt["ledger_close_time"])

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

        acct_trades = trades_dt[
            (trades_dt["base_account"] == account)
            | (trades_dt["counter_account"] == account)
        ]
        if not acct_trades.empty:
            features["timestamp"] = float(
                acct_trades["ledger_close_time"].max().timestamp()
            )
        else:
            features["timestamp"] = 0.0

        rows.append(features)

    return pd.DataFrame(rows, columns=[*FEATURE_NAMES, "wallet", "label", "timestamp"])
