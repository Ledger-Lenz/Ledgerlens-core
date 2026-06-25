"""Tests for CausalFeatureSelector and PC-skeleton feature selection (Issue #113).

Verifies the Fisher's Z test, the PC skeleton phase, the train_ensemble
causal_feature_selection parameter, and training_metadata.json persistence.
"""

import json

import numpy as np
import pandas as pd

from detection.causal_engine import (
    CausalFeatureSelector,
    _fishers_z_test,
    _partial_correlation,
)


# ---------------------------------------------------------------------------
# _partial_correlation helper
# ---------------------------------------------------------------------------


def test_partial_correlation_no_conditioning():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(200)
    y = 2 * x + rng.standard_normal(200) * 0.1
    s = np.empty((200, 0))
    r = _partial_correlation(x, y, s)
    assert abs(r) > 0.9


def test_partial_correlation_removes_linear_dependence():
    rng = np.random.default_rng(1)
    z = rng.standard_normal(300)
    x = z + rng.standard_normal(300) * 0.05
    y = z + rng.standard_normal(300) * 0.05
    # x and y are independent given z
    s = z.reshape(-1, 1)
    r = _partial_correlation(x, y, s)
    assert abs(r) < 0.3


# ---------------------------------------------------------------------------
# _fishers_z_test
# ---------------------------------------------------------------------------


def test_fishers_z_test_detects_marginal_dependence():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(500)
    y = 3 * x + rng.standard_normal(500) * 0.1
    s = np.empty((500, 0))
    # Should NOT be independent (p < alpha)
    assert not _fishers_z_test(x, y, s, alpha=0.01)


def test_fishers_z_test_detects_independence():
    rng = np.random.default_rng(2)
    x = rng.standard_normal(500)
    y = rng.standard_normal(500)
    s = np.empty((500, 0))
    assert _fishers_z_test(x, y, s, alpha=0.05)


def test_fishers_z_test_conditional_independence():
    rng = np.random.default_rng(3)
    z = rng.standard_normal(400)
    x = z + rng.standard_normal(400) * 0.05
    y = z + rng.standard_normal(400) * 0.05
    s = z.reshape(-1, 1)
    # Given z, x and y should be detected as conditionally independent
    assert _fishers_z_test(x, y, s, alpha=0.05)


def test_fishers_z_test_too_few_samples_retains_edge():
    # With very few samples relative to conditioning set size, edge should be kept.
    rng = np.random.default_rng(4)
    x = rng.standard_normal(5)
    y = rng.standard_normal(5)
    s = rng.standard_normal((5, 4))  # n - |S| - 3 <= 0
    # Should retain edge (return False) when df <= 0
    assert not _fishers_z_test(x, y, s, alpha=0.05)


# ---------------------------------------------------------------------------
# CausalFeatureSelector
# ---------------------------------------------------------------------------


def _make_selection_data(seed: int = 0) -> tuple:
    """Build a toy dataset: features 0-1 directly cause the label; feature 2 is pure noise."""
    rng = np.random.default_rng(seed)
    n = 800
    # Causal features (strong direct signal)
    f0 = rng.standard_normal(n)
    f1 = rng.standard_normal(n)
    y = (2.0 * f0 + 1.5 * f1 > 0.5).astype(float)
    # Correlated-but-caused-by-y features with large noise (so conditioning on
    # them doesn't collapse y's variance)
    f2 = y + rng.standard_normal(n) * 2.0  # weak descendant
    # Purely noise feature (marginal independence from y)
    f3 = rng.standard_normal(n)
    X = np.column_stack([f0, f1, f2, f3])
    names = ["f0", "f1", "f2", "f3"]
    return X, y, names


def test_causal_selector_returns_list():
    X, y, names = _make_selection_data()
    sel = CausalFeatureSelector(alpha=0.05, max_conditioning_size=1)
    selected = sel.fit(X, y, names)
    assert isinstance(selected, list)
    assert len(selected) > 0


