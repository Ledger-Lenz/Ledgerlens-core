"""Tests for AnalystFeedbackStore in detection/feedback_store.py."""

import math
import time
from datetime import datetime, timedelta, timezone

import pytest

from detection.feedback_store import (
    FEEDBACK_DECAY_LAMBDA,
    AnalystFeedbackStore,
    FeedbackRecord,
)


STELLAR_WALLET = "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF"


class TestAddCorrection:
    def test_persists_to_sqlite(self, tmp_path):
        db = str(tmp_path / "test.db")
        store = AnalystFeedbackStore(db_path=db)
        record = store.add_correction(
            wallet=STELLAR_WALLET,
            asset_pair="XLM/USDC",
            analyst_label=1,
            original_score=85,
            confidence=0.9,
        )
        assert record.id is not None
        assert record.wallet == STELLAR_WALLET
        assert record.analyst_label == 1
        assert record.confidence == 0.9
        assert record.importance_weight == pytest.approx(0.9)

    def test_invalid_label_raises(self, tmp_path):
        db = str(tmp_path / "test.db")
        store = AnalystFeedbackStore(db_path=db)
        with pytest.raises(ValueError, match="analyst_label"):
            store.add_correction(STELLAR_WALLET, "XLM/USDC", 2, 50)

    def test_invalid_confidence_raises(self, tmp_path):
        db = str(tmp_path / "test.db")
        store = AnalystFeedbackStore(db_path=db)
        with pytest.raises(ValueError, match="confidence"):
            store.add_correction(STELLAR_WALLET, "XLM/USDC", 1, 50, confidence=1.5)


class TestGetWeightedCorrections:
    def test_weights_decrease_with_age(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "test.db")
        store = AnalystFeedbackStore(db_path=db)
        conn = store._connect_and_init()

        now = datetime.now(timezone.utc)
        for days_ago in [0, 30, 60]:
            ts = (now - timedelta(days=days_ago)).isoformat()
            conn.execute(
                """INSERT INTO analyst_feedback
                   (wallet, asset_pair, analyst_label, original_score,
                    confidence, has_feature_vector, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (STELLAR_WALLET, "XLM/USDC", 1, 80, 1.0, 1, ts),
            )
        conn.commit()
        conn.close()

        corrections = store.get_weighted_corrections(since_days=90)
        weights = [w for _, _, w in corrections]

        assert len(weights) == 3
        assert weights[0] > weights[1] > weights[2]
        assert all(w > 0 for w in weights)

    def test_zero_decay_lambda_all_equal(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "test.db")
        store = AnalystFeedbackStore(db_path=db)
        conn = store._connect_and_init()

        now = datetime.now(timezone.utc)
        for days_ago in [0, 30, 60]:
            ts = (now - timedelta(days=days_ago)).isoformat()
            conn.execute(
                """INSERT INTO analyst_feedback
                   (wallet, asset_pair, analyst_label, original_score,
                    confidence, has_feature_vector, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (STELLAR_WALLET, "XLM/USDC", 1, 80, 1.0, 1, ts),
            )
        conn.commit()
        conn.close()

        corrections = store.get_weighted_corrections(since_days=90, decay_lambda=0.0)
        weights = [w for _, _, w in corrections]

        assert all(w == pytest.approx(1.0, abs=0.01) for w in weights)

    def test_excludes_no_feature_vector(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "test.db")
        store = AnalystFeedbackStore(db_path=db)
        conn = store._connect_and_init()

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO analyst_feedback
               (wallet, asset_pair, analyst_label, original_score,
                confidence, has_feature_vector, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (STELLAR_WALLET, "XLM/USDC", 1, 80, 1.0, 0, now),
        )
        conn.commit()
        conn.close()

        corrections = store.get_weighted_corrections()
        assert len(corrections) == 0


class TestCorrectionCount:
    def test_counts_all_records(self, tmp_path):
        db = str(tmp_path / "test.db")
        store = AnalystFeedbackStore(db_path=db)
        assert store.correction_count() == 0

        store.add_correction(STELLAR_WALLET, "XLM/USDC", 1, 80)
        store.add_correction(STELLAR_WALLET, "XLM/USDC", 0, 30)
        assert store.correction_count() == 2


class TestPagination:
    def test_paginated_results(self, tmp_path):
        db = str(tmp_path / "test.db")
        store = AnalystFeedbackStore(db_path=db)

        for i in range(15):
            store.add_correction(STELLAR_WALLET, f"pair_{i}", 1, 80)

        records, total = store.get_corrections_paginated(page=1, page_size=10)
        assert len(records) == 10
        assert total == 15

        records2, total2 = store.get_corrections_paginated(page=2, page_size=10)
        assert len(records2) == 5
        assert total2 == 15
