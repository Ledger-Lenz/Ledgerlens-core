---
title: "Build Synthetic Adversarial Trade Data Generator for Model Hardening"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `ingestion/adversarial_data.py` with adversarial wash-trade pattern generators that simulate evasion strategies: amount camouflage (Benford-conforming amounts), timing jitter (Poisson inter-arrival times), graph fragmentation (break wash rings into overlapping 3-node SCCs), and cross-pair rotation. These generators produce labelled adversarial trade datasets used in nightly CI adversarial evaluation runs to verify that the detection models cannot be easily evaded by sophisticated wash-trade bots.

## Background & Context

LedgerLens's synthetic data generator (`ingestion/synthetic_data.py`) creates wash-trade patterns that are representative of naive bot behaviour: fixed lot sizes (easily caught by Benford), regular timing intervals (easily caught by timing features), and simple ring structures (easily caught by SCC detection). Real wash-trade operators, however, adapt: they add noise to amounts to conform to Benford's Law, add timing jitter to avoid periodicity detection, and fragment their trading rings to stay below graph-feature thresholds.

Adversarial evaluation — testing whether the detection models correctly flag wallets using evasion strategies — is essential for production confidence. Without adversarial test cases, a model can achieve high precision/recall on the naive synthetic test set while being completely blind to evasion.

The adversarial data generator produces labelled trade histories (ground truth: `label=1` for wash, `label=0` for clean) with evasion strategies applied. These datasets feed:
1. **Nightly CI adversarial tests**: `pytest tests/test_adversarial_detection.py` — assert that detection rates on each evasion strategy remain above a configurable minimum threshold (e.g., ≥60% recall on Benford-camouflaged trades).
2. **Adversarial retraining**: the adversarial generator can be included as a data augmentation source in `detection/model_training.py` to improve model robustness.

## Objectives

- [ ] Implement `BenfordCamouflageGenerator` in `ingestion/adversarial_data.py`: generates wash trades with amounts that conform to Benford's Law (leading digit distribution matches expected) while still being coordinated.
- [ ] Implement `TimingJitterGenerator`: generates wash trades with Poisson inter-arrival times (mean interval configurable, default: λ=10 minutes) instead of regular intervals.
- [ ] Implement `GraphFragmentationGenerator`: breaks a single large wash ring into multiple overlapping 3-node SCCs to stay below the `MAX_RING_SIZE` threshold for full enumeration.
- [ ] Implement `CrossPairRotationGenerator`: rotates the same wash-trade volume across multiple asset pairs (XLM/USDC, XLM/yXLM, USDC/yUSDC) so no single pair shows concentrated volume.
- [ ] Implement `AdversarialDataset` class that combines multiple evasion strategies and produces a `pd.DataFrame` with `FEATURE_NAMES` columns and a `label` column.
- [ ] Each generator exposes `generate(n_wallets, n_trades_per_wallet, seed) -> list[Trade]`.
- [ ] All generated trades are validly formatted as `ingestion/data_models.py` `Trade` objects.
- [ ] Implement `cli.py generate-adversarial` command writing adversarial datasets to CSV (same format as `generate-data`).
- [ ] Add `tests/test_adversarial_detection.py` asserting that the default LedgerLens detection pipeline achieves ≥60% recall on each evasion strategy.
- [ ] All generator code covered by tests; ≥90% branch coverage.

## Technical Requirements

### `BenfordCamouflageGenerator`

The key challenge: generate trade amounts where the wash-trade relationship is real (same wallets, coordinated timing) but the leading-digit distribution is manipulated to conform to Benford.

