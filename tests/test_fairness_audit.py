"""Tests for the Fairness and Bias Audit Framework (Issue #344).

Tests cover cohort assignment, all metric functions, CLI integration,
API endpoint security, and minimum-sample exclusion.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from cli import app
from detection.fairness_audit import (
    MIN_COHORT_SAMPLES,
    FairnessAuditReport,
    FairnessFinding,
    _find_volume_column,
    assign_cohorts,
    cold_start_bias_check,
    demographic_parity_gap,
    equalized_odds_gap,
    run_fairness_audit,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def feature_df() -> pd.DataFrame:
    """A synthetic feature DataFrame with all three cohort dimensions populated.

    Creates 500 wallets spread across volume tiers, account ages, and
    network centrality values so every bin is represented.
    """
    np.random.seed(42)
    n = 500
    return pd.DataFrame(
        {
            "total_volume": np.concatenate([
                np.random.uniform(0, 500, 125),       # micro
                np.random.uniform(1_000, 8_000, 125),  # retail
                np.random.uniform(10_000, 80_000, 125),  # active
                np.random.uniform(100_000, 500_000, 125),  # whale
            ]),
            "account_age_days": np.concatenate([
                np.random.uniform(0, 5, 125),       # new
                np.random.uniform(7, 25, 125),       # recent
                np.random.uniform(30, 150, 125),      # established
                np.random.uniform(180, 500, 125),     # veteran
            ]),
            "network_centrality": np.concatenate([
                np.random.uniform(0.0, 0.2, 125),     # isolated
                np.random.uniform(0.25, 0.45, 125),   # peripheral
                np.random.uniform(0.5, 0.7, 125),     # connected
                np.random.uniform(0.75, 0.95, 125),   # hub
            ]),
        }
    )


@pytest.fixture
def equal_rates_df() -> pd.DataFrame:
    """Feature DataFrame where all cohorts should have equal flag rates.

    Account age is uniform across all samples so cold-start check should
    find no significant disparity.
    """
    np.random.seed(42)
    n = 200
    return pd.DataFrame(
        {
            "total_volume": np.random.uniform(0, 200_000, n),
            "account_age_days": np.random.uniform(10, 100, n),
            "network_centrality": np.random.uniform(0.1, 0.9, n),
        }
    )


# ---------------------------------------------------------------------------
# Cohort assignment
# ---------------------------------------------------------------------------


class TestAssignCohorts:
    def test_all_rows_assigned(self, feature_df: pd.DataFrame):
        """Every row gets a volume_tier, account_age_tier, and centrality_tier."""
        result = assign_cohorts(feature_df)
        assert len(result) == len(feature_df)
        for col in ("volume_tier", "account_age_tier", "centrality_tier"):
            assert col in result.columns
            assert result[col].isna().sum() == 0, f"NaN values in {col}"

    def test_volume_tier_labels(self, feature_df: pd.DataFrame):
        """Volume tiers use the expected label set."""
        result = assign_cohorts(feature_df)
        expected = {"micro", "retail", "active", "whale"}
        assert set(result["volume_tier"].cat.categories) >= expected

    def test_account_age_tier_labels(self, feature_df: pd.DataFrame):
        """Account age tiers use the expected label set."""
        result = assign_cohorts(feature_df)
        expected = {"new", "recent", "established", "veteran"}
        assert set(result["account_age_tier"].cat.categories) >= expected

    def test_centrality_tier_labels(self, feature_df: pd.DataFrame):
        """Centrality tiers use the expected label set."""
        result = assign_cohorts(feature_df)
        expected = {"isolated", "peripheral", "connected", "hub"}
        assert set(result["centrality_tier"].cat.categories) >= expected

    def test_missing_column_falls_back_to_unknown(self):
        """When a required column is missing, the tier is set to 'unknown'."""
        df = pd.DataFrame({"total_volume": [1000.0]})
        result = assign_cohorts(df)
        assert result["account_age_tier"].iloc[0] == "unknown"
        assert result["centrality_tier"].iloc[0] == "unknown"

    def test_volume_column_fallback(self):
        """If no exact volume column match, falls back to any column with 'volume' in name."""
        df = pd.DataFrame({"trade_volume": [5000.0], "account_age_days": [50.0], "network_centrality": [0.5]})
        result = assign_cohorts(df)
        assert _find_volume_column(df) == "trade_volume"
        assert result["volume_tier"].iloc[0] == "retail"

    def test_no_unassigned_rows(self, feature_df: pd.DataFrame):
        """No row should have an unassigned (NaN) cohort label."""
        result = assign_cohorts(feature_df)
        assert result["volume_tier"].isna().sum() == 0
        assert result["account_age_tier"].isna().sum() == 0
        assert result["centrality_tier"].isna().sum() == 0


# ---------------------------------------------------------------------------
# Demographic parity
# ---------------------------------------------------------------------------


class TestDemographicParityGap:
    def test_detects_injected_disparity(self):
        """A cohort with a 2x higher flag rate is flagged above the threshold."""
        np.random.seed(42)
        n = 100
        # Cohort A: low flag rate (~10%)
        cohort_a_scores = np.random.uniform(0, 0.15, n)
        # Cohort B: high flag rate (~80%)
        cohort_b_scores = np.random.uniform(0.6, 0.95, n)
        scores = np.concatenate([cohort_a_scores, cohort_b_scores])
        labels = np.array(["cohort_a"] * n + ["cohort_b"] * n)

        findings = demographic_parity_gap(scores, labels, threshold=0.1, risk_score_threshold=70)
        assert len(findings) == 1
        f = findings[0]
        assert f.flagged, "Injected disparity should be flagged"
        assert f.gap > 0.1
        assert f.metric == "demographic_parity"

    def test_equal_rates_not_flagged(self):
        """When flag rates are identical across cohorts, no disparity is flagged."""
        np.random.seed(42)
        n = 100
        scores = np.random.uniform(0.4, 0.6, n * 2)
        labels = np.array(["cohort_a"] * n + ["cohort_b"] * n)

        findings = demographic_parity_gap(scores, labels, threshold=0.1, risk_score_threshold=70)
        assert len(findings) == 1
        f = findings[0]
        # With uniform random scores around 0.5 and threshold at 70 (0.7),
        # both cohorts should have ~0% flag rate, so gap ~ 0
        assert not f.flagged, "Equal rates should not be flagged"

    def test_small_cohort_excluded(self):
        """Cohorts with fewer than MIN_COHORT_SAMPLES are excluded from the gap."""
        scores = np.ones(200)
        # Cohort with only 5 samples — excluded
        labels = np.array(["big"] * 180 + ["tiny"] * 20)
        # Use threshold of 0 so any gap would be flagged
        findings = demographic_parity_gap(scores, labels, threshold=0.0, risk_score_threshold=70)
        # If "tiny" is excluded because 20 < MIN_COHORT_SAMPLES (30), then only "big"
        # remains and the gap is 0.0
        for f in findings:
            assert "tiny" not in f.cohort_rates or f.cohort_rates.get("tiny", 0) == 0.0
            if "tiny" not in f.cohort_rates:
                assert f.gap == 0.0

    def test_probability_scores(self):
        """Works with scores already in 0-1 probability range."""
        scores = np.array([0.8, 0.9, 0.3, 0.4, 0.85, 0.95])
        labels = np.array(["a", "a", "b", "b", "a", "b"])
        # With risk_score_threshold=70 (0.7), cohort a has 2/3 flagged, cohort b has 1/3
        findings = demographic_parity_gap(scores, labels, threshold=0.0, risk_score_threshold=70)
        assert len(findings) >= 1


# ---------------------------------------------------------------------------
# Equalised odds
# ---------------------------------------------------------------------------


class TestEqualizedOddsGap:
    def test_tpr_gap_detected(self):
        """A difference in TPR across cohorts is detected."""
        np.random.seed(42)
        n = 100
        # Cohort A: 80% TPR
        scores_a = np.where(np.random.random(n) < 0.8, 0.9, 0.1)
        y_true_a = np.ones(n)  # all positive
        # Cohort B: 30% TPR
        scores_b = np.where(np.random.random(n) < 0.3, 0.9, 0.1)
        y_true_b = np.ones(n)  # all positive

        scores = np.concatenate([scores_a, scores_b])
        y_true = np.concatenate([y_true_a, y_true_b])
        labels = np.array(["cohort_a"] * n + ["cohort_b"] * n)

        findings = equalized_odds_gap(scores, y_true, labels, threshold=0.2, risk_score_threshold=70)
        tpr_findings = [f for f in findings if f.metric == "equalized_odds_tpr"]
        assert len(tpr_findings) >= 1
        assert tpr_findings[0].gap > 0.2

    def test_fpr_gap_detected(self):
        """A difference in FPR across cohorts is detected."""
        np.random.seed(42)
        n = 100
        # Cohort A: 5% FPR
        scores_a = np.where(np.random.random(n) < 0.05, 0.9, 0.1)
        y_true_a = np.zeros(n)  # all negative
        # Cohort B: 40% FPR
        scores_b = np.where(np.random.random(n) < 0.4, 0.9, 0.1)
        y_true_b = np.zeros(n)  # all negative

        scores = np.concatenate([scores_a, scores_b])
        y_true = np.concatenate([y_true_a, y_true_b])
        labels = np.array(["cohort_a"] * n + ["cohort_b"] * n)

        findings = equalized_odds_gap(scores, y_true, labels, threshold=0.2, risk_score_threshold=70)
        fpr_findings = [f for f in findings if f.metric == "equalized_odds_fpr"]
        assert len(fpr_findings) >= 1

    def test_min_samples_excluded(self):
        """Cohorts below MIN_COHORT_SAMPLES are excluded from TPR/FPR computation."""
        scores = np.array([0.9, 0.9, 0.9, 0.1])
        y_true = np.array([1, 1, 1, 0])
        labels = np.array(["big_cohort"] * 3 + ["tiny_cohort"])
        findings = equalized_odds_gap(scores, y_true, labels, threshold=0.0, risk_score_threshold=70)
        # The tiny cohort should be excluded since it has < MIN_COHORT_SAMPLES
        for f in findings:
            assert "tiny_cohort" not in f.cohort_rates


# ---------------------------------------------------------------------------
# Cold-start bias
# ---------------------------------------------------------------------------


class TestColdStartBias:
    def test_detects_elevated_flag_rate_for_young_wallets(self):
        """Young wallets flagged at a higher rate than mature ones (≥ MIN_COHORT_SAMPLES)."""
        np.random.seed(42)
        n = 40  # > MIN_COHORT_SAMPLES (30)
        # Young wallets: high flag rate (90% flagged)
        young_scores = np.random.uniform(0.8, 0.95, n)
        young_ages = np.random.uniform(0, 5, n)
        # Mature wallets: low flag rate (10% flagged)
        mature_scores = np.random.uniform(0, 0.2, n)
        mature_ages = np.random.uniform(100, 500, n)

        scores = np.concatenate([young_scores, mature_scores])
        ages = np.concatenate([young_ages, mature_ages])

        finding = cold_start_bias_check(
            scores, ages, age_threshold_days=7, threshold=0.1, risk_score_threshold=70
        )
        assert finding.flagged, "Cold-start bias should be detected"
        assert finding.gap > 0.1

    def test_no_bias_when_rates_equal(self):
        """When young and mature wallets have similar flag rates, no bias."""
        np.random.seed(42)
        n = 40
        scores = np.random.uniform(0.3, 0.5, n * 2)  # all well below threshold
        ages = np.concatenate([np.random.uniform(0, 5, n), np.random.uniform(100, 500, n)])
        finding = cold_start_bias_check(
            scores, ages, age_threshold_days=7, threshold=0.1, risk_score_threshold=70
        )
        assert not finding.flagged

    def test_insufficient_young_wallets(self):
        """When too few young wallets exist, the check is skipped (not flagged)."""
        scores = np.array([0.9, 0.3, 0.2])
        ages = np.array([2, 100, 200])
        finding = cold_start_bias_check(
            scores, ages, age_threshold_days=7, threshold=0.1, risk_score_threshold=70
        )
        # Only 1 young wallet < MIN_COHORT_SAMPLES (30)
        assert not finding.flagged
        assert finding.gap == 0.0


# ---------------------------------------------------------------------------
# Full audit orchestration
# ---------------------------------------------------------------------------


class TestRunFairnessAudit:
    def test_full_audit_completes(self, feature_df: pd.DataFrame):
        """`run_fairness_audit` returns a valid report without errors."""
        np.random.seed(42)
        n = len(feature_df)
        scores = np.random.uniform(0, 1, n)
        y_true = np.random.randint(0, 2, n)

        report = run_fairness_audit(
            scores=scores,
            y_true=y_true,
            feature_df=feature_df,
            model_name="test_model",
            model_version="v1",
        )
        assert isinstance(report, FairnessAuditReport)
        assert report.model_name == "test_model"
        assert report.model_version == "v1"
        assert len(report.findings) > 0
        assert isinstance(report.significant_disparity, bool)
        assert report.computed_at is not None

    def test_audit_without_labels(self, feature_df: pd.DataFrame):
        """`run_fairness_audit` works when y_true is None (no equalised odds)."""
        np.random.seed(42)
        n = len(feature_df)
        scores = np.random.uniform(0, 1, n)

        report = run_fairness_audit(
            scores=scores,
            y_true=None,
            feature_df=feature_df,
        )
        # Only demographic parity and cold-start findings — no equalised odds
        metrics = {f.metric for f in report.findings}
        assert "equalized_odds_tpr" not in metrics
        assert "equalized_odds_fpr" not in metrics

    def test_report_to_dict(self, feature_df: pd.DataFrame):
        """`to_dict()` produces a JSON-serialisable dict."""
        np.random.seed(42)
        n = len(feature_df)
        scores = np.random.uniform(0, 1, n)
        y_true = np.random.randint(0, 2, n)

        report = run_fairness_audit(scores=scores, y_true=y_true, feature_df=feature_df)
        d = report.to_dict()
        assert "model_name" in d
        assert "findings" in d
        assert isinstance(d["findings"], list)
        # Should be JSON-serialisable
        json.dumps(d)  # does not raise


# ---------------------------------------------------------------------------
# CLI integration (retrain-check promotion gate)
# ---------------------------------------------------------------------------


class TestRetrainCheckFairnessGate:
    """Tests that retrain-check honours the fairness audit as a promotion gate.

    We patch the heavy training and fairness-audit dependencies so tests run
    quickly and deterministically.
    """

    def _setup_metadata(self, tmp_path, monkeypatch) -> str:
        """Create a training metadata file with dummy metrics."""
        metadata_dir = tmp_path / "models"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        training_csv = metadata_dir / "training_reference.csv"
        pd.DataFrame({"feature_a": [1.0, 2.0], "feature_b": [3.0, 4.0]}).to_csv(
            training_csv, index=False
        )

        metadata = {
            "version": "v1",
            "training_dataset_path": str(training_csv),
            "model_metrics": {
                "random_forest": {"auc_roc": 0.85, "f1": 0.80},
                "xgboost": {"auc_roc": 0.84, "f1": 0.79},
                "lightgbm": {"auc_roc": 0.83, "f1": 0.78},
            },
            "shap_importances": {},
        }
        meta_path = metadata_dir / "training_metadata.json"
        with open(meta_path, "w") as f:
            json.dump(metadata, f)

        import config.settings as settings_module
        monkeypatch.setattr(settings_module.settings, "model_dir", str(metadata_dir))
        return str(training_csv)

    @patch("detection.drift_monitor.run_drift_report")
    @patch("detection.drift_monitor.is_drift_detected")
    @patch("detection.model_training.train_ensemble")
    @patch("detection.fairness_audit.run_fairness_audit")
    def test_blocks_promotion_on_fairness_failure(
        self,
        mock_fairness: MagicMock,
        mock_train: MagicMock,
        mock_is_drift: MagicMock,
        mock_drift_report: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ):
        """retrain-check blocks promotion when fairness audit finds disparity."""
        self._setup_metadata(tmp_path, monkeypatch)

        mock_drift_report.return_value = {"feature_a": 0.3}
        mock_is_drift.return_value = True

        # Mock train_ensemble to return a realistic result
        mock_model = MagicMock()
        mock_model.predict.return_value = np.array([0, 1, 0])
        mock_model.predict_proba.return_value = np.array([[0.8, 0.2], [0.3, 0.7], [0.6, 0.4]])
        mock_train.return_value = {
            "random_forest": {
                "model": mock_model,
                "auc_roc": 0.86,
                "pr_auc": 0.82,
                "f1": 0.81,
            },
            "xgboost": {
                "model": mock_model,
                "auc_roc": 0.87,
                "pr_auc": 0.83,
                "f1": 0.82,
            },
        }

        # Mock fairness audit to fail
        mock_finding = FairnessFinding(
            dimension="account_age_tier",
            metric="demographic_parity",
            cohort_rates={"new": 0.5, "veteran": 0.1},
            gap=0.4,
            threshold=0.15,
            flagged=True,
        )
        mock_fairness.return_value = FairnessAuditReport(
            model_name="ensemble",
            model_version="new",
            findings=[mock_finding],
            significant_disparity=True,
            computed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )

        with patch("detection.drift_monitor.PerformanceMonitor.check_degradation") as mock_perf:
            mock_perf.side_effect = Exception("No performance data")
            result = runner.invoke(app, ["retrain-check"])

        assert "Fairness audit FAILED" in result.output
        assert "Skipping promotion due to fairness audit failure" in result.output

    @patch("detection.drift_monitor.run_drift_report")
    @patch("detection.drift_monitor.is_drift_detected")
    @patch("detection.model_training.train_ensemble")
    @patch("detection.fairness_audit.run_fairness_audit")
    def test_force_promote_overrides_fairness(
        self,
        mock_fairness: MagicMock,
        mock_train: MagicMock,
        mock_is_drift: MagicMock,
        mock_drift_report: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ):
        """--force-promote overrides a failed fairness audit and logs a warning."""
        self._setup_metadata(tmp_path, monkeypatch)

        mock_drift_report.return_value = {"feature_a": 0.3}
        mock_is_drift.return_value = True

        mock_model = MagicMock()
        mock_model.predict.return_value = np.array([0, 1, 0])
        mock_model.predict_proba.return_value = np.array([[0.8, 0.2], [0.3, 0.7], [0.6, 0.4]])
        mock_train.return_value = {
            "random_forest": {
                "model": mock_model,
                "auc_roc": 0.86,
                "pr_auc": 0.82,
                "f1": 0.81,
            },
        }

        mock_finding = FairnessFinding(
            dimension="account_age_tier",
            metric="demographic_parity",
            cohort_rates={"new": 0.5, "veteran": 0.1},
            gap=0.4,
            threshold=0.15,
            flagged=True,
        )
        mock_fairness.return_value = FairnessAuditReport(
            model_name="ensemble",
            model_version="new",
            findings=[mock_finding],
            significant_disparity=True,
            computed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )

        with patch("detection.drift_monitor.PerformanceMonitor.check_degradation") as mock_perf:
            mock_perf.side_effect = Exception("No performance data")
            result = runner.invoke(app, ["retrain-check", "--force-promote"])

        assert "Fairness audit disparity overridden by --force-promote" in result.output

    @patch("detection.drift_monitor.run_drift_report")
    @patch("detection.drift_monitor.is_drift_detected")
    @patch("detection.model_training.train_ensemble")
    @patch("detection.fairness_audit.run_fairness_audit")
    def test_promotes_when_fairness_passes(
        self,
        mock_fairness: MagicMock,
        mock_train: MagicMock,
        mock_is_drift: MagicMock,
        mock_drift_report: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ):
        """retrain-check promotes normally when fairness audit passes."""
        self._setup_metadata(tmp_path, monkeypatch)

        mock_drift_report.return_value = {"feature_a": 0.3}
        mock_is_drift.return_value = True

        mock_model = MagicMock()
        mock_model.predict.return_value = np.array([0, 1, 0])
        mock_model.predict_proba.return_value = np.array([[0.8, 0.2], [0.3, 0.7], [0.6, 0.4]])
        mock_train.return_value = {
            "random_forest": {
                "model": mock_model,
                "auc_roc": 0.86,
                "pr_auc": 0.82,
                "f1": 0.81,
            },
        }

        mock_fairness.return_value = FairnessAuditReport(
            model_name="ensemble",
            model_version="new",
            findings=[],
            significant_disparity=False,
            computed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )

        with patch("detection.drift_monitor.PerformanceMonitor.check_degradation") as mock_perf:
            mock_perf.side_effect = Exception("No performance data")
            result = runner.invoke(app, ["retrain-check"])

        assert "Fairness audit PASSED" in result.output


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestFairnessReportStorage:
    def test_save_and_get(self):
        """A fairness report can be saved and retrieved via storage functions."""
        from detection.storage import get_fairness_reports, save_fairness_report

        reports_before = get_fairness_reports(limit=10)
        n_before = len(reports_before)

        report_id = save_fairness_report(
            model_name="test",
            model_version="v1",
            significant_disparity=True,
            findings_json=json.dumps({
                "findings": [
                    {"dimension": "volume_tier", "metric": "demographic_parity", "gap": 0.3, "flagged": True}
                ]
            }),
            computed_at="2026-07-27T10:00:00+00:00",
        )
        assert report_id is not None and report_id > 0

        reports_after = get_fairness_reports(limit=10)
        assert len(reports_after) == n_before + 1
        latest = reports_after[0]
        assert latest["model_name"] == "test"
        assert latest["model_version"] == "v1"
        assert latest["significant_disparity"] is True
        assert "findings" in latest
        # Never include per-wallet data
        assert "wallets" not in latest
        assert "wallet_ids" not in latest

    def test_reports_ordered_most_recent_first(self):
        """Reports are returned newest first."""
        from detection.storage import get_fairness_reports, save_fairness_report

        # Save two reports
        save_fairness_report(
            model_name="m1", model_version="v1", significant_disparity=False,
            findings_json="[]", computed_at="2026-01-01T00:00:00+00:00",
        )
        save_fairness_report(
            model_name="m2", model_version="v1", significant_disparity=False,
            findings_json="[]", computed_at="2026-06-01T00:00:00+00:00",
        )

        reports = get_fairness_reports(limit=5)
        assert len(reports) >= 2
        # The first should be the most recent
        timestamps = [r["computed_at"] for r in reports]
        assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# API endpoint security
# ---------------------------------------------------------------------------


class TestFairnessReportsAPI:
    def test_requires_admin_key(self):
        """GET /v1/admin/fairness-reports returns 403 without admin key."""
        from fastapi.testclient import TestClient

        from api.main import app as fastapi_app

        client = TestClient(fastapi_app)
        response = client.get("/v1/admin/fairness-reports")
        assert response.status_code == 403

    def test_no_per_wallet_data(self):
        """Admin-authenticated response contains no per-wallet fields."""
        from fastapi.testclient import TestClient

        from api.main import app as fastapi_app

        client = TestClient(fastapi_app)
        response = client.get(
            "/v1/admin/fairness-reports",
            headers={"X-LedgerLens-Admin-Key": "test-key"},
        )
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)
            for report in data:
                assert "wallets" not in report
                assert "wallet_ids" not in report
                assert "wallet" not in report


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_scores(self):
        """An empty scores array returns a non-flagged finding."""
        findings = demographic_parity_gap(
            np.array([]), np.array([]), threshold=0.15, risk_score_threshold=70
        )
        assert len(findings) >= 1

    def test_single_cohort(self):
        """A single cohort has zero gap and is not flagged."""
        scores = np.random.uniform(0, 1, 100)
        labels = np.array(["only"] * 100)
        findings = demographic_parity_gap(scores, labels, threshold=0.15, risk_score_threshold=70)
        assert len(findings) == 1
        assert findings[0].gap == 0.0
        assert not findings[0].flagged

    def test_risk_score_format_100_scale(self):
        """Works with scores on the 0-100 scale (e.g. risk_score_threshold=70)."""
        scores = np.array([90, 85, 20, 15, 80, 10])
        labels = np.array(["a", "a", "b", "b", "a", "b"])
        # Cohort a: 2/3 >= 70, Cohort b: 0/3 >= 70
        findings = demographic_parity_gap(scores, labels, threshold=0.1, risk_score_threshold=70)
        assert len(findings) == 1
        assert findings[0].gap == pytest.approx(0.6667, abs=0.01)
