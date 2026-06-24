---
title: "Harden Sandwich Attack Detection Engine with False-Positive Reduction"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary

Harden `detection/sandwich_engine.py` to significantly reduce false positives from legitimate arbitrage activity. The current engine flags any front-run/back-run order pair around a victim trade without accounting for victim-amount thresholds, price-impact magnitude, or cross-ledger timing precision. This causes legitimate arbitrageurs — who react to genuine price discrepancies, not victims — to be incorrectly scored as sandwich attackers, degrading precision and eroding trust in LedgerLens scores.

## Background & Context

A sandwich attack on the SDEX consists of three ordered events within a tight ledger window:
1. **Front-run**: attacker buys asset X just before a known large victim order
2. **Victim order**: large buy of asset X that moves the price
3. **Back-run**: attacker sells asset X immediately after the victim order executes at the inflated price

The current `sandwich_engine.py` identifies this triplet by scanning a ledger window for (buy, victim_buy, sell) sequences by the same attacker wallet around the same asset pair. The false-positive problem has two sources:

**Source 1 — Legitimate arbitrage**: a bot may buy an asset in ledger N because it's underpriced relative to another venue, and sell in ledger N+2 for an unrelated reason. Without a minimum victim-amount threshold, any large order between two attacker orders triggers a false flag.

**Source 2 — Coincidental order proximity**: two unrelated large orders for the same asset pair naturally cluster near each other in busy market conditions. Without statistical significance testing, the engine flags random proximity as attacks.

This issue adds three hardening measures:
1. **Victim-amount minimum threshold** (`MIN_VICTIM_AMOUNT_XLM`): ignore victim orders below a configurable threshold (default 500 XLM equivalent)
2. **Price-impact score**: compute the actual price movement caused by the victim order; require a minimum price impact (default 0.3%) for a genuine sandwich signal
3. **Statistical significance test**: use a permutation test against a 24-hour baseline of same-pair order intervals to confirm that the front-run/back-run timing is non-random

## Objectives

- [ ] Add `victim_amount_filter` to `SandwichEngine.__init__` and apply it in the triplet-detection loop
- [ ] Implement `_compute_price_impact(pre_trade_price, post_trade_price)` method returning a signed price-change fraction
- [ ] Implement `_timing_significance(attacker_interval_s, pair_baseline_intervals)` using a one-sided permutation test (n=1000 bootstraps)
- [ ] Introduce `SandwichCandidate.confidence` field (0–1) combining victim amount, price impact, and timing significance
- [ ] Only emit `SandwichEvent` records where `confidence >= MIN_SANDWICH_CONFIDENCE` (default 0.7)
- [ ] Add `sandwich_confidence` as a stored field on the `SandwichEvent` schema
- [ ] Update `GET /sandwiches` API response to include `confidence`
- [ ] Write regression tests proving the three historical false-positive patterns no longer trigger
- [ ] Benchmark: processing 10,000 ledger events in < 500ms

## Technical Requirements

### Updated data structures

```python
# detection/sandwich_engine.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import numpy as np

@dataclass
class SandwichCandidate:
    attacker_wallet: str
    victim_wallet: str
    asset_pair: str
    frontrun_price: float
    victim_price: float
    backrun_price: float
    frontrun_amount: float
    victim_amount_xlm_equiv: float
    backrun_amount: float
    frontrun_ledger: int
    victim_ledger: int
    backrun_ledger: int
    attacker_interval_s: float       # time between front-run and back-run
    price_impact: float              # (victim_price - frontrun_price) / frontrun_price
    timing_p_value: float            # p-value from permutation test; lower = more significant
    confidence: float                # composite 0–1 score

@dataclass
class SandwichEvent:
    attacker_wallet: str
    victim_wallet: str
    asset_pair: str
    profit_xlm_equiv: float
    victim_amount_xlm_equiv: float
    price_impact: float
    confidence: float
    detected_at: datetime = field(default_factory=datetime.utcnow)
```

### Hardening methods

```python
class SandwichEngine:
    def __init__(
        self,
        ledger_window: int = 3,
        min_victim_amount_xlm: float = 500.0,
        min_price_impact: float = 0.003,       # 0.3%
        min_sandwich_confidence: float = 0.7,
        significance_alpha: float = 0.05,
        bootstrap_n: int = 1000,
    ): ...

    def _compute_price_impact(
        self,
        pre_trade_price: float,
        post_trade_price: float,
    ) -> float:
        """
        Returns (post - pre) / pre.
        Raises ValueError if pre_trade_price <= 0.
        """
        ...

    def _timing_significance(
        self,
        attacker_interval_s: float,
        pair_baseline_intervals: list[float],
    ) -> float:
        """
        One-sided permutation test.
        Returns p-value: fraction of bootstrap samples with interval <= attacker_interval_s.
        Lower p-value = more anomalous (tighter than baseline).
        """
        if len(pair_baseline_intervals) < 30:
            return 0.5  # insufficient baseline; conservative neutral score
        bootstrap = np.random.choice(pair_baseline_intervals, size=self.bootstrap_n)
        return float(np.mean(bootstrap <= attacker_interval_s))

    def _score_candidate(self, candidate: SandwichCandidate) -> float:
        """
        Composite confidence:
          35% price_impact_score  (sigmoid of price_impact / min_price_impact)
          35% timing_score        (1 - timing_p_value)
          30% victim_amount_score (sigmoid of victim_amount_xlm_equiv / min_victim_amount)
        """
        ...

    def detect(self, ledger_events: list[dict]) -> list[SandwichEvent]:
        """
        Process events from a ledger window.
        Filter candidates by:
          1. victim_amount_xlm_equiv >= min_victim_amount_xlm
          2. abs(price_impact) >= min_price_impact
          3. confidence >= min_sandwich_confidence
        """
        ...
```

