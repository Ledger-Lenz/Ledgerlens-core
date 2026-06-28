Adversarial Testing
===================

This document describes the adversarial data generators in
`ingestion/adversarial_data.py`, their evasion strategies, the recall
thresholds enforced in CI, and how to extend the suite with new strategies.

---

## Background

LedgerLens's synthetic data generator (`ingestion/synthetic_data.py`)
produces wash-trade patterns representative of **naive** bot behaviour:
- Fixed lot sizes → caught by Benford chi-square / MAD
- Regular timing intervals → caught by intra-minute clustering
- Simple ring structures → caught by SCC graph detection

Real wash-trade operators adapt.  The *adversarial generators* simulate four
evasion strategies that sophisticated bots use to evade these signals.

---

## Evasion Strategy Descriptions

### 1. `benford_camouflage`

**Class:** `BenfordCamouflageGenerator`

**Goal:** Bypass Benford's-Law checks by engineering trade amounts so that
their leading-digit distribution matches the expected logarithmic curve.

**Mechanism:**
1. Sample leading digit d ~ Benford(d) = log₁₀(1 + 1/d).
2. Sample order of magnitude k ~ Uniform([min_order, max_order]).
3. Draw amount uniformly within [d·10^k, (d+1)·10^k).

Despite Benford-conforming amounts the wash relationship is preserved: the
same wallet ring executes the same cyclic round-trip pattern.  Graph
features (SCC detection, ring membership) remain effective.

**Recall threshold:** ≥ 60 %

---

### 2. `timing_jitter`

**Class:** `TimingJitterGenerator`

**Goal:** Avoid periodicity / intra-minute clustering detection by using
Poisson-process inter-arrival times (exponentially distributed) instead of
regular or burst-clustered intervals.

**Mechanism:**
- Inter-arrival times drawn from Exponential(mean = λ·60 s), where
  λ = `mean_interval_minutes` (default 10 min).
- Produces a sequence of timestamps with coefficient of variation ≈ 1.0,
  which matches organic human trading rather than a periodic bot.

Graph structure is unchanged; the ring still forms an SCC that the
wash-ring detector identifies.

**Recall threshold:** ≥ 65 %

---

### 3. `graph_fragmentation`

**Class:** `GraphFragmentationGenerator`

**Goal:** Stay below the `MAX_RING_SIZE=10` threshold by splitting wash
activity into many small, isolated 3-node strongly connected components
instead of one large ring.

**Mechanism:**
- Creates `n_hub_wallets // 3` separate 3-node ring groups.
- Each group has wallets with a `GFRAG` prefix (61 chars; not valid Stellar
  addresses) and forms its own closed directed cycle (A→B→C→A).
- Rings are disconnected so no individual SCC exceeds 3 nodes.

Despite fragmentation, each 3-node SCC satisfies `min_ring_size=3` and is
detected as a wash ring with `wash_ring_membership=1`.

**Recall threshold:** ≥ 55 %

---

### 4. `cross_pair_rotation`

**Class:** `CrossPairRotationGenerator`

**Goal:** Dilute per-pair volume concentration by rotating the same wallet
ring across five asset pairs: XLM/USDC, XLM/yXLM, USDC/yUSDC, XLM/AQUA,
USDC/AQUA.

**Mechanism:**
- Each pair receives `n_trades_per_pair` wash trades in a 6-day window.
- The wallet ring structure (cyclic rotation: wallet[i] → wallet[i+1 % n])
  is identical across all pairs.

Cross-pair concentration features (`cross_pair_volume_concentration`,
`cross_pair_synchrony_score`) detect the coordinated rotation pattern.

**Recall threshold:** ≥ 60 %

---

## Recall Thresholds

Thresholds are enforced by `tests/test_adversarial_detection.py`:

