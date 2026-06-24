---
title: "Implement Cross-Pair Synchrony Score for Coordinated Multi-Pair Wash Trading"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/cross_pair_engine.py` to compute burst overlap ratio and shared wallet cluster size across trading pairs. Sophisticated wash-trading campaigns rotate activity across multiple asset pairs to stay below single-pair detection thresholds — the same wallet cluster trades XLM/USDC for an hour, then switches to XLM/yXLM, then BTC/USDC, cycling back every few hours. This issue builds the statistical machinery to detect coordinated multi-pair campaigns.

## Background & Context

The current `cross_pair_engine.py` computes five features: `cross_pair_activity_count`, `synchrony_score`, `burst_overlap_ratio`, `shared_wallet_cluster_size`, and `volume_concentration`. However, the implementation is incomplete: `synchrony_score` and `burst_overlap_ratio` are computed with placeholder logic (simple counts), and `shared_wallet_cluster_size` uses a naïve pairwise intersection rather than proper graph-based community detection.

This issue replaces the placeholder implementations with statistically rigorous methods:

**Burst overlap ratio**: For two asset pairs P1 and P2, a "burst" is a 5-minute window where volume exceeds 2× the pair's rolling median. The burst overlap ratio for a wallet cluster C is the fraction of P1-bursts that co-occur with P2-bursts within a 15-minute window. High overlap (> 0.6) across ≥ 3 pairs is a strong coordination signal.

**Synchrony score**: Mutual information between the per-pair volume time series (binned into 5-minute buckets over a 24-hour window). I(X;Y) > 0.3 nats between two pairs that share ≥ 2 wallet participants is suspicious.

**Shared wallet cluster size**: Build a bipartite graph of (wallet → pair) participation and use a label propagation algorithm to find communities. Report the largest community touching each pair.

The output feeds directly into `FEATURE_NAMES` entries 33–37 (the existing cross-pair features) and improves the ML ensemble's ability to detect coordinated campaigns.

## Objectives

- [ ] Replace placeholder `synchrony_score` with mutual-information-based computation over 5-minute volume buckets
- [ ] Replace placeholder `burst_overlap_ratio` with sliding-window burst co-occurrence analysis
- [ ] Replace naïve `shared_wallet_cluster_size` with label-propagation community detection on the wallet-pair bipartite graph
- [ ] Implement `CrossPairCampaign` dataclass grouping wallet clusters, affected pairs, campaign score, and time window
- [ ] Emit `CrossPairCampaign` records for campaigns exceeding a configurable threshold
- [ ] Expose `GET /cross-pair-campaigns` in `api/main.py`
- [ ] Ensure all five cross-pair feature values in `FEATURE_NAMES` are now rigorously computed
- [ ] Write tests covering single-pair baseline, two-pair coordination, and multi-pair campaign scenarios

## Technical Requirements

### Data structures

```python
# detection/cross_pair_engine.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import numpy as np

@dataclass
class VolumeBucket:
    pair: str
    window_start: datetime
    total_volume: float
    active_wallets: set[str]

@dataclass
class CrossPairCampaign:
    campaign_id: str               # SHA-256[:8] of sorted wallet set + timestamp
    wallets: list[str]
    pairs: list[str]
    burst_overlap_ratio: float
    synchrony_score: float
    shared_wallet_cluster_size: int
    volume_concentration: float
    campaign_score: float          # 0–1 composite
    window_start: datetime
    window_end: datetime
    detected_at: datetime = field(default_factory=datetime.utcnow)
```

### Synchrony score (mutual information)

```python
def _mutual_information(
    series_a: np.ndarray,   # volume per 5-min bucket, shape (T,)
    series_b: np.ndarray,   # same shape
    bins: int = 10,
) -> float:
    """
    Compute I(A;B) in nats using a joint histogram.
    Returns 0.0 if either series has zero variance.
    Normalise by H(A) + H(B) to produce a value in [0, 1].
    """
    if series_a.std() < 1e-9 or series_b.std() < 1e-9:
        return 0.0
    # Joint histogram
    joint, _, _ = np.histogram2d(series_a, series_b, bins=bins, density=True)
    # Marginals
    p_a = joint.sum(axis=1)
    p_b = joint.sum(axis=0)
    # I(A;B) = sum p(a,b) * log(p(a,b) / (p(a)*p(b)))
    ...
```

### Burst overlap ratio

```python
def _burst_overlap_ratio(
    bursts_a: list[datetime],   # burst start times for pair A
    bursts_b: list[datetime],   # burst start times for pair B
    co_occurrence_window_s: float = 900.0,  # 15 minutes
) -> float:
    """
    For each burst in A, check if any burst in B falls within
    co_occurrence_window_s. Return fraction of A-bursts with a co-occurring B-burst.
    Uses a sorted-pointer sweep for O((|A|+|B|) log(|A|+|B|)) complexity.
    """
    if not bursts_a:
        return 0.0
    bursts_b_sorted = sorted(bursts_b)
    count = 0
    for t_a in bursts_a:
        # Binary search for bursts_b within [t_a - window, t_a + window]
        ...
    return count / len(bursts_a)
```

### Label propagation for community detection

```python
def _label_propagation(
    wallet_pair_edges: list[tuple[str, str]],  # (wallet, pair)
    max_iterations: int = 50,
) -> dict[str, int]:
    """
    Standard label propagation on a bipartite graph.
    Returns {node_id: community_id} for all nodes (wallets and pairs).
    """
    ...