```python
import numpy as np
from typing import Optional, List

BENFORD_PROBS = np.array([np.log10(1 + 1/d) for d in range(1, 10)])
BENFORD_PROBS /= BENFORD_PROBS.sum()

class BenfordCamouflageGenerator:
    """
    Generates wash trades with Benford-conforming amounts.
    Strategy: sample leading digit d ~ Benford(d); then sample amount uniformly
    in [d * 10^k, (d+1) * 10^k) for a random order of magnitude k.
    """
    def __init__(self, min_order: int = 2, max_order: int = 5, seed: Optional[int] = None):
        self.rng = np.random.default_rng(seed)
        self.min_order = min_order
        self.max_order = max_order

    def sample_amount(self) -> float:
        """Sample a Benford-conforming amount."""
        d = self.rng.choice(9, p=BENFORD_PROBS) + 1    # digit 1-9
        k = self.rng.integers(self.min_order, self.max_order + 1)
        base = d * (10 ** k)
        # Add uniform noise within the leading-digit bucket
        amount = base + self.rng.uniform(0, (10 ** k) - 1)
        return round(amount, 7)

    def generate(
        self, wallets: List[str], n_trades: int, asset_pair: str = "XLM/USDC"
    ) -> List[Trade]:
        """Generate n_trades wash trades between wallets with Benford amounts."""
        trades = []
        for i in range(n_trades):
            seller = wallets[i % len(wallets)]
            buyer = wallets[(i + 1) % len(wallets)]
            amount = self.sample_amount()
            trades.append(Trade(
                base_account=seller,
                counter_account=buyer,
                base_asset_code=asset_pair.split("/")[0],
                counter_asset_code=asset_pair.split("/")[1],
                base_amount=amount,
                counter_amount=amount,
                price=1.0,
                timestamp=...,
                trade_type="orderbook",
            ))
        return trades
```

### `TimingJitterGenerator`

```python
class TimingJitterGenerator:
    """Generates wash trades with Poisson inter-arrival times."""
    def __init__(self, mean_interval_minutes: float = 10.0, seed: Optional[int] = None):
        self.lam = mean_interval_minutes
        self.rng = np.random.default_rng(seed)

    def generate_timestamps(self, n_trades: int, start_time: datetime) -> List[datetime]:
        """
        Inter-arrival times ~ Poisson(lambda) minutes.
        Returns list of n_trades timestamps starting from start_time.
        """
        intervals = self.rng.exponential(scale=self.lam * 60, size=n_trades)  # seconds
        timestamps = [start_time]
        for interval in intervals[1:]:
            timestamps.append(timestamps[-1] + timedelta(seconds=float(interval)))
        return timestamps
```

### `GraphFragmentationGenerator`

```python
class GraphFragmentationGenerator:
    """
    Breaks a large wash ring into multiple overlapping 3-node SCCs.
    Each SCC shares one 'hub' wallet with adjacent SCCs to simulate
    a network that looks fragmented but is actually coordinated.
    """
    def generate(
        self,
        n_hub_wallets: int = 10,
        n_trades_per_fragment: int = 20,
        seed: Optional[int] = None,
    ) -> List[Trade]:
        """
        Creates n_hub_wallets / 2 overlapping 3-node rings.
        Ring i uses wallets [i, i+1, i+2] (overlapping by 2).
        """
        rng = np.random.default_rng(seed)
        wallets = [f"GFRAG{i:056d}" for i in range(n_hub_wallets)]
        trades = []
        for i in range(0, n_hub_wallets - 2, 1):
            ring = [wallets[i], wallets[i+1], wallets[i+2]]
            for j in range(n_trades_per_fragment):
                seller = ring[j % 3]
                buyer = ring[(j + 1) % 3]
                trades.append(Trade(
                    base_account=seller, counter_account=buyer,
                    base_amount=float(rng.uniform(100, 10000)),
                    ...
                ))
        return trades
```

### `CrossPairRotationGenerator`

```python
ASSET_PAIRS = ["XLM/USDC", "XLM/yXLM", "USDC/yUSDC", "XLM/AQUA", "USDC/AQUA"]

class CrossPairRotationGenerator:
    """Rotates wash volume across multiple asset pairs per time window."""
    def generate(
        self,
        wallets: List[str],
        n_trades_per_pair: int = 30,
        pairs: List[str] = ASSET_PAIRS,
        seed: Optional[int] = None,
    ) -> List[Trade]:
        rng = np.random.default_rng(seed)
        trades = []
        for pair in pairs:
            for i in range(n_trades_per_pair):
                seller = wallets[i % len(wallets)]
                buyer = wallets[(i + 1) % len(wallets)]
                trades.append(Trade(base_account=seller, counter_account=buyer,
                                    base_asset_code=pair.split("/")[0],
                                    counter_asset_code=pair.split("/")[1],
                                    base_amount=float(rng.uniform(50, 5000)),
                                    ...))
        return trades
```

### `AdversarialDataset`

```python
class AdversarialDataset:
    def build(
        self,
        strategy: Literal["benford_camouflage", "timing_jitter", "graph_fragmentation", "cross_pair_rotation"],
        n_wallets: int = 50,
        n_trades: int = 200,
        seed: int = 42,
    ) -> pd.DataFrame:
        """
        Generate labelled feature dataset for a specific evasion strategy.
        Returns DataFrame with FEATURE_NAMES columns + 'label' column (all 1 for adversarial).
        """
        ...
```

