"""Tests for RAPS multi-class conformal prediction (Issue-109)."""

import json

import numpy as np
import pytest

from detection.conformal import (
    RAPSConformal,
    aggregate_ensemble_softmax,
    calibrate_raps,
    predict_set_raps,
    raps_score,
    score_to_class,
    validate_coverage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uniform_softmax(n: int, k: int = 3, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    raw = rng.dirichlet(np.ones(k), size=n)
    return raw


# ---------------------------------------------------------------------------
# score_to_class
# ---------------------------------------------------------------------------


class TestScoreToClass:
    def test_clean_lower_bound(self):
        assert score_to_class(0) == 0

    def test_clean_upper_bound(self):
        assert score_to_class(33) == 0

    def test_suspicious_lower_bound(self):
        assert score_to_class(34) == 1

    def test_suspicious_upper_bound(self):
        assert score_to_class(66) == 1

    def test_wash_lower_bound(self):
        assert score_to_class(67) == 2

    def test_wash_upper_bound(self):
        assert score_to_class(100) == 2


# ---------------------------------------------------------------------------
# raps_score
# ---------------------------------------------------------------------------


class TestRAPSScore:
    def test_known_value_true_class_0(self):
        """raps_score([0.7, 0.2, 0.1], true_class=0) == 0.7 (rank 0, no reg)."""
        probs = np.array([0.7, 0.2, 0.1])
        score = raps_score(probs, true_class=0, lambda_reg=0.2, k_reg=2)
        assert pytest.approx(score, abs=1e-6) == 0.7

    def test_known_value_true_class_1(self):
        """raps_score([0.7, 0.2, 0.1], true_class=1): rank=1, cumsum[1]=0.9."""
        probs = np.array([0.7, 0.2, 0.1])
        # sorted: [0.7, 0.2, 0.1], rank of class 1 is 1 (0-indexed)
        # cumsum[1] = 0.9, reg = lambda * max(2-2, 0) = 0
        score = raps_score(probs, true_class=1, lambda_reg=0.2, k_reg=2)
        assert pytest.approx(score, abs=1e-6) == 0.9

    def test_known_value_true_class_2_lowest(self):
        """true_class=2 (rank=2) includes regularisation penalty."""
        probs = np.array([0.7, 0.2, 0.1])
        # sorted: [0.7, 0.2, 0.1], rank of class 2 is 2 (0-indexed)
        # cumsum[2] = 1.0, reg = 0.2 * max(3-2, 0) = 0.2
        score = raps_score(probs, true_class=2, lambda_reg=0.2, k_reg=2)
        assert pytest.approx(score, abs=1e-6) == 1.2

    def test_regularisation_not_applied_within_k_reg(self):
        """Regularisation is zero when rank + 1 <= k_reg."""
        probs = np.array([0.6, 0.3, 0.1])
        # rank of class 1 is 1 (0-indexed), rank+1=2 == k_reg → reg = 0
        score_with_reg = raps_score(probs, true_class=1, lambda_reg=0.5, k_reg=2)
        score_no_reg = raps_score(probs, true_class=1, lambda_reg=0.0, k_reg=2)
        assert pytest.approx(score_with_reg) == score_no_reg

    def test_score_is_positive(self):
        probs = np.array([0.4, 0.4, 0.2])
        for k in range(3):
            assert raps_score(probs, k) > 0


# ---------------------------------------------------------------------------
# calibrate_raps and predict_set_raps
# ---------------------------------------------------------------------------


class TestCalibrateAndPredict:
    def _calibration_data(self, n: int = 300, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
        rng = np.random.default_rng(seed)
        labels = rng.integers(0, 3, size=n)
        probs = _uniform_softmax(n, k=3, seed=seed)
        return probs, labels

    def test_calibrate_returns_finite_positive_q_hat(self):
        probs, labels = self._calibration_data(n=1000)
        q_hat = calibrate_raps(probs, labels, alpha=0.10)
        assert np.isfinite(q_hat)
        assert q_hat > 0

    def test_predict_set_all_classes_at_large_q_hat(self):
        """With q_hat=Inf, all classes should be included."""
        probs = np.array([0.7, 0.2, 0.1])
        ps = predict_set_raps(probs, q_hat=np.inf)
        assert sorted(ps) == [0, 1, 2]

    def test_predict_set_empty_at_zero_q_hat(self):
        """With q_hat=0, prediction set may be empty (or just highest-prob class)."""
        probs = np.array([0.7, 0.2, 0.1])
        ps = predict_set_raps(probs, q_hat=0.0)
        # cumsum[0]=0.7 > 0, so no class qualifies
        assert ps == []

    def test_predict_set_contains_highest_prob_class(self):
        """The highest-probability class should almost always appear in the set."""
        probs = np.array([0.8, 0.15, 0.05])
        q_hat = calibrate_raps(np.tile(probs, (200, 1)), np.zeros(200, dtype=int), alpha=0.10)
        ps = predict_set_raps(probs, q_hat)
        assert 0 in ps, f"Expected class 0 in prediction set, got {ps}"


# ---------------------------------------------------------------------------
# validate_coverage
# ---------------------------------------------------------------------------


class TestValidateCoverage:
    def test_coverage_within_tolerance(self):
        n = 500
        rng = np.random.default_rng(7)
        probs = _uniform_softmax(n, k=3, seed=7)
        labels = rng.integers(0, 3, size=n)
        q_hat = calibrate_raps(probs, labels, alpha=0.10)
        # Use a generous tolerance since we calibrate and validate on the same data
        achieved = validate_coverage(probs, labels, q_hat, alpha=0.10, tolerance=0.15)
        assert 0.0 <= achieved <= 1.0

    def test_coverage_assertion_fails_on_zero_q_hat(self):
        probs = _uniform_softmax(200, k=3, seed=8)
        labels = np.arange(200) % 3
        with pytest.raises(AssertionError):
            validate_coverage(probs, labels, q_hat=0.0, alpha=0.10, tolerance=0.02)


# ---------------------------------------------------------------------------
# RAPSConformal class
# ---------------------------------------------------------------------------


class TestRAPSConformal:
    def test_calibrate_sets_q_hat(self):
        raps = RAPSConformal(alpha=0.10)
        n = 300
        probs = _uniform_softmax(n, seed=1)
        labels = np.arange(n) % 3
        raps.calibrate(probs, labels)
        assert raps.q_hat is not None
        assert np.isfinite(raps.q_hat)
        assert raps.n_calibration == n

    def test_predict_set_before_calibrate_raises(self):
        raps = RAPSConformal()
        with pytest.raises(RuntimeError, match="calibrate"):
            raps.predict_set(np.array([0.5, 0.3, 0.2]))

    def test_predict_set_returns_sorted_list(self):
        raps = RAPSConformal(alpha=0.10)
        n = 300
        probs = _uniform_softmax(n, seed=2)
        labels = np.arange(n) % 3
        raps.calibrate(probs, labels)
        test_probs = np.array([0.6, 0.3, 0.1])
        ps = raps.predict_set(test_probs)
        assert ps == sorted(ps)
        assert all(k in (0, 1, 2) for k in ps)

    def test_save_load_round_trip(self, tmp_path):
        raps = RAPSConformal(alpha=0.10)
        probs = _uniform_softmax(200, seed=3)
        labels = np.arange(200) % 3
        raps.calibrate(probs, labels)
        path = str(tmp_path / "raps.json")
        raps.save(path)

        loaded = RAPSConformal.load(path)
        assert pytest.approx(loaded.q_hat) == raps.q_hat
        assert loaded.alpha == raps.alpha
        assert loaded.n_calibration == raps.n_calibration

    def test_save_integrity_tamper_raises(self, tmp_path):
        from detection.conformal import CalibrationIntegrityError

        raps = RAPSConformal(alpha=0.10)
        probs = _uniform_softmax(200, seed=4)
        labels = np.arange(200) % 3
        raps.calibrate(probs, labels)
        path = str(tmp_path / "raps.json")
        raps.save(path)

        with open(path) as f:
            payload = json.load(f)
        payload["data"]["q_hat"] = 999.0
        with open(path, "w") as f:
            json.dump(payload, f)

        with pytest.raises(CalibrationIntegrityError):
            RAPSConformal.load(path)

    def test_small_calibration_set_warns(self, caplog):
        import logging
        raps = RAPSConformal(alpha=0.10)
        probs = _uniform_softmax(10, seed=5)
        labels = np.arange(10) % 3
        with caplog.at_level(logging.WARNING, logger="ledgerlens.conformal"):
            raps.calibrate(probs, labels)
        assert any("unreliable" in m for m in caplog.messages)

    def test_alpha_near_zero_prediction_set_all_classes(self):
        """alpha very small (near 100% target coverage) → prediction set includes all classes."""
        raps = RAPSConformal(alpha=0.001)  # near-0 alpha rather than exact 0
        probs = _uniform_softmax(200, seed=6)
        labels = np.arange(200) % 3
        raps.calibrate(probs, labels)
        test_probs = np.array([0.5, 0.3, 0.2])
        ps = raps.predict_set(test_probs)
        assert sorted(ps) == [0, 1, 2], f"Expected all classes at near-zero alpha, got {ps}"


# ---------------------------------------------------------------------------
# aggregate_ensemble_softmax
# ---------------------------------------------------------------------------


class TestAggregateEnsembleSoftmax:
    def test_output_sums_to_one(self):
        rf = np.array([0.3, 0.7])
        xgb = np.array([0.2, 0.8])
        lgbm = np.array([0.4, 0.6])
        result = aggregate_ensemble_softmax(rf, xgb, lgbm)
        assert len(result) == 3
        assert pytest.approx(result.sum()) == 1.0

    def test_all_three_clean(self):
        """All models predict 0% → all land in class 0 → result is [1, 0, 0]."""
        rf = np.array([1.0, 0.0])
        xgb = np.array([1.0, 0.0])
        lgbm = np.array([1.0, 0.0])
        result = aggregate_ensemble_softmax(rf, xgb, lgbm)
        assert result[0] == pytest.approx(1.0)
        assert result[1] == pytest.approx(0.0)
        assert result[2] == pytest.approx(0.0)
