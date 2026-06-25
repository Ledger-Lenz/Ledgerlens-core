"""Tests for SMOTE variant oversampling strategies (Issue #105).

Verifies _get_oversampler factory, imbalance_strategy parameter flow,
compare_oversamplers function, and training_metadata.json persistence.
"""

import json

import pandas as pd
import pytest
from imblearn.over_sampling import ADASYN, SMOTE, BorderlineSMOTE

from detection.model_training import _get_oversampler, compare_oversamplers


# ---------------------------------------------------------------------------
# _get_oversampler factory
# ---------------------------------------------------------------------------


def test_get_oversampler_smote():
    sampler = _get_oversampler("smote")
    assert isinstance(sampler, SMOTE)


def test_get_oversampler_adasyn():
    sampler = _get_oversampler("adasyn")
    assert isinstance(sampler, ADASYN)


def test_get_oversampler_borderline1():
    sampler = _get_oversampler("borderline1")
    assert isinstance(sampler, BorderlineSMOTE)
    assert sampler.kind == "borderline-1"


def test_get_oversampler_borderline2():
    sampler = _get_oversampler("borderline2")
    assert isinstance(sampler, BorderlineSMOTE)
    assert sampler.kind == "borderline-2"


def test_get_oversampler_none_returns_none():
    assert _get_oversampler("none") is None


def test_get_oversampler_case_insensitive():
    assert isinstance(_get_oversampler("SMOTE"), SMOTE)
    assert isinstance(_get_oversampler("ADASYN"), ADASYN)


def test_get_oversampler_unknown_raises():
    with pytest.raises(ValueError, match="Unknown imbalance_strategy"):
        _get_oversampler("random_forest")


def test_oversampler_has_tuned_defaults():
    smote = _get_oversampler("smote")
    assert smote.k_neighbors == 5

    adasyn = _get_oversampler("adasyn")
    assert adasyn.n_neighbors == 5

    bl1 = _get_oversampler("borderline1")
    assert bl1.k_neighbors == 5


# ---------------------------------------------------------------------------
# train_ensemble with imbalance_strategy
# ---------------------------------------------------------------------------


def _make_tiny_df(n_clean: int = 60, n_wash: int = 10, seed: int = 42) -> pd.DataFrame:
    """Build a minimal labelled DataFrame for fast training tests."""
    from detection.dataset import build_training_dataset
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=n_clean,
        n_wash_rings=n_wash // 3,
        ring_size=3,
        seed=seed,
    )
    return build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)


def test_train_ensemble_default_strategy_is_smote(tmp_path):
    from detection.model_training import save_models, train_ensemble

    df = _make_tiny_df()
    results = train_ensemble(df, calibrate=False)
    assert results.get("_imbalance_strategy") == "smote"

    save_models(results, model_dir=str(tmp_path))
    meta_path = tmp_path / "training_metadata.json"
    assert meta_path.exists()
    with open(meta_path) as f:
        meta = json.load(f)
    assert meta["imbalance_strategy"] == "smote"


def test_train_ensemble_none_strategy_skips_resampling(tmp_path):
    from detection.model_training import save_models, train_ensemble

    df = _make_tiny_df()
    results = train_ensemble(df, calibrate=False, imbalance_strategy="none")
    assert results.get("_imbalance_strategy") == "none"

    save_models(results, model_dir=str(tmp_path))
    meta_path = tmp_path / "training_metadata.json"
    with open(meta_path) as f:
        meta = json.load(f)
    assert meta["imbalance_strategy"] == "none"


def test_train_ensemble_adasyn_strategy(tmp_path):
    from detection.model_training import train_ensemble

    df = _make_tiny_df()
    results = train_ensemble(df, calibrate=False, imbalance_strategy="adasyn")
    assert results.get("_imbalance_strategy") == "adasyn"
    # Models should still be trained
    for name in ("random_forest", "xgboost", "lightgbm"):
        assert name in results
        assert "pr_auc" in results[name]


# ---------------------------------------------------------------------------
# compare_oversamplers
# ---------------------------------------------------------------------------


def test_compare_oversamplers_returns_dataframe():
    df = _make_tiny_df()
    comparison = compare_oversamplers(df, strategies=["smote", "none"])
    assert hasattr(comparison, "columns")
    assert set(comparison.columns) >= {"strategy", "model", "auc_pr", "auc_roc", "f1"}


def test_compare_oversamplers_covers_all_models():
    df = _make_tiny_df()
    comparison = compare_oversamplers(df, strategies=["smote"])
    models = set(comparison["model"].tolist())
    assert models >= {"random_forest", "xgboost", "lightgbm"}


def test_compare_oversamplers_best_strategy_attribute():
    df = _make_tiny_df()
    comparison = compare_oversamplers(df, strategies=["smote", "none"])
    assert "best_strategy" in comparison.attrs
    assert comparison.attrs["best_strategy"] in ("smote", "none")


def test_compare_oversamplers_sorted_by_auc_pr():
    df = _make_tiny_df()
    comparison = compare_oversamplers(df, strategies=["smote", "none"])
    pr_values = comparison["auc_pr"].tolist()
    assert pr_values == sorted(pr_values, reverse=True)
