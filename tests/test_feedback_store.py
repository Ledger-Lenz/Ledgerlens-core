"""Tests for detection/feedback_store.py."""

import threading
from datetime import datetime, timezone


from detection.feedback_store import ScoringFeedback, get_recent_feedback, record_feedback


def _fb(model_name="random_forest", ground_truth=1, days_ago=0, db_path=None):
    from datetime import timedelta
    scored_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    fb = ScoringFeedback(
        wallet="GABC",
        asset_pair="XLM/USDC",
        model_name=model_name,
        predicted_probability=0.8,
        ground_truth=ground_truth,
        scored_at=scored_at,
        confirmed_at=datetime.now(timezone.utc),
    )
    record_feedback(fb, db_path=db_path)
    return fb


def test_record_and_retrieve(tmp_path):
    db = str(tmp_path / "test.db")
    _fb(db_path=db)
    rows = get_recent_feedback(days_back=7, db_path=db)
    assert len(rows) == 1
    assert rows[0].wallet == "GABC"
    assert rows[0].ground_truth == 1


def test_get_recent_feedback_filters_by_days_back(tmp_path):
    db = str(tmp_path / "test.db")
    _fb(days_ago=0, db_path=db)   # recent
    _fb(days_ago=10, db_path=db)  # old — outside window

    recent = get_recent_feedback(days_back=7, db_path=db)
    assert len(recent) == 1


def test_get_recent_feedback_filters_by_model(tmp_path):
    db = str(tmp_path / "test.db")
    _fb(model_name="random_forest", db_path=db)
    _fb(model_name="xgboost", db_path=db)

    rf_only = get_recent_feedback(days_back=7, model_name="random_forest", db_path=db)
    assert all(r.model_name == "random_forest" for r in rf_only)
    assert len(rf_only) == 1


def test_concurrent_writes_no_corruption(tmp_path):
    db = str(tmp_path / "test.db")
    errors = []

    def write_many():
        for _ in range(20):
            try:
                _fb(db_path=db)
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=write_many) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent write errors: {errors}"
    rows = get_recent_feedback(days_back=7, db_path=db)
    assert len(rows) == 40  # 2 threads × 20 writes
