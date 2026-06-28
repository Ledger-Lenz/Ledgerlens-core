"""Tests for backtesting/backtest_runner.py."""

import json
from pathlib import Path

import numpy as np
import pytest

from backtesting.backtest_runner import (
    BacktestReport,
    _compute_metrics,
    load_labelled_dataset,
    save_report,
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
