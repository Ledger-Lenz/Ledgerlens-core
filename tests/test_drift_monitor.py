"""Tests for drift detection and feature distribution monitoring."""

import sqlite3

import numpy as np
import pandas as pd
import pytest

from detection.drift_monitor import (
    MAX_SNAPSHOT_ROWS,
    MIN_SNAPSHOT_ROWS_AFTER_PRUNE,
    compute_psi,
    is_drift_detected,
    record_scored_features,
    run_drift_report,
)
from detection.feature_engineering import FEATURE_NAMES


class TestComputePSI:
    """Tests for Population Stability Index computation."""

    def test_psi_zero_for_identical_distributions(self):
        """PSI should be 0 when distributions are identical."""
        dist = np.array([1.0, 2.0, 3.0, 4.0, 5.0] * 100)
        psi = compute_psi(dist, dist)
        assert psi == pytest.approx(0.0, abs=1e-5)

    def test_psi_positive_for_shifted_distribution(self):
        """PSI should be > 0 for shifted distributions."""
        training = np.array([1.0, 2.0, 3.0, 4.0, 5.0] * 100)
        shifted = np.array([2.0, 3.0, 4.0, 5.0, 6.0] * 100)
        psi = compute_psi(training, shifted)
        assert psi > 0.0

    def test_psi_exceeds_threshold_for_significant_shift(self):
        """PSI should exceed 0.20 for significantly shifted distributions."""
        training = np.random.normal(0, 1, 10000)
        shifted = np.random.normal(2, 1, 10000)
        psi = compute_psi(training, shifted)
        assert psi > 0.20

    def test_psi_handles_nan_values(self):
        """PSI should handle NaN values gracefully."""
        training = np.array([1.0, 2.0, 3.0, np.nan, 5.0])
        current = np.array([1.0, 2.0, 3.0, np.nan, 5.0])
        psi = compute_psi(training, current)
        assert psi == pytest.approx(0.0, abs=1e-5)

    def test_psi_zero_for_empty_arrays(self):
        """PSI should be 0 for empty or all-nan arrays."""
        empty = np.array([])
        valid = np.array([1.0, 2.0, 3.0])
        assert compute_psi(empty, valid) == 0.0
        assert compute_psi(valid, empty) == 0.0

    def test_psi_zero_for_constant_array(self):
        """PSI should be 0 when all values are identical."""
        const = np.array([5.0] * 100)
        psi = compute_psi(const, const)
        assert psi == pytest.approx(0.0, abs=1e-5)


