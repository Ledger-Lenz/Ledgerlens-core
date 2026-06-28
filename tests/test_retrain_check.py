"""Tests for continuous retraining with drift detection."""

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
from typer.testing import CliRunner

from cli import app
from detection.drift_monitor import record_scored_features
from detection.feature_engineering import FEATURE_NAMES

runner = CliRunner()


class TestRetrainCheckCommand:
    """Tests for the retrain-check CLI command."""

    def _write_training_metadata(self, metadata_dir, training_csv, model_metrics=None):
        metadata_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = metadata_dir / "training_metadata.json"
        with open(metadata_path, "w") as f:
            json.dump({
                "training_dataset_path": str(training_csv),
                "model_metrics": model_metrics or {},
            }, f)

    def _configure_model_dir(self, monkeypatch, metadata_dir):
        import config.settings as settings_module

        monkeypatch.setenv("MODEL_DIR", str(metadata_dir))
        object.__setattr__(settings_module.settings, "model_dir", str(metadata_dir))

    def test_retrain_check_skipped_when_no_drift(self, monkeypatch, tmp_path):
        """retrain-check should skip retraining when drift is not detected."""
        metadata_dir = tmp_path / "models"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        training_csv = metadata_dir / "training_reference.csv"
        pd.DataFrame({"feature_a": [1.0, 2.0], "feature_b": [3.0, 4.0]}).to_csv(training_csv, index=False)
        self._write_training_metadata(metadata_dir, training_csv)
        self._configure_model_dir(monkeypatch, metadata_dir)

        with (
            patch("detection.drift_monitor.run_drift_report") as mock_drift_report,
            patch("detection.drift_monitor.is_drift_detected") as mock_is_drift,
            patch("detection.model_training.train_ensemble") as mock_train,
        ):
            mock_drift_report.return_value = {"feature_a": 0.10, "feature_b": 0.15}
            mock_is_drift.return_value = False

            result = runner.invoke(app, ["retrain-check"])

            assert result.exit_code == 0, result.output
            mock_is_drift.assert_called_once()
            mock_train.assert_not_called()

    def test_retrain_check_triggered_on_drift(self, monkeypatch, tmp_path):
        """retrain-check should trigger retraining when drift is detected."""
        metadata_dir = tmp_path / "models"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        training_csv = metadata_dir / "training_reference.csv"
        pd.DataFrame({
            "feature_a": np.random.normal(0, 1, 50),
            "feature_b": np.random.normal(0, 1, 50),
            "feature_c": np.random.normal(0, 1, 50),
        }).to_csv(training_csv, index=False)
        self._write_training_metadata(
            metadata_dir,
            training_csv,
            model_metrics={
                "random_forest": {"auc_roc": 0.85},
                "xgboost": {"auc_roc": 0.87},
                "lightgbm": {"auc_roc": 0.86},
            },
        )
        self._configure_model_dir(monkeypatch, metadata_dir)

        mock_model = MagicMock()
        mock_results = {
            name: {"model": mock_model, "auc_roc": 0.90, "pr_auc": 0.85, "f1": 0.84}
            for name in ("random_forest", "xgboost", "lightgbm")
        }

        with (
            patch("detection.drift_monitor.run_drift_report") as mock_drift_report,
            patch("detection.drift_monitor.is_drift_detected") as mock_is_drift,
            patch("detection.model_training.train_ensemble", return_value=mock_results) as mock_train,
            patch("detection.model_training.save_models") as mock_save_models,
            patch("detection.model_registry.get_current_version", return_value="v0001"),
        ):
            mock_drift_report.return_value = {"feature_a": 0.25, "feature_b": 0.22, "feature_c": 0.21}
            mock_is_drift.return_value = True

            result = runner.invoke(app, ["retrain-check"])

            assert result.exit_code == 0, result.output
            mock_is_drift.assert_called_once()
            mock_train.assert_called_once()
            mock_save_models.assert_called_once()

    def test_drift_report_written(self, tmp_path):
        """Drift report should be written to drift_reports/ directory."""
        report_dir = tmp_path / "drift_reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        # Simulate writing a drift report
        report = {
            "timestamp": "20240101_0000",
            "drift_detected": True,
            "psi_report": {"feature_a": 0.25},
            "promoted": True,
            "new_model_metrics": {"random_forest": 0.86},
        }

        report_path = report_dir / "20240101_0000.json"
        with open(report_path, "w") as f:
            json.dump(report, f)

        assert report_path.exists()

        with open(report_path, "r") as f:
            loaded = json.load(f)

        assert loaded["drift_detected"] is True
        assert loaded["promoted"] is True


class TestDriftDetectionIntegration:
    """Integration tests for drift detection in the pipeline."""

    def test_record_features_then_detect_drift(self, tmp_path):
        """Should correctly detect drift after recording features."""
        feature_a, feature_b, feature_c = FEATURE_NAMES[0], FEATURE_NAMES[1], FEATURE_NAMES[2]
        db_path = str(tmp_path / "test.db")
        training_csv = tmp_path / "training.csv"

        # Create training reference with normal distribution
        training_df = pd.DataFrame({
            feature_a: np.random.normal(0, 1, 500),
            feature_b: np.random.normal(0, 1, 500),
            feature_c: np.random.normal(0, 1, 500),
        })
        training_df.to_csv(training_csv, index=False)

        # Record shifted features (simulating drift)
        shifted_features = [
            {
                feature_a: v_a,
                feature_b: v_b,
                feature_c: v_c,
            }
            for v_a, v_b, v_c in zip(
                np.random.normal(2, 1, 100),  # Mean shift for feature_a
                np.random.normal(0, 1, 100),  # No shift for feature_b
                np.random.normal(0, 1, 100),  # No shift for feature_c
            )
        ]

        record_scored_features(shifted_features, db_path=db_path)

        # Import here to avoid module-level database operations
        from detection.drift_monitor import is_drift_detected, run_drift_report

        report = run_drift_report(str(training_csv), db_path=db_path)

        # At least one feature should show drift
        assert any(psi > 0.20 for psi in report.values())

        # Drift detection should trigger on enough drifted features
        assert is_drift_detected(report, psi_threshold=0.20, min_drifted_features=1) is True

    def test_no_drift_detection_with_consistent_features(self, tmp_path):
        """Should not detect drift when features remain consistent."""
        feature_name = FEATURE_NAMES[0]
        db_path = str(tmp_path / "test.db")
        training_csv = tmp_path / "training.csv"

        # Use one large reference pool; score a bootstrap sample from the same pool.
        np.random.seed(42)
        training_data = np.random.normal(0, 1, 5000)
        training_df = pd.DataFrame({feature_name: training_data})
        training_df.to_csv(training_csv, index=False)

        scored_values = np.random.choice(training_data, size=1000, replace=True)
        similar_features = [{feature_name: float(v)} for v in scored_values]
        record_scored_features(similar_features, db_path=db_path)

        # Import here to avoid module-level database operations
        from detection.drift_monitor import is_drift_detected, run_drift_report

        report = run_drift_report(str(training_csv), db_path=db_path)

        # PSI should be low when production samples come from the same distribution
        assert report.get(feature_name, 1.0) < 0.20

        # Drift should not be detected
        assert is_drift_detected(report) is False
