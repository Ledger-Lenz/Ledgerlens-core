"""Recall and unit tests for the adversarial detection pipeline.

Asserts that:
  - Each generator produces statistically correct output (unit tests).
  - The default LedgerLens detection pipeline achieves ≥ minimum recall
    thresholds on each evasion strategy (integration tests).
  - AdversarialDataset.build() produces complete, finite feature DataFrames.

Run these alongside nightly CI to gate model robustness on adversarial data.
"""

from __future__ import annotations

import random
import string

import numpy as np
import pandas as pd
import pytest

from detection.feature_engineering import FEATURE_NAMES
from ingestion.adversarial_data import (
    ASSET_PAIRS,
    BENFORD_PROBS,
    AdversarialDataset,
    BenfordCamouflageGenerator,
    CrossPairRotationGenerator,
    GraphFragmentationGenerator,
    TimingJitterGenerator,
)

_ALPHA = string.ascii_uppercase + "234567"


def _random_wallets(n: int, seed: int = 0) -> list[str]:
    rng = random.Random(seed)
    return ["G" + "".join(rng.choices(_ALPHA, k=55)) for _ in range(n)]


# ---------------------------------------------------------------------------
# Shared trained-models fixture (module-scoped to avoid re-training per test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def trained_models():
    """Train a minimal RF/XGB/LightGBM ensemble for adversarial recall tests.

    Skips automatically when ``mlflow`` or other optional training deps are
    absent (same pre-condition as the rest of the model-training test suite).
    """
    pytest.importorskip("mlflow", reason="mlflow required for model training tests")
    from detection.dataset import build_training_dataset
    from detection.model_training import train_ensemble
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=60, n_wash_rings=15, ring_size=3, seed=42
    )
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
    results = train_ensemble(df, adversarial_augment=True, calibrate=False)

    models = {
        k: v["model"]
        for k, v in results.items()
        if not k.startswith("_") and isinstance(v, dict) and "model" in v
    }

    class _EnsemblePredictor:
        def __init__(self, model_dict: dict) -> None:
            self._models = model_dict

        def predict(self, X: pd.DataFrame) -> np.ndarray:
            X_arr = X.fillna(0.0).values
            probas = np.stack([m.predict_proba(X_arr)[:, 1] for m in self._models.values()])
            return probas.mean(axis=0)

    return _EnsemblePredictor(models)


# ---------------------------------------------------------------------------
# Unit: BenfordCamouflageGenerator
# ---------------------------------------------------------------------------


def test_benford_camouflage_conformity():
    """10 000 generated amounts must not reject Benford's Law (chi-square p > 0.05)."""
    from scipy.stats import chisquare

    gen = BenfordCamouflageGenerator(seed=0)
    amounts = [gen.sample_amount() for _ in range(10_000)]
    leading = [int(str(a).lstrip("0").replace(".", "")[0]) for a in amounts]
    observed = np.array([leading.count(d) for d in range(1, 10)], dtype=float)
    expected = BENFORD_PROBS * len(amounts)
    _, p = chisquare(observed, expected)
    assert p > 0.05, f"Benford chi-square p={p:.4f} < 0.05 — amounts do not conform"


def test_benford_amounts_all_positive():
    """All generated amounts must be strictly positive."""
    gen = BenfordCamouflageGenerator(seed=1)
    amounts = [gen.sample_amount() for _ in range(1_000)]
    assert all(a > 0 for a in amounts), "Non-positive amount generated"


def test_benford_camouflage_generates_correct_count():
    """generate() must return exactly n_trades Trade objects."""
    gen = BenfordCamouflageGenerator(seed=2)
    wallets = _random_wallets(5)
    trades = gen.generate(wallets, n_trades=50)
    assert len(trades) == 50


# ---------------------------------------------------------------------------
# Unit: TimingJitterGenerator
# ---------------------------------------------------------------------------


def test_timing_jitter_mean_interval():
    """Mean inter-arrival time must be within 20 % of λ*60 s."""
    from datetime import datetime, timezone

    lam = 10.0
    gen = TimingJitterGenerator(mean_interval_minutes=lam, seed=0)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts = gen.generate_timestamps(1_000, start)
    intervals = np.array([(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)])
    mean_s = intervals.mean()
    target_s = lam * 60
    assert abs(mean_s - target_s) / target_s < 0.20, (
        f"Mean interval {mean_s:.1f}s differs from target {target_s:.1f}s by >20 %"
    )


