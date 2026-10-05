import sqlite3

from detection.shap_drift_monitor import compute_shap_psi, record_shap_snapshot


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


def test_shap_drift_distributions_are_isolated_by_model_name(tmp_path):
    db_path = str(tmp_path / "shap.db")
    for model_name, versions in (
        ("random_forest", {"v1": (0.1, 0.2), "v2": (0.1, 0.2)}),
        ("xgboost", {"v1": (0.1, 0.2), "v2": (10.0, 20.0)}),
    ):
        for version, values in versions.items():
            for value in values:
                record_shap_snapshot(
                    "GABC",
                    "XLM/USDC",
                    model_name,
                    version,
                    {"feature_a": value},
                    db_path=db_path,
                    sample_random=lambda: 0.0,
                )

    rf_psi = compute_shap_psi("feature_a", "v1", "v2", db_path, model_name="random_forest")
    xgb_psi = compute_shap_psi("feature_a", "v1", "v2", db_path, model_name="xgboost")
    assert rf_psi != xgb_psi