| Strategy             | Min Recall | Primary Detection Signal          |
|----------------------|-----------|-----------------------------------|
| `benford_camouflage` | 60 %      | Graph SCC, round-trip frequency   |
| `timing_jitter`      | 65 %      | Graph SCC, counterparty conc.     |
| `graph_fragmentation`| 55 %      | Wash ring membership (3-node SCC) |
| `cross_pair_rotation`| 60 %      | Cross-pair synchrony, graph SCC   |

Recall is measured as the fraction of adversarial wash accounts
(`label=1`) that receive a detection score ≥ 0.5 from the trained ensemble.

---

## Nightly CI Integration

The adversarial recall tests run in **nightly CI** via
`pytest tests/test_adversarial_detection.py`.

The `trained_models` fixture (module-scoped) trains the full
RF/XGB/LightGBM ensemble on synthetic data with `adversarial_augment=True`
before any recall test runs.  Trained once per CI invocation; all four
recall tests share the same trained ensemble.

To run locally:

```bash
# Train and evaluate all four strategies
pytest tests/test_adversarial_detection.py -v

# Unit tests only (fast, no model training)
pytest tests/test_adversarial_detection.py -v -k "not test_detection_recall"

# Single strategy
pytest tests/test_adversarial_detection.py -v -k "benford_camouflage"
```

---

## Generating Adversarial Datasets via CLI

```bash
# Generate feature CSV for one strategy (labelled as wash)
python cli.py generate-adversarial --strategy benford_camouflage \
  --out-dir ./data/adversarial --n-wallets 200 --n-trades 1000 --seed 42

# Generate a clean-labelled baseline (all label=0) for FPR benchmarking
python cli.py generate-adversarial --strategy cross_pair_rotation \
  --label-clean --out-dir ./data/adversarial --seed 42
```

Output: `./data/adversarial/adversarial_{strategy}.csv` with `FEATURE_NAMES`
columns plus a `label` column.  The `--label-wash` flag (default on) marks
all adversarial accounts as `label=1`.  Pass `--label-clean` to zero all
labels — **unlabelled adversarial data must not silently enter training
datasets**.

---

## Using for Adversarial Retraining

To include adversarial data in model training:

```python
from ingestion.adversarial_data import AdversarialDataset
from detection.model_training import train_ensemble

# Build datasets for all four strategies
adv_frames = [
    AdversarialDataset().build(strategy=s, seed=None)  # seed=None for fresh patterns
    for s in ["benford_camouflage", "timing_jitter", "graph_fragmentation", "cross_pair_rotation"]
]

# Concatenate with the main training dataset before calling train_ensemble
import pandas as pd
df_augmented = pd.concat([df_main] + adv_frames, ignore_index=True)
results = train_ensemble(df_augmented, adversarial_augment=False)
```

Use `seed=None` for production retraining to generate fresh evasion patterns.
Use a fixed seed in CI for reproducibility.

---

## Adding New Evasion Strategies

1. **Implement the generator** in `ingestion/adversarial_data.py`:
   - Subclass or write a standalone class with a `generate(wallets, n_trades,
     ...) -> list[Trade]` method.
   - Expose `start_time: datetime | None = None` for reproducibility.
   - Floor all amounts at `1e-7`.

2. **Register in `AdversarialDataset.build()`**:
   - Add a branch in the `if/elif` chain.
   - Add the strategy name to `_ADV_STRATEGIES`.

3. **Add unit tests** in `tests/test_adversarial_detection.py`:
   - Test the statistical property the strategy targets (Benford p-value,
     timing CoV, SCC size, pair coverage, etc.).
   - Test that all generated amounts are positive.
   - Test that `AdversarialDataset.build()` produces complete, finite features.

4. **Add a recall test** with an appropriate threshold:
   ```python
   @pytest.mark.parametrize("strategy,min_recall", [
       ...,
       ("my_new_strategy", 0.55),
   ])
   def test_detection_recall_on_adversarial_strategy(strategy, min_recall, trained_models):
       ...
   ```

5. **Update this document** with a new strategy section and an updated
   recall threshold table.

6. **Update `CHANGELOG.md`** under `## [Unreleased]`.
