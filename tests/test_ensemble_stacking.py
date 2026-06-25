"""Tests for ensemble stacking meta-learner (Issue-111)."""

import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier

from detection.model_training import (
    _build_meta_features,
    _walk_forward_cv,
    generate_oof_predictions,
    train_meta_learner,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _synthetic_binary(n: int = 300, n_features: int = 5, seed: int = 42):
    rng = np.random.default_rng(seed)
    X = rng.uniform(-2, 2, size=(n, n_features))
    y = (X[:, 0] + rng.normal(0, 0.3, n) > 0).astype(int)
    timestamps = np.arange(n, dtype=float)
    return X, y, timestamps


def _base_models():
    return {
        "rf": RandomForestClassifier(n_estimators=10, random_state=42),
        "xgb": XGBClassifier(n_estimators=10, eval_metric="logloss", random_state=42, verbosity=0),
        "lgbm": LGBMClassifier(n_estimators=10, random_state=42, verbose=-1),
    }


# ---------------------------------------------------------------------------
# _walk_forward_cv
# ---------------------------------------------------------------------------


class TestWalkForwardCV:
    def test_returns_correct_number_of_folds(self):
        X, y, ts = _synthetic_binary(n=200)
        folds = list(_walk_forward_cv(X, ts, n_splits=5, gap_days=0.0))
        assert len(folds) <= 5

    def test_no_overlap_between_train_and_val(self):
        _, _, ts = _synthetic_binary(n=200)
        for train_idx, val_idx in _walk_forward_cv(np.zeros((200, 1)), ts, n_splits=5, gap_days=0.0):
            assert len(np.intersect1d(train_idx, val_idx)) == 0

    def test_train_always_before_val_in_time(self):
        _, _, ts = _synthetic_binary(n=200)
        for train_idx, val_idx in _walk_forward_cv(np.zeros((200, 1)), ts, n_splits=5, gap_days=0.0):
            assert ts[train_idx].max() <= ts[val_idx].min()


# ---------------------------------------------------------------------------
# generate_oof_predictions
# ---------------------------------------------------------------------------


class TestGenerateOOFPredictions:
    def test_oof_shape(self):
        X, y, ts = _synthetic_binary(n=300)
        # gap_days=0.0: avoid excluding all training data with integer proxy timestamps
        oof_proba, oof_labels = generate_oof_predictions(X, y, ts, _base_models(), n_splits=3, gap_days=0.0)
        assert oof_proba.ndim == 2
        assert oof_proba.shape[1] == 3
        assert len(oof_labels) == oof_proba.shape[0]
        assert len(oof_labels) <= len(y)

    def test_oof_probabilities_in_unit_interval(self):
        X, y, ts = _synthetic_binary(n=300)
        oof_proba, _ = generate_oof_predictions(X, y, ts, _base_models(), n_splits=3, gap_days=0.0)
        assert (oof_proba >= 0).all()
        assert (oof_proba <= 1).all()

    def test_bad_model_raises_with_name(self):
        """A model that raises during fit should propagate a ValueError with model name."""
        from sklearn.base import BaseEstimator

        class BrokenModel(BaseEstimator):
            def fit(self, X, y, **kw):
                raise RuntimeError("fit exploded")

            def predict_proba(self, X):
                return np.zeros((len(X), 2))

        X, y, ts = _synthetic_binary(n=200)
        with pytest.raises(ValueError, match="broken"):
            generate_oof_predictions(
                X, y, ts,
                {"rf": _base_models()["rf"], "broken": BrokenModel(), "lgbm": _base_models()["lgbm"]},
                n_splits=3, gap_days=0.0,
            )


# ---------------------------------------------------------------------------
# train_meta_learner
# ---------------------------------------------------------------------------


class TestTrainMetaLearner:
    def test_returns_logistic_regression(self):
        X, y, ts = _synthetic_binary(n=300)
        oof_proba, oof_labels = generate_oof_predictions(X, y, ts, _base_models(), n_splits=3, gap_days=0.0)
        meta = train_meta_learner(oof_proba, oof_labels)
        assert isinstance(meta, LogisticRegression)

    def test_coefficients_exist_and_positive_sum(self):
        X, y, ts = _synthetic_binary(n=300)
        oof_proba, oof_labels = generate_oof_predictions(X, y, ts, _base_models(), n_splits=3, gap_days=0.0)
        meta = train_meta_learner(oof_proba, oof_labels)
        if meta is not None:
            assert np.sum(meta.coef_) > 0, "Meta-learner coefficients should sum positive"

    def test_single_class_oof_returns_none(self):
        """If OOF labels have only one class, meta-learner should return None."""
        oof_proba = np.random.rand(100, 3)
        oof_labels = np.zeros(100, dtype=int)
        meta = train_meta_learner(oof_proba, oof_labels)
        assert meta is None

    def test_disagrement_features_added(self):
        X, y, ts = _synthetic_binary(n=300)
        oof_proba, oof_labels = generate_oof_predictions(X, y, ts, _base_models(), n_splits=3, gap_days=0.0)
        meta = train_meta_learner(oof_proba, oof_labels, use_disagreement_features=True)
        if meta is not None:
            assert meta.coef_.shape[1] == 5  # 3 base + disagreement + mean


# ---------------------------------------------------------------------------
# _build_meta_features
# ---------------------------------------------------------------------------


class TestBuildMetaFeatures:
    def test_no_disagreement_returns_input(self):
        inp = np.random.rand(5, 3)
        out = _build_meta_features(inp, use_disagreement=False)
        np.testing.assert_array_equal(out, inp)

    def test_disagreement_adds_two_columns(self):
        inp = np.random.rand(5, 3)
        out = _build_meta_features(inp, use_disagreement=True)
        assert out.shape == (5, 5)

    def test_disagreement_column_values(self):
        inp = np.array([[0.8, 0.1, 0.1], [0.4, 0.4, 0.2]])
        out = _build_meta_features(inp, use_disagreement=True)
        # column 3 = max - min
        assert pytest.approx(out[0, 3], abs=1e-6) == 0.7
        assert pytest.approx(out[1, 3], abs=1e-6) == 0.2


# ---------------------------------------------------------------------------
# Inference integration
# ---------------------------------------------------------------------------


class TestInferenceWithMetaLearner:
    def _make_models_with_meta(self):
        from types import SimpleNamespace

        def _prob(p):
            return lambda X: np.column_stack([1 - np.full(len(X), p), np.full(len(X), p)])

        rf = SimpleNamespace(predict_proba=_prob(0.7), feature_names_in_=None)
        xgb = SimpleNamespace(predict_proba=_prob(0.8), feature_names_in_=None)
        lgbm = SimpleNamespace(predict_proba=_prob(0.6), feature_names_in_=None)

        # Make a tiny meta-learner
        meta = LogisticRegression(C=1.0, max_iter=100, random_state=42)
        X_meta = np.array([[0.7, 0.8, 0.6], [0.2, 0.1, 0.3]])
        y_meta = np.array([1, 0])
        meta.fit(_build_meta_features(X_meta), y_meta)

        return {
            "random_forest": rf,
            "xgboost": xgb,
            "lightgbm": lgbm,
            "meta_learner": meta,
        }

    def test_meta_learner_inference_in_unit_interval(self):
        from detection.feature_engineering import FEATURE_NAMES
        from detection.model_inference import score_feature_vector

        models = self._make_models_with_meta()
        fv = {name: 0.0 for name in FEATURE_NAMES}
        prob, conf = score_feature_vector(models, fv)
        assert 0.0 <= prob <= 1.0
        assert 0.0 <= conf <= 1.0

    def test_fallback_without_meta_learner(self):
        from types import SimpleNamespace
        from detection.feature_engineering import FEATURE_NAMES
        from detection.model_inference import score_feature_vector

        def _prob(p):
            return lambda X: np.column_stack([1 - np.full(len(X), p), np.full(len(X), p)])

        models = {
            "random_forest": SimpleNamespace(predict_proba=_prob(0.5), feature_names_in_=None),
            "xgboost": SimpleNamespace(predict_proba=_prob(0.5), feature_names_in_=None),
            "lightgbm": SimpleNamespace(predict_proba=_prob(0.5), feature_names_in_=None),
        }
        fv = {name: 0.0 for name in FEATURE_NAMES}
        prob, conf = score_feature_vector(models, fv)
        assert 0.0 <= prob <= 1.0