### Baseline interval tracking

The permutation test requires a rolling 24-hour baseline of inter-order intervals for each asset pair. Store this in SQLite:

```sql
CREATE TABLE IF NOT EXISTS sandwich_pair_baselines (
    asset_pair  TEXT NOT NULL,
    interval_s  REAL NOT NULL,
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_spb_pair_time
    ON sandwich_pair_baselines(asset_pair, recorded_at DESC);
```

Prune rows older than 24 hours during each `detect()` call (batch delete, not row-by-row).

### API update

```python
@router.get("/sandwiches")
async def list_sandwiches(
    min_confidence: float = Query(0.7, ge=0.0, le=1.0),
    asset_pair: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
) -> list[SandwichEventResponse]:
    ...
```

### Configuration additions

```
SANDWICH_MIN_VICTIM_AMOUNT_XLM=500.0
SANDWICH_MIN_PRICE_IMPACT=0.003
SANDWICH_MIN_CONFIDENCE=0.7
SANDWICH_SIGNIFICANCE_ALPHA=0.05
SANDWICH_BOOTSTRAP_N=1000
```

### False-positive regression test fixtures

Three synthetic scenarios must be added to `tests/fixtures/sandwich_fp_cases.json`:
1. **Arbitrage case**: attacker interval < median, but victim amount = 100 XLM (below threshold)
2. **Random proximity case**: statistically non-significant timing (p-value = 0.45)
3. **Low price impact case**: victim price moved only 0.05% (below threshold)

Each scenario must produce `confidence < 0.7` and therefore no emitted `SandwichEvent`.

## Security Considerations

- **Bootstrap seeding**: `np.random.choice` must use a fixed seed in tests (`np.random.seed(42)`) but a random seed in production; never use a hardcoded seed in production code paths
- **Baseline poisoning**: an attacker who submits many low-interval orders can shift the baseline distribution to make their attacks look "normal". Mitigate by capping the baseline contribution per wallet to 10 intervals per 24-hour window
- **Price feed integrity**: `pre_trade_price` and `post_trade_price` must be sourced from Horizon's `price` field on the trade record, not computed from amounts (which can be manipulated via partial fills)
- **Overflow in profit calculation**: profit is computed as `backrun_proceeds - frontrun_cost`; use `Decimal` arithmetic and clamp to `[−10^12, 10^12]` XLM before storing
- **Log sanitisation**: wallet addresses and asset pair strings in log lines must be truncated to 64 chars; never log raw victim wallet with ERROR level (information leakage)

## Testing Requirements

- [ ] `tests/test_sandwich_engine.py` — unit tests for all three hardening methods
- [ ] Test: `_compute_price_impact` with `pre=1.0, post=1.005` → `0.005`; with `pre=0` → `ValueError`
- [ ] Test: `_timing_significance` with `attacker_interval=1.0` against baseline of 100 uniformly drawn from `[0.5, 10.0]` → p-value ≈ 0.05 (verify within ±0.02 tolerance)
- [ ] Test: `_timing_significance` with fewer than 30 baseline samples → returns `0.5`
- [ ] Regression test: all three false-positive fixture scenarios produce no emitted events
- [ ] Test: genuine sandwich (victim=1000 XLM, price_impact=0.8%, p-value=0.01) → event emitted with `confidence > 0.7`
- [ ] Test: baseline pruning — rows older than 24h are deleted after `detect()` call
- [ ] Benchmark: `detect()` on 10,000 events in < 500ms (`@pytest.mark.benchmark`)

## Documentation Requirements

- [ ] Update docstrings on all modified/added methods
- [ ] Add `docs/sandwich_detection.md` explaining the three-phase model, each hardening measure, threshold rationale, known limitations, and baseline-poisoning mitigation
- [ ] Update `README.md` to mention confidence field on sandwich events
- [ ] Document the `sandwich_pair_baselines` table in `docs/database_schema.md`
- [ ] Update `.env.example` with the five new configuration variables

## Definition of Done

- [ ] All three hardening measures implemented and passing tests
- [ ] Three false-positive regression fixtures added and passing
- [ ] `GET /sandwiches` returns `confidence` field
- [ ] Benchmark passes (< 500ms for 10k events)
- [ ] SQLite baseline table created via migration
- [ ] No new lint errors
- [ ] `docs/sandwich_detection.md` authored
- [ ] `.env.example` updated

## For Contributors

**Ideal contributor profile**: You have a strong statistics background and understand hypothesis testing (permutation tests, p-values). You are familiar with SDEX order-book mechanics and MEV/sandwich attack patterns in DeFi. Experience reducing false positives in production anomaly detection systems (security, fraud, or market surveillance) is particularly valuable. Comfort with NumPy and Python benchmarking tools (pytest-benchmark) is expected.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "statistical testing for anomaly detection", "MEV/sandwich attack research", "DeFi market microstructure"
2. **Relevant experience** — systems where you reduced false positives in detection pipelines; any published work on MEV or SDEX market manipulation
3. **Approach / initial thoughts** — your view on the permutation-test approach vs alternatives (e.g., KS test, z-score against baseline); any concerns about the bootstrap_n=1000 default
4. **Estimated time** — realistic estimate with breakdown by component