class TestRecordScoredFeatures:
    """Tests for recording scored features to SQLite."""

    def test_record_scored_features_persists_to_db(self, tmp_path):
        """record_scored_features should persist feature vectors to SQLite."""
        db_path = str(tmp_path / "test.db")
        features = [{"feature_a": 1.0, "feature_b": 2.0}]

        record_scored_features(features, db_path=db_path)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM feature_distribution_snapshots")
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 2  # Two features recorded

    def test_record_scored_features_with_wallet_and_pair(self, tmp_path):
        """record_scored_features should store wallet and asset pair information."""
        db_path = str(tmp_path / "test.db")
        features = [{"feature_a": 1.0}]
        wallets = ["wallet123"]
        pairs = ["XLM/USDC"]

        record_scored_features(features, wallets, pairs, db_path=db_path)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT wallet, asset_pair FROM feature_distribution_snapshots LIMIT 1")
        wallet, pair = cursor.fetchone()
        conn.close()

        assert wallet == "wallet123"
        assert pair == "XLM/USDC"

    def test_record_scored_features_skips_nan_values(self, tmp_path):
        """record_scored_features should skip NaN values."""
        db_path = str(tmp_path / "test.db")
        features = [{"feature_a": 1.0, "feature_b": np.nan, "feature_c": 3.0}]

        record_scored_features(features, db_path=db_path)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM feature_distribution_snapshots")
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 2  # Only feature_a and feature_c

    def test_record_scored_features_handles_empty_input(self, tmp_path):
        """record_scored_features should handle empty feature list gracefully."""
        db_path = str(tmp_path / "test.db")
        record_scored_features([], db_path=db_path)
        # Should not raise

    def test_hard_row_cap_enforcement(self, tmp_path):
        """Rows beyond MAX_SNAPSHOT_ROWS should be pruned to MIN_SNAPSHOT_ROWS_AFTER_PRUNE."""
        db_path = str(tmp_path / "test.db")

        # Insert rows exceeding the cap
        num_rows_to_insert = MAX_SNAPSHOT_ROWS + 100
        features_list = []
        for _ in range(num_rows_to_insert // 2):
            features_list.append({"f": 1.0})

        record_scored_features(features_list, db_path=db_path)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM feature_distribution_snapshots")
        count = cursor.fetchone()[0]
        conn.close()

        assert count <= MIN_SNAPSHOT_ROWS_AFTER_PRUNE + 100  # Allow some margin


class TestIsDriftDetected:
    """Tests for drift detection logic."""

    def test_drift_detected_when_threshold_exceeded(self):
        """is_drift_detected should return True when enough features exceed PSI threshold."""
        report = {
            "feature_a": 0.25,
            "feature_b": 0.22,
            "feature_c": 0.21,
            "feature_d": 0.10,
            "feature_e": 0.05,
        }
        assert is_drift_detected(report, psi_threshold=0.20, min_drifted_features=3) is True

    def test_drift_not_detected_when_below_threshold(self):
        """is_drift_detected should return False when insufficient features exceed threshold."""
        report = {
            "feature_a": 0.25,
            "feature_b": 0.22,
            "feature_c": 0.05,
            "feature_d": 0.10,
            "feature_e": 0.05,
        }
        assert is_drift_detected(report, psi_threshold=0.20, min_drifted_features=3) is False

    def test_drift_detected_at_exact_threshold(self):
        """is_drift_detected should use > not >= for PSI threshold."""
        report = {
            "feature_a": 0.20,  # Exactly at threshold
            "feature_b": 0.21,
            "feature_c": 0.22,
            "feature_d": 0.10,
        }
        assert is_drift_detected(report, psi_threshold=0.20, min_drifted_features=3) is False

    def test_drift_detected_above_threshold(self):
        """is_drift_detected should return True when PSI > threshold."""
        report = {
            "feature_a": 0.201,  # Slightly above threshold
            "feature_b": 0.21,
            "feature_c": 0.22,
            "feature_d": 0.10,
        }
        assert is_drift_detected(report, psi_threshold=0.20, min_drifted_features=3) is True

    def test_drift_detected_with_empty_report(self):
        """is_drift_detected should return False for empty report."""
        report = {}
        assert is_drift_detected(report) is False


class TestRunDriftReport:
    """Tests for drift report generation."""

    def test_run_drift_report_returns_dict(self, tmp_path):
        """run_drift_report should return a dict of PSI values."""
        feature_a, feature_b = FEATURE_NAMES[0], FEATURE_NAMES[1]
        # Create a minimal training dataset
        training_csv = tmp_path / "training.csv"
        df = pd.DataFrame({
            feature_a: [1.0, 2.0, 3.0, 4.0, 5.0],
            feature_b: [10.0, 20.0, 30.0, 40.0, 50.0],
        })
        df.to_csv(training_csv, index=False)

        # Create mock database with recent scored features
        db_path = str(tmp_path / "test.db")
        features = [
            {feature_a: 1.5, feature_b: 15.0},
            {feature_a: 2.5, feature_b: 25.0},
        ]
        record_scored_features(features, db_path=db_path)

        # Run drift report (will load only the two features we added)
        report = run_drift_report(str(training_csv), db_path=db_path)

        assert isinstance(report, dict)
        assert feature_a in report
        assert feature_b in report

    def test_run_drift_report_handles_missing_training_file(self, tmp_path):
        """run_drift_report should handle missing training file gracefully."""
        db_path = str(tmp_path / "test.db")
        report = run_drift_report("/nonexistent/path.csv", db_path=db_path)
        assert report == {}

    def test_run_drift_report_empty_database(self, tmp_path):
        """run_drift_report should handle empty database gracefully."""
        training_csv = tmp_path / "training.csv"
        df = pd.DataFrame({"feature_a": [1.0, 2.0, 3.0]})
        df.to_csv(training_csv, index=False)

        db_path = str(tmp_path / "test.db")
        report = run_drift_report(str(training_csv), db_path=db_path)
        assert report == {}


class TestIntegrationRecordAndDetect:
    """Integration tests for recording features and detecting drift."""

    def test_record_then_detect_drift(self, tmp_path):
        """Should correctly detect drift in recorded features."""
        feature_name = FEATURE_NAMES[0]
        db_path = str(tmp_path / "test.db")
        training_csv = tmp_path / "training.csv"

        # Create training data with normal distribution
        training_df = pd.DataFrame({
            feature_name: np.random.normal(0, 1, 1000),
        })
        training_df.to_csv(training_csv, index=False)

        # Record shifted features (simulating drift)
        shifted_features = [
            {feature_name: v}
            for v in np.random.normal(2, 1, 100)  # Mean shifted from 0 to 2
        ]
        record_scored_features(shifted_features, db_path=db_path)

        # Run drift report
        report = run_drift_report(str(training_csv), db_path=db_path)

        # Should detect drift
        assert report.get(feature_name, 0) > 0.20
        assert is_drift_detected(report, min_drifted_features=1) is True