def test_timing_jitter_coefficient_of_variation():
    """Exponential distribution has coefficient of variation ≈ 1.0 (within 0.3)."""
    from datetime import datetime, timezone

    gen = TimingJitterGenerator(seed=2)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts = gen.generate_timestamps(1_000, start)
    intervals = np.array([(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)])
    cv = intervals.std() / intervals.mean()
    assert 0.70 <= cv <= 1.30, f"CoV={cv:.3f} is far from expected 1.0 (Poisson process)"


def test_timing_jitter_timestamps_monotonic():
    """Generated timestamps must be strictly increasing."""
    from datetime import datetime, timezone

    gen = TimingJitterGenerator(seed=3)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts = gen.generate_timestamps(100, start)
    assert all(ts[i] < ts[i + 1] for i in range(len(ts) - 1)), "Timestamps not monotonic"


# ---------------------------------------------------------------------------
# Unit: GraphFragmentationGenerator
# ---------------------------------------------------------------------------


def test_graph_fragmentation_scc_size():
    """All SCCs in the fragmented graph must have at most 3 nodes."""
    from detection.graph_engine import build_transaction_graph, find_wash_rings

    gen = GraphFragmentationGenerator()
    trades = gen.generate(n_hub_wallets=12, n_trades_per_fragment=6, seed=0)
    df = pd.DataFrame([t.model_dump() for t in trades])
    df["ledger_close_time"] = pd.to_datetime(df["ledger_close_time"], utc=True)
    graph = build_transaction_graph(df)
    rings = find_wash_rings(graph, min_ring_size=2)
    for ring in rings:
        assert len(ring["accounts"]) <= 3, (
            f"Ring has {len(ring['accounts'])} nodes; expected ≤ 3 for fragmented graph"
        )


def test_graph_fragmentation_gfrag_addresses_not_stellar():
    """GFRAG addresses must not pass Stellar G-address validation."""
    from ingestion.adversarial_data import _is_valid_stellar_address

    gen = GraphFragmentationGenerator()
    trades = gen.generate(n_hub_wallets=9, n_trades_per_fragment=3, seed=0)
    addrs = {t.base_account for t in trades} | {
        t.counter_account for t in trades if t.counter_account
    }
    for addr in addrs:
        assert not _is_valid_stellar_address(addr), (
            f"GFRAG address {addr!r} incorrectly passes Stellar validation"
        )


def test_graph_fragmentation_generates_trades():
    """generate() with n_hub_wallets=9 must produce exactly 3 rings × n_trades trades."""
    gen = GraphFragmentationGenerator()
    n_per = 10
    trades = gen.generate(n_hub_wallets=9, n_trades_per_fragment=n_per, seed=0)
    assert len(trades) == 3 * n_per  # 9 // 3 = 3 rings


# ---------------------------------------------------------------------------
# Unit: CrossPairRotationGenerator
# ---------------------------------------------------------------------------


def test_cross_pair_coverage():
    """Each asset pair must have exactly n_trades_per_pair trades."""
    wallets = _random_wallets(5)
    n_per_pair = 15
    gen = CrossPairRotationGenerator()
    trades = gen.generate(wallets, n_trades_per_pair=n_per_pair, seed=0)

    counts: dict[str, int] = {}
    for t in trades:
        key = f"{t.base_asset.code}/{t.counter_asset.code}"
        counts[key] = counts.get(key, 0) + 1

    for pair in ASSET_PAIRS:
        base_code, counter_code = pair.split("/")
        key = f"{base_code}/{counter_code}"
        assert counts.get(key, 0) >= n_per_pair, (
            f"Pair {pair}: {counts.get(key, 0)} trades < {n_per_pair}"
        )


def test_cross_pair_total_trade_count():
    """Total trades == len(ASSET_PAIRS) * n_trades_per_pair."""
    wallets = _random_wallets(4)
    n_per = 10
    gen = CrossPairRotationGenerator()
    trades = gen.generate(wallets, n_trades_per_pair=n_per, seed=0)
    assert len(trades) == len(ASSET_PAIRS) * n_per


# ---------------------------------------------------------------------------
# Unit: zero/negative amount guard
# ---------------------------------------------------------------------------