def test_causal_selector_retains_causal_features():
    """Direct causes of the label must survive the PC skeleton phase."""
    X, y, names = _make_selection_data()
    sel = CausalFeatureSelector(alpha=0.001, max_conditioning_size=0)
    selected = sel.fit(X, y, names)
    # At level l=0 (marginal tests only), f0 and f1 are highly correlated with
    # y so they are retained.
    for name in ("f0", "f1"):
        assert name in selected, f"{name} should be in causal selection"


def test_causal_selector_removes_noise_feature():
    """Pure noise feature must be pruned at the marginal independence level."""
    X, y, names = _make_selection_data()
    sel = CausalFeatureSelector(alpha=0.01, max_conditioning_size=0)
    sel.fit(X, y, names)
    # f3 (pure noise) should not be in the selected set
    assert "f3" not in sel.selected_features_


def test_causal_selector_stores_selected_features_attribute():
    X, y, names = _make_selection_data()
    sel = CausalFeatureSelector(alpha=0.05, max_conditioning_size=1)
    returned = sel.fit(X, y, names)
    assert returned == sel.selected_features_
    assert sel.n_features_in_ == 4


def test_causal_selector_empty_data_returns_all():
    # Very small dataset → all features retained (insufficient df)
    sel = CausalFeatureSelector(alpha=0.01, max_conditioning_size=1)
    X = np.zeros((5, 3))
    y = np.zeros(5)
    selected = sel.fit(X, y, ["a", "b", "c"])
    assert set(selected) == {"a", "b", "c"}


def test_causal_selector_no_features_returns_empty():
    sel = CausalFeatureSelector()
    selected = sel.fit(np.empty((100, 0)), np.zeros(100), [])
    assert selected == []


# ---------------------------------------------------------------------------
# train_ensemble causal_feature_selection parameter
# ---------------------------------------------------------------------------


def _make_tiny_df(n_clean: int = 60, n_wash: int = 10, seed: int = 42) -> pd.DataFrame:
    from detection.dataset import build_training_dataset
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=n_clean,
        n_wash_rings=n_wash // 3,
        ring_size=3,
        seed=seed,
    )
    return build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)


def test_train_ensemble_causal_selection_flag_off_by_default(tmp_path):
    from detection.model_training import save_models, train_ensemble

    df = _make_tiny_df()
    results = train_ensemble(df, calibrate=False)
    assert "_causal_selected_features" not in results

    save_models(results, model_dir=str(tmp_path))
    meta_path = tmp_path / "training_metadata.json"
    with open(meta_path) as f:
        meta = json.load(f)
    assert meta["causal_feature_selection"] is False
    assert meta["causal_selected_features"] == []


def test_train_ensemble_causal_selection_enabled(tmp_path):
    from detection.model_training import train_ensemble

    df = _make_tiny_df()
    results = train_ensemble(df, calibrate=False, causal_feature_selection=True)

    # Check that selected features are stored
    assert "_causal_selected_features" in results
    selected = results["_causal_selected_features"]
    assert isinstance(selected, list)
    assert len(selected) > 0

    # All selected feature names must be valid FEATURE_NAMES
    from detection.feature_engineering import FEATURE_NAMES
    for name in selected:
        assert name in FEATURE_NAMES, f"{name!r} not in FEATURE_NAMES"

    # Models should still train successfully
    for model_name in ("random_forest", "xgboost", "lightgbm"):
        assert model_name in results
        assert "pr_auc" in results[model_name]


def test_train_ensemble_causal_selection_persisted_to_metadata(tmp_path):
    from detection.model_training import save_models, train_ensemble

    df = _make_tiny_df()
    results = train_ensemble(df, calibrate=False, causal_feature_selection=True)
    save_models(results, model_dir=str(tmp_path))

    meta_path = tmp_path / "training_metadata.json"
    assert meta_path.exists()
    with open(meta_path) as f:
        meta = json.load(f)
    assert meta["causal_feature_selection"] is True
    assert isinstance(meta["causal_selected_features"], list)
    assert len(meta["causal_selected_features"]) > 0
