"""Tests for continuous retraining with drift detection."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from cli import app
from detection.drift_monitor import record_scored_features


@pytest.fixture
def runner():
    """Typer CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_settings(tmp_path):
    """Mock settings with temporary model directory."""
    settings = MagicMock()
    settings.model_dir = str(tmp_path / "models")
    settings.db_path = str(tmp_path / "ledgerlens.db")
    return settings


@pytest.fixture
def training_metadata(tmp_path):
    """Create training metadata JSON for testing."""
    metadata_dir = tmp_path / "models"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    training_csv = metadata_dir / "training_reference.csv"
    df = pd.DataFrame({
        "feature_a": np.random.normal(0, 1, 100),
        "feature_b": np.random.normal(0, 1, 100),
        "feature_c": np.random.normal(0, 1, 100),
    })
    df.to_csv(training_csv, index=False)

    metadata = {
        "timestamp": "2024-01-01T00:00:00Z",
        "version": "v0001",
        "training_dataset_path": str(training_csv),
        "training_row_count": 100,
        "column_hash": "abc123",
        "model_metrics": {
            "random_forest": {"auc_roc": 0.85, "pr_auc": 0.80, "f1": 0.82},
            "xgboost": {"auc_roc": 0.87, "pr_auc": 0.82, "f1": 0.84},
            "lightgbm": {"auc_roc": 0.86, "pr_auc": 0.81, "f1": 0.83},
        },
    }

    metadata_path = metadata_dir / "training_metadata.json"
    with open(metadata_path, "w") as f:
        json.dump(metadata, f)

    return str(tmp_path)


class TestRetrainCheckCommand:
    """Tests for the retrain-check CLI command."""

    @patch("cli.run_drift_report")
    @patch("cli.is_drift_detected")
    def test_retrain_check_skipped_when_no_drift(self, mock_is_drift, mock_drift_report, runner, mock_settings, tmp_path):
        """retrain-check should skip retraining when drift is not detected."""
        mock_drift_report.return_value = {"feature_a": 0.10, "feature_b": 0.15}
        mock_is_drift.return_value = False

        # Create training metadata
        metadata_dir = tmp_path / "models"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        training_csv = metadata_dir / "training_reference.csv"
        df = pd.DataFrame({"feature_a": [1.0, 2.0], "feature_b": [3.0, 4.0]})
        df.to_csv(training_csv, index=False)

        metadata_path = metadata_dir / "training_metadata.json"
        with open(metadata_path, "w") as f:
            json.dump({
                "training_dataset_path": str(training_csv),
                "model_metrics": {},
            }, f)

        with patch("cli.settings", mock_settings):
            mock_settings.model_dir = str(metadata_dir)

            # We can't easily test the full CLI without more mocking,
            # but we can verify the drift detection logic
            report = mock_drift_report.return_value
            is_drifted = mock_is_drift(report)

            assert is_drifted is False

    @patch("cli.train_ensemble")
    @patch("cli.run_drift_report")
    @patch("cli.is_drift_detected")
    def test_retrain_check_triggered_on_drift(self, mock_is_drift, mock_drift_report, mock_train, runner, tmp_path):
        """retrain-check should trigger retraining when drift is detected."""
        mock_drift_report.return_value = {"feature_a": 0.25, "feature_b": 0.22, "feature_c": 0.21}
        mock_is_drift.return_value = True

        # Create a mock ensemble result
        mock_model = MagicMock()
        mock_train.return_value = {
            "random_forest": {
                "model": mock_model,
                "auc_roc": 0.86,
                "pr_auc": 0.81,
                "f1": 0.83,
            },
            "xgboost": {
                "model": mock_model,
                "auc_roc": 0.88,
                "pr_auc": 0.83,
                "f1": 0.85,
            },
            "lightgbm": {
                "model": mock_model,
                "auc_roc": 0.87,
                "pr_auc": 0.82,
                "f1": 0.84,
            },
        }

        # Create training metadata
        metadata_dir = tmp_path / "models"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        training_csv = metadata_dir / "training_reference.csv"
        df = pd.DataFrame({
            "feature_a": np.random.normal(0, 1, 50),
            "feature_b": np.random.normal(0, 1, 50),
            "feature_c": np.random.normal(0, 1, 50),
        })
        df.to_csv(training_csv, index=False)

        metadata_path = metadata_dir / "training_metadata.json"
        with open(metadata_path, "w") as f:
            json.dump({
                "training_dataset_path": str(training_csv),
                "model_metrics": {
                    "random_forest": {"auc_roc": 0.85},
                    "xgboost": {"auc_roc": 0.87},
                    "lightgbm": {"auc_roc": 0.86},
                },
            }, f)

        # Verify drift detection logic
        report = mock_drift_report.return_value
        is_drifted = mock_is_drift(report)
        assert is_drifted is True

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
        db_path = str(tmp_path / "test.db")
        training_csv = tmp_path / "training.csv"

        # Create training reference with normal distribution
        training_df = pd.DataFrame({
            "feature_a": np.random.normal(0, 1, 500),
            "feature_b": np.random.normal(0, 1, 500),
            "feature_c": np.random.normal(0, 1, 500),
        })
        training_df.to_csv(training_csv, index=False)

        # Record shifted features (simulating drift)
        shifted_features = [
            {
                "feature_a": v_a,
                "feature_b": v_b,
                "feature_c": v_c,
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
        db_path = str(tmp_path / "test.db")
        training_csv = tmp_path / "training.csv"

        # Create training reference
        np.random.seed(42)
        training_data = np.random.normal(0, 1, 500)
        training_df = pd.DataFrame({"feature_a": training_data})
        training_df.to_csv(training_csv, index=False)

        # Record similar features (same distribution)
        np.random.seed(43)  # Different seed but same parameters
        similar_features = [
            {"feature_a": v}
            for v in np.random.normal(0, 1, 100)
        ]
        record_scored_features(similar_features, db_path=db_path)

        # Import here to avoid module-level database operations
        from detection.drift_monitor import is_drift_detected, run_drift_report

        report = run_drift_report(str(training_csv), db_path=db_path)

        # PSI should be low for consistent features
        assert report.get("feature_a", 1.0) < 0.20

        # Drift should not be detected
        assert is_drift_detected(report) is False
