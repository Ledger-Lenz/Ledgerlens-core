"""Tests for the adversarial evasion detection system."""

import pandas as pd
import pytest

from ingestion.adversarial_data import ALL_STRATEGIES, generate_adversarial_dataset
from detection.adversarial_features import (
    ADVERSARIAL_FEATURE_NAMES,
    compute_adversarial_features,
    temporal_regularity_score,
    counterparty_rotation_index,
    decoy_trade_signature,
    jitter_fingerprint,
    evasion_composite_score,
)
from detection.feature_engineering import FEATURE_NAMES, build_feature_vector


# ---------------------------------------------------------------------------
# ingestion/adversarial_data.py
# ---------------------------------------------------------------------------

class TestGenerateAdversarialDataset:
    def test_returns_four_tuple(self):
        result = generate_adversarial_dataset(
            n_normal_accounts=5, n_wash_rings=2, ring_size=2, seed=0
        )
        assert len(result) == 4

    def test_wash_accounts_labelled_1(self):
        _, _, _, labels = generate_adversarial_dataset(
            n_normal_accounts=5, n_wash_rings=2, ring_size=2, seed=0
        )
        assert 1 in labels.values()

    def test_all_strategies_accepted(self):
        trades, _, _, labels = generate_adversarial_dataset(
            n_normal_accounts=5, n_wash_rings=2, ring_size=2,
            evasion_strategies=ALL_STRATEGIES, seed=1
        )
        assert not trades.empty
        assert len(labels) > 0

    def test_single_strategy(self):
        trades, _, _, labels = generate_adversarial_dataset(
            n_normal_accounts=5, n_wash_rings=2, ring_size=2,
            evasion_strategies=["benford_mimicry"], seed=2
        )
        assert not trades.empty

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            generate_adversarial_dataset(evasion_strategies=["not_a_strategy"])

    def test_reproducible_with_same_seed(self):
        t1, _, _, l1 = generate_adversarial_dataset(seed=7)
        t2, _, _, l2 = generate_adversarial_dataset(seed=7)
        assert len(t1) == len(t2)
        assert l1 == l2

    def test_trades_have_required_columns(self):
        trades, _, _, _ = generate_adversarial_dataset(
            n_normal_accounts=5, n_wash_rings=1, ring_size=2, seed=3
        )
        for col in ("base_account", "counter_account", "base_amount", "ledger_close_time"):
            assert col in trades.columns


# ---------------------------------------------------------------------------
# detection/adversarial_features.py
# ---------------------------------------------------------------------------

def _make_account_trades(n: int = 10, regular: bool = False) -> pd.DataFrame:
    now = pd.Timestamp("2026-01-01T12:00:00Z")
    rows = []
    for i in range(n):
        gap = pd.Timedelta(seconds=60 if regular else (i * 37 + 13) % 120 + 1)
        rows.append({
            "ledger_close_time": now + gap * i,
            "base_account": "A",
            "counter_account": f"CP{i}",
            "base_amount": float(10 + i * 3),
        })
    return pd.DataFrame(rows)


class TestAdversarialFeatures:
    def test_compute_adversarial_features_returns_all_keys(self):
        trades = _make_account_trades(12)
        feats = compute_adversarial_features(trades, "A")
        assert set(feats.keys()) == set(ADVERSARIAL_FEATURE_NAMES)

    def test_all_values_in_0_1(self):
        trades = _make_account_trades(12)
        feats = compute_adversarial_features(trades, "A")
        for k, v in feats.items():
            assert 0.0 <= v <= 1.0, f"{k}={v} out of range"

    def test_temporal_regularity_high_for_regular_spacing(self):
        regular = _make_account_trades(15, regular=True)
        score = temporal_regularity_score(regular, "A")
        assert score >= 0.5

    def test_counterparty_rotation_high_when_all_unique(self):
        trades = _make_account_trades(10)
        score = counterparty_rotation_index(trades, "A")
        assert score > 0.5

    def test_counterparty_rotation_low_when_one_counterparty(self):
        now = pd.Timestamp("2026-01-01T12:00:00Z")
        rows = [
            {"ledger_close_time": now + pd.Timedelta(seconds=i * 60),
             "base_account": "A", "counter_account": "B", "base_amount": 100.0}
            for i in range(10)
        ]
        trades = pd.DataFrame(rows)
        score = counterparty_rotation_index(trades, "A")
        assert score < 0.2

    def test_evasion_composite_is_weighted_average(self):
        feats = {k: 0.5 for k in ADVERSARIAL_FEATURE_NAMES if k != "evasion_composite_score"}
        result = evasion_composite_score(feats)
        assert abs(result - 0.5) < 1e-6

    def test_short_trades_return_zero(self):
        now = pd.Timestamp("2026-01-01T00:00:00Z")
        single = pd.DataFrame([{
            "ledger_close_time": now, "base_account": "A",
            "counter_account": "B", "base_amount": 100.0,
        }])
        assert temporal_regularity_score(single, "A") == 0.0
        assert jitter_fingerprint(single, "A") == 0.0
        assert decoy_trade_signature(single, "A") == 0.0


# ---------------------------------------------------------------------------
# FEATURE_NAMES integration
# ---------------------------------------------------------------------------

def test_feature_names_includes_adversarial():
    for name in ADVERSARIAL_FEATURE_NAMES:
        assert name in FEATURE_NAMES, f"{name} missing from FEATURE_NAMES"


def test_build_feature_vector_includes_adversarial_keys():
    trades, meta, events, _ = generate_adversarial_dataset(
        n_normal_accounts=5, n_wash_rings=1, ring_size=2, seed=10
    )
    account = next(
        acc for acc, lbl in
        __import__("ingestion.adversarial_data", fromlist=["generate_adversarial_dataset"])
        .__dict__  # not ideal but works; use labels from dataset
        .get("generate_adversarial_dataset", lambda **k: (None, None, None, {}))(
            n_normal_accounts=5, n_wash_rings=1, ring_size=2, seed=10
        )[3].items()
        if lbl == 0
    ) if False else list(meta.keys())[0]

    as_of = pd.Timestamp(trades["ledger_close_time"].max())
    fv = build_feature_vector(trades, account, as_of, account_metadata=meta)
    for name in ADVERSARIAL_FEATURE_NAMES:
        assert name in fv, f"{name} missing from build_feature_vector output"


# ---------------------------------------------------------------------------
# detection/robustness_eval.py (smoke test only — full eval is slow)
# ---------------------------------------------------------------------------

def test_evaluate_robustness_returns_expected_keys():
    from detection.dataset import build_training_dataset
    from detection.model_training import train_ensemble
    from detection.robustness_eval import evaluate_robustness
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=30, n_wash_rings=8, ring_size=3, seed=99
    )
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
    results = train_ensemble(df, adversarial_augment=False, calibrate=False)
    models = {k: v["model"] for k, v in results.items()}

    robustness = evaluate_robustness(models, n_trials=1, seed=99)
    assert "baseline" in robustness
    assert "all_strategies" in robustness
    assert "auc_roc" in robustness["baseline"]
    assert "delta_auc" in robustness["all_strategies"]


# ---------------------------------------------------------------------------
# CLI: eval-robustness (smoke test)
# ---------------------------------------------------------------------------

def test_eval_robustness_cli_exits_zero():
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "eval-robustness",
            "--n-trials", "1",
            "--n-normal-accounts", "30",
            "--n-wash-rings", "8",
            "--ring-size", "3",
            "--seed", "0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "AUC-ROC" in result.output
    assert "Baseline" in result.output
    assert "All strategies" in result.output
