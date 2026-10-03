"""Tests for backtesting/backtest_runner.py."""

import json
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.backtest_runner import (
    BacktestReport,
    LookaheadBiasError,
    WalkForwardConfig,
    _compute_metrics,
    assert_no_lookahead,
    load_labelled_dataset,
    run_walk_forward_backtest,
    save_report,
    walk_forward_splits,
)


def _write_csv(tmp_path: Path, content: str) -> str:
    csv_path = tmp_path / "test_cases.csv"
    csv_path.write_text(content)
    return str(csv_path)


class TestLoadLabelledDataset:
    def test_loads_valid_csv(self, tmp_path):
        path = _write_csv(tmp_path, "wallet,label,start_date,end_date\nGABC,1,2026-01-01,2026-03-31\nGDEF,0,2026-01-01,2026-03-31\n")
        df = load_labelled_dataset(path)
        assert len(df) == 2
        assert list(df.columns) >= ["wallet", "label"]

    def test_missing_columns_raises(self, tmp_path):
        path = _write_csv(tmp_path, "name,value\nfoo,1\n")
        with pytest.raises(ValueError, match="Missing required columns"):
            load_labelled_dataset(path)


class TestComputeMetrics:
    def test_perfect_classification(self):
        y_true = np.array([1, 1, 0, 0])
        y_scores = np.array([90, 85, 30, 20])
        m = _compute_metrics(y_true, y_scores, threshold=70)
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0
        assert m["f1"] == 1.0
        assert m["tp"] == 2
        assert m["fp"] == 0

    def test_threshold_effect(self):
        y_true = np.array([1, 1, 0, 0])
        y_scores = np.array([90, 50, 30, 20])
        m70 = _compute_metrics(y_true, y_scores, threshold=70)
        m40 = _compute_metrics(y_true, y_scores, threshold=40)
        assert m70["recall"] < m40["recall"]

    def test_all_negative(self):
        y_true = np.array([0, 0, 0])
        y_scores = np.array([10, 20, 30])
        m = _compute_metrics(y_true, y_scores, threshold=70)
        assert m["tp"] == 0
        assert m["precision"] == 0.0

    def test_all_positive_above_threshold(self):
        y_true = np.array([1, 1, 1])
        y_scores = np.array([80, 90, 75])
        m = _compute_metrics(y_true, y_scores, threshold=70)
        assert m["recall"] == 1.0


class TestSaveReport:
    def test_saves_json(self, tmp_path):
        report = BacktestReport(
            dataset_path="test.csv",
            threshold=70,
            total_wallets=4,
            labelled_positive=2,
            labelled_negative=2,
            predicted_positive=2,
            true_positives=2,
            false_positives=0,
            false_negatives=0,
            true_negatives=2,
            precision=1.0,
            recall=1.0,
            f1=1.0,
            auc_roc=1.0,
            average_precision=1.0,
            per_wallet=[],
            generated_at="2026-06-25T00:00:00",
        )
        path = save_report(report, output_dir=str(tmp_path))
        assert Path(path).exists()
        with open(path) as f:
            data = json.load(f)
        assert data["precision"] == 1.0
        assert data["total_wallets"] == 4


# ---------------------------------------------------------------------------
# Walk-forward mode
# ---------------------------------------------------------------------------


def _lookahead_trap_dataset(n: int = 400) -> pd.DataFrame:
    """Rows whose feature ``x`` is a unique random key and whose label is noise.

    Nothing about ``x`` generalises, so an honest evaluation of a model that
    memorises ``x -> label`` must score at chance.  If any test row leaks
    into training, the memoriser recognises it and scores perfectly -- a
    known, detectable lookahead-bias trap.
    """
    rng = np.random.default_rng(7)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC"),
        "x": rng.permutation(n).astype(float),
        "label": rng.integers(0, 2, size=n),
    })


def _fit_memoriser(train: pd.DataFrame) -> dict:
    return dict(zip(train["x"], train["label"]))


def _predict_memoriser(model: dict, test: pd.DataFrame) -> np.ndarray:
    return np.array([100.0 * model[x] if x in model else 50.0 for x in test["x"]])


def _config(**overrides) -> WalkForwardConfig:
    kwargs = {
        "train_window": pd.Timedelta(days=90),
        "test_window": pd.Timedelta(days=30),
        "step": pd.Timedelta(days=30),
    }
    kwargs.update(overrides)
    return WalkForwardConfig(**kwargs)


class TestWalkForward:
    def test_lookahead_trap_is_not_triggered(self):
        df = _lookahead_trap_dataset()
        report = run_walk_forward_backtest(df, _config(), _fit_memoriser, _predict_memoriser, threshold=70)

        # A leaky evaluation (train on everything) falls into the trap ...
        leaky = _compute_metrics(df["label"].to_numpy(), _predict_memoriser(_fit_memoriser(df), df), 70)
        assert leaky["auc_roc"] > 0.99
        # ... walk-forward does not: the memoriser is exactly at chance.
        assert report.aggregate["auc_roc"] == pytest.approx(0.5)
        assert len(report.folds) >= 5

    def test_training_strictly_precedes_each_test_window(self):
        df = _lookahead_trap_dataset()
        seen: list[pd.Timestamp] = []

        def fit(train):
            seen.append(train["timestamp"].max())
            return _fit_memoriser(train)

        report = run_walk_forward_backtest(df, _config(), fit, _predict_memoriser)
        for train_max, fold in zip(seen, report.folds):
            assert train_max < pd.Timestamp(fold.test_start)

    def test_rolling_window_and_step_are_respected(self):
        df = _lookahead_trap_dataset()
        splits = walk_forward_splits(df, _config())
        for train, test, boundary, test_end in splits:
            assert train["timestamp"].min() >= boundary - pd.Timedelta(days=90)
            assert test["timestamp"].max() < test_end
        boundaries = [b for _, _, b, _ in splits]
        assert all(b2 - b1 == pd.Timedelta(days=30) for b1, b2 in pairwise(boundaries))

    def test_expanding_window_and_gap(self):
        df = _lookahead_trap_dataset()
        splits = walk_forward_splits(df, _config(train_window=None, gap=pd.Timedelta(days=7)))
        for train, _, boundary, _ in splits:
            assert train["timestamp"].min() == df["timestamp"].min()
            assert train["timestamp"].max() < boundary - pd.Timedelta(days=7)

    def test_labels_known_only_after_boundary_are_excluded_from_training(self):
        df = _lookahead_trap_dataset()
        # Labels are confirmed 60 days after the observation.
        df["label_known_at"] = df["timestamp"] + pd.Timedelta(days=60)
        for train, _, boundary, _ in walk_forward_splits(df, _config(label_time_column="label_known_at")):
            assert train["label_known_at"].max() < boundary

    def test_assert_no_lookahead_detects_leakage(self):
        df = _lookahead_trap_dataset()
        boundary = df["timestamp"].iloc[200]
        with pytest.raises(LookaheadBiasError):
            assert_no_lookahead(df.iloc[:210], df.iloc[200:230], _config(), boundary)

    def test_invalid_config_rejected(self):
        with pytest.raises(ValueError):
            _config(step=pd.Timedelta(0))
