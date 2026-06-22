"""Build labelled feature datasets for `detection.model_training`.

Turns a `Trade`/`OrderBookEvent`/account-metadata bundle — either from
`ingestion.historical_loader` + `ingestion.account_loader` +
`ingestion.operations_loader`, or from
`ingestion.synthetic_data.generate_synthetic_dataset` for local
development — into a feature matrix with one row per labelled account,
ready for `detection.model_training.train_ensemble`.
"""

import pandas as pd

from detection.feature_engineering import FEATURE_NAMES, build_feature_vector
from detection.graph_engine import build_ring_membership_index, build_transaction_graph, find_wash_rings


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