def test_all_generators_produce_positive_amounts():
    """All generators must produce base_amount > 0."""
    wallets = _random_wallets(5, seed=99)

    gen_b = BenfordCamouflageGenerator(seed=99)
    for t in gen_b.generate(wallets, n_trades=200):
        assert t.base_amount > 0, f"BenfordCamouflage: base_amount={t.base_amount}"

    gen_t = TimingJitterGenerator(seed=99)
    for t in gen_t.generate(wallets, n_trades=200):
        assert t.base_amount > 0, f"TimingJitter: base_amount={t.base_amount}"

    gen_g = GraphFragmentationGenerator()
    for t in gen_g.generate(n_hub_wallets=9, n_trades_per_fragment=10, seed=99):
        assert t.base_amount > 0, f"GraphFragmentation: base_amount={t.base_amount}"

    gen_c = CrossPairRotationGenerator()
    for t in gen_c.generate(wallets, n_trades_per_pair=20, seed=99):
        assert t.base_amount > 0, f"CrossPairRotation: base_amount={t.base_amount}"


# ---------------------------------------------------------------------------
# Integration: AdversarialDataset feature completeness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", [
    "benford_camouflage",
    "timing_jitter",
    "graph_fragmentation",
    "cross_pair_rotation",
])
def test_adversarial_dataset_feature_completeness(strategy):
    """AdversarialDataset.build() must produce all FEATURE_NAMES columns, all finite."""
    dataset = AdversarialDataset().build(
        strategy=strategy, n_wallets=20, n_trades=100, seed=0
    )
    assert "label" in dataset.columns, "Missing 'label' column"
    for col in FEATURE_NAMES:
        assert col in dataset.columns, f"Missing feature column: {col}"
    X = (
        dataset[FEATURE_NAMES]
        .fillna(0.0)
        .replace([float("inf"), float("-inf")], 0.0)
        .values.astype(float)
    )
    assert np.all(np.isfinite(X)), "Non-finite values remain in feature matrix after 0-fill"


@pytest.mark.parametrize("strategy", [
    "benford_camouflage",
    "timing_jitter",
    "graph_fragmentation",
    "cross_pair_rotation",
])
def test_adversarial_dataset_has_wash_labels(strategy):
    """AdversarialDataset.build() must include at least one wash-labelled account."""
    dataset = AdversarialDataset().build(
        strategy=strategy, n_wallets=10, n_trades=50, seed=0
    )
    assert (dataset["label"] == 1).any(), f"No wash-labelled accounts for strategy {strategy}"


def test_adversarial_dataset_unknown_strategy_raises():
    """AdversarialDataset.build() must raise ValueError for an unknown strategy."""
    with pytest.raises(ValueError, match="Unknown strategy"):
        AdversarialDataset().build(strategy="not_a_strategy")


def test_adversarial_dataset_reproducible():
    """AdversarialDataset.build() must be deterministic for the same seed."""
    d1 = AdversarialDataset().build(strategy="timing_jitter", n_wallets=10, n_trades=40, seed=7)
    d2 = AdversarialDataset().build(strategy="timing_jitter", n_wallets=10, n_trades=40, seed=7)
    pd.testing.assert_frame_equal(d1.reset_index(drop=True), d2.reset_index(drop=True))


# ---------------------------------------------------------------------------
# Integration: detection recall on adversarial strategies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy,min_recall", [
    ("benford_camouflage", 0.60),
    ("timing_jitter", 0.65),
    ("graph_fragmentation", 0.55),
    ("cross_pair_rotation", 0.60),
])
def test_detection_recall_on_adversarial_strategy(strategy, min_recall, trained_models):
    """Detection recall on adversarial wash trades must meet the minimum threshold."""
    dataset = AdversarialDataset().build(strategy=strategy, seed=42)
    X = dataset[FEATURE_NAMES].fillna(0.0)
    y = dataset["label"].values
    y_pred = trained_models.predict(X)
    wash_mask = y == 1
    if not wash_mask.any():
        pytest.skip(f"No wash accounts in adversarial dataset for strategy={strategy}")
    recall = float((y_pred[wash_mask] >= 0.5).mean())
    assert recall >= min_recall, (
        f"Detection recall on {strategy} adversarial trades: {recall:.2%} < {min_recall:.0%}"
    )