### Adversarial detection tests (`tests/test_adversarial_detection.py`)

```python
@pytest.mark.parametrize("strategy,min_recall", [
    ("benford_camouflage", 0.60),
    ("timing_jitter", 0.65),
    ("graph_fragmentation", 0.55),
    ("cross_pair_rotation", 0.60),
])
def test_detection_recall_on_adversarial_strategy(strategy, min_recall, trained_models):
    dataset = AdversarialDataset().build(strategy=strategy, seed=42)
    X = dataset[FEATURE_NAMES]
    y = dataset["label"]
    y_pred = trained_models.predict(X)
    recall = (y_pred[y == 1] >= 0.5).mean()
    assert recall >= min_recall, (
        f"Detection recall on {strategy} adversarial trades: {recall:.2%} < {min_recall:.0%}"
    )
```

## Security Considerations

- Adversarial generators must use deterministic seeds in CI tests to ensure reproducibility. Production adversarial generation (for retraining) uses `seed=None` for fresh patterns.
- The `generate-adversarial` CLI command must include a `--label-wash/--label-clean` flag to clearly mark generated trades. Unlabelled adversarial data must not silently enter training datasets.
- Benford-conforming amount generation must not produce amounts of zero or negative values. Add a floor: `max(amount, 1e-7)`.
- Graph fragmentation generator produces wallets with `GFRAG` prefix — ensure these are clearly synthetic and cannot conflict with real Stellar G-addresses (they won't, as real addresses are base32-encoded with different entropy, but add a check: raise if any generated address passes Stellar G-address validation).

## Testing Requirements

- **Unit — `BenfordCamouflageGenerator` conformity**: generate 10,000 amounts; compute Benford chi-square p-value; assert p-value > 0.05 (generated amounts should not reject Benford null).
- **Unit — `TimingJitterGenerator` distribution**: generate 1,000 inter-arrival times; assert mean ≈ λ*60s (within 20%); assert Poisson fit (coefficient of variation ≈ 1.0).
- **Unit — `GraphFragmentationGenerator` SCC size**: run graph_engine on generated trades; assert no SCC has more than 3 nodes (all rings are fragmented).
- **Unit — `CrossPairRotationGenerator` pair coverage**: assert all 5 pairs have at least `n_trades_per_pair` trades.
- **Unit — zero/negative amount guard**: assert all generated amounts are > 0.
- **Integration — adversarial dataset feature computation**: build adversarial dataset; assert all `FEATURE_NAMES` columns are present and finite.
- **Integration — detection recall** (adversarial tests): run on pre-trained models; assert recall thresholds met for each strategy.

## Documentation Requirements

- Docstrings on all generator classes and `AdversarialDataset.build()`.
- New file `docs/adversarial_testing.md` covering: evasion strategy descriptions, recall thresholds, nightly CI integration, and how to add new evasion strategies.
- Update `README.md` adversarial robustness section.
- Add `generate-adversarial` to CLI Reference table in README.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] All four generators implemented in `ingestion/adversarial_data.py`.
- [ ] `AdversarialDataset.build()` produces valid feature DataFrames for all four strategies.
- [ ] `cli.py generate-adversarial` command operational.
- [ ] `tests/test_adversarial_detection.py` parameterised recall tests written.
- [ ] All unit tests pass; ≥90% branch coverage on `adversarial_data.py`.
- [ ] Adversarial recall tests pass (with pre-trained models from `cli.py train`).
- [ ] `docs/adversarial_testing.md` written.
- [ ] `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience designing adversarial evaluation frameworks for ML models — generating evasion test cases that probe specific model weaknesses. You understand Benford's Law and how wash-trade bots can be designed to evade Benford-based detection. Familiarity with LedgerLens's feature engineering schema (Benford, timing, graph, cross-pair features) is essential for building evasion strategies that specifically target each feature group. Experience with generative data synthesis for fraud detection or security testing is highly valued.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., adversarial ML, fraud detection, data synthesis, blockchain analytics).
2. **Relevant experience**: adversarial evaluation frameworks, evasion strategy generators, or red-teaming ML systems you have built.
3. **Approach / thoughts**: beyond the four strategies listed, what additional evasion technique would you add first, and why? How would you handle the detection recall threshold calibration — what is the right minimum for a "hardened" model?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
