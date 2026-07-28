import sqlite3

from detection.shap_drift_monitor import record_shap_snapshot


def test_record_shap_snapshot_does_not_create_db_when_sample_skipped(tmp_path):
    db_path = tmp_path / "shap.db"

    record_shap_snapshot(
        "GABC",
        "XLM/USDC",
        "random_forest",
        "v1",
        {"feature_a": 0.5},
        db_path=str(db_path),
        sample_random=lambda: 1.0,
    )

    assert not db_path.exists()


def test_record_shap_snapshot_persists_sampled_values(tmp_path):
    db_path = tmp_path / "shap.db"

    record_shap_snapshot(
        "GABC",
        "XLM/USDC",
        "random_forest",
        "v1",
        {"feature_a": 0.5, "feature_b": -0.25},
        db_path=str(db_path),
        sample_random=lambda: 0.0,
    )

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT feature_name, shap_value FROM shap_value_history ORDER BY feature_name"
        ).fetchall()

    assert rows == [("feature_a", 0.5), ("feature_b", -0.25)]
