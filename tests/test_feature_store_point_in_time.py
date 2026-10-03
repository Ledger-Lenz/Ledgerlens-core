"""Point-in-time correctness of feature retrieval (#981)."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from detection.dataset import build_training_dataset
from detection.feature_store import FeatureStore
from ingestion.synthetic_data import generate_synthetic_dataset
from tests.test_feature_store_archival import _init_snapshot_table, _insert_row

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_as_of_query_excludes_future_feature_updates(tmp_path):
    db = str(tmp_path / "fs.db")
    _init_snapshot_table(db)
    _insert_row(db, "GA", "benford_mad", 0.1, _T0)
    _insert_row(db, "GA", "trade_count", 5.0, _T0 + timedelta(hours=1))
    # Future updates recorded after the label time.
    _insert_row(db, "GA", "benford_mad", 0.9, _T0 + timedelta(days=2))
    _insert_row(db, "GA", "new_feature", 1.0, _T0 + timedelta(days=2))

    store = FeatureStore()
    as_of = _T0 + timedelta(days=1)
    assert store.get_features_as_of("GA", as_of, db_path=db) == {
        "benford_mad": 0.1,
        "trade_count": 5.0,
    }
    assert store.get_features_as_of("GA", _T0 + timedelta(days=3), db_path=db)["benford_mad"] == 0.9
    assert store.get_features_as_of("GA", _T0 - timedelta(seconds=1), db_path=db) == {}


def test_training_dataset_ignores_trades_after_as_of():
    trades, metadata, _events, labels = generate_synthetic_dataset()
    as_of = pd.Timestamp(trades["ledger_close_time"].quantile(0.5))
    subset = dict(list(labels.items())[:5])

    past_only = build_training_dataset(
        trades[trades["ledger_close_time"] <= as_of], subset, metadata, as_of=as_of
    )
    with_future = build_training_dataset(trades, subset, metadata, as_of=as_of)

    pd.testing.assert_frame_equal(past_only, with_future)