```

### Main engine class

```python
class CrossPairEngine:
    def __init__(
        self,
        bucket_size_minutes: int = 5,
        burst_multiplier: float = 2.0,
        co_occurrence_window_s: float = 900.0,
        min_mutual_info: float = 0.3,
        min_burst_overlap: float = 0.6,
        min_campaign_score: float = 0.65,
        min_shared_wallets: int = 2,
    ): ...

    def ingest_trades(self, trades: list[dict]) -> None:
        """Build volume buckets and burst lists per pair."""
        ...

    def detect_campaigns(self) -> list[CrossPairCampaign]:
        """
        1. For all pair combinations sharing >= min_shared_wallets, compute
           synchrony_score and burst_overlap_ratio.
        2. Run label propagation to find communities.
        3. Emit CrossPairCampaign for each community exceeding min_campaign_score.
        """
        ...

    def get_features(self, wallet: str, asset_pair: str) -> dict[str, float]:
        """Return the five cross-pair features for feature_engineering.py."""
        ...
```

### API endpoint

```python
@router.get("/cross-pair-campaigns")
async def list_campaigns(
    min_score: float = Query(0.65, ge=0.0, le=1.0),
    limit: int = Query(50, le=200),
) -> list[CrossPairCampaignResponse]:
    ...
```

### Configuration

```
CROSS_PAIR_BUCKET_SIZE_MINUTES=5
CROSS_PAIR_BURST_MULTIPLIER=2.0
CROSS_PAIR_CO_OCCURRENCE_WINDOW_S=900
CROSS_PAIR_MIN_MUTUAL_INFO=0.3
CROSS_PAIR_MIN_BURST_OVERLAP=0.6
CROSS_PAIR_MIN_CAMPAIGN_SCORE=0.65
```

## Security Considerations

- **Quadratic pair-combination blow-up**: with N active pairs, all combinations are O(N²). Cap at `MAX_ACTIVE_PAIRS = 200` to bound this; pairs beyond the cap are ranked by volume and the lowest-volume pairs are dropped with a warning log
- **Label propagation convergence**: the algorithm can oscillate on certain graph structures. Enforce `max_iterations = 50` and detect non-convergence (label set unchanged for 3 iterations) as the termination condition
- **Asset pair string normalisation**: always normalise pairs to canonical `BASE/COUNTER` format (alphabetical if ambiguous) before using as dict keys — prevents duplicate tracking of the same pair
- **Campaign ID collision**: `SHA-256[:8]` has ~3 billion values; collision probability is negligible for typical volumes but log a warning if a collision is detected (matching campaign_id, different wallet set)
- **Mutual information edge case**: if all volume in a bucket is zero (market closed, network outage), skip that bucket from the histogram to avoid division-by-zero in the MI calculation

## Testing Requirements

- [ ] `tests/test_cross_pair_engine.py` — unit tests for all three statistical methods
- [ ] Test `_mutual_information`: two identical series → MI near 1.0; two independent uniform series → MI near 0.0; zero-variance series → 0.0
- [ ] Test `_burst_overlap_ratio`: A bursts every 10min, B bursts 5min after A → ratio = 1.0; no overlap → ratio = 0.0
- [ ] Test `_label_propagation`: three-wallet cluster sharing pair → all in same community
- [ ] Test `detect_campaigns`: two coordinated pairs produce `CrossPairCampaign` with `campaign_score > 0.65`
- [ ] Test: single pair, single wallet → no campaign emitted
- [ ] Test: `MAX_ACTIVE_PAIRS` guard drops excess pairs with warning
- [ ] Integration test: `GET /cross-pair-campaigns?min_score=0.65` returns correct schema
- [ ] Benchmark: `detect_campaigns()` with 50 pairs and 10k trades in < 3 seconds

## Documentation Requirements

- [ ] Docstrings on all public methods with parameter and return type documentation
- [ ] Add `docs/cross_pair_detection.md` explaining the rotation attack model, the three statistical methods, threshold guidance, and the `MAX_ACTIVE_PAIRS` operational limit
- [ ] Update `README.md` cross-pair features table with revised descriptions
- [ ] Update `.env.example` with six new configuration keys
- [ ] Document `CrossPairCampaign` fields in the API reference

## Definition of Done

- [ ] All three placeholder implementations replaced with rigorous statistical methods
- [ ] `CrossPairCampaign` dataclass and `detect_campaigns()` implemented
- [ ] `GET /cross-pair-campaigns` endpoint live
- [ ] All unit, integration, and benchmark tests pass
- [ ] No regressions in existing cross-pair feature tests
- [ ] `docs/cross_pair_detection.md` authored
- [ ] `.env.example` updated

## For Contributors

**Ideal contributor profile**: You have a strong background in statistics and information theory (mutual information, entropy), and have applied these concepts to time-series anomaly detection. You understand label propagation and community detection in graphs. Familiarity with NumPy vectorised operations and the Stellar SDEX trading mechanics will help. Experience building coordination-detection systems (bot networks, Sybil attacks, coordinated inauthentic behaviour) is highly relevant.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "information theory applied to fraud detection", "graph community detection", "time-series coordination analysis"
2. **Relevant experience** — systems where you detected coordinated activity across multiple dimensions; publications or projects in bot/Sybil detection
3. **Approach / initial thoughts** — your view on mutual information vs cross-correlation for the synchrony signal; any concerns about the label propagation approach vs spectral clustering
4. **Estimated time** — breakdown by method (MI, burst overlap, label prop, campaign scoring, API, tests, docs)
