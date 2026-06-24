---
title: "Implement Byzantine-Fault-Tolerant Federated Aggregation Server Using Krum/Multi-Krum"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/federated/server.py` to replace plain FedAvg aggregation with Krum/Multi-Krum, a Byzantine-fault-tolerant aggregation rule. Krum scores each client's gradient update by its squared Euclidean distance to the `n-f-2` closest peers, excluding the `f` most outlying updates (likely poisoning attacks). Support configurable Byzantine tolerance `f` (default: `floor(n/3)`) and optionally return a Multi-Krum aggregate over the top-`m` scoring clients. This hardens the federated learning pipeline against gradient poisoning from malicious or compromised federation participants.

## Background & Context

LedgerLens's Flower-based federated learning server currently uses FedAvg to aggregate gradient updates from client nodes (institutional participants). FedAvg is optimal when all clients are honest, but it is trivially broken by Byzantine clients: a single malicious client can submit an arbitrarily scaled gradient that shifts the global model toward misclassifying wash-trading patterns as legitimate.

Krum (Blanchard et al., 2017) is the canonical Byzantine-resilient aggregation rule for federated learning. Given `n` clients with `f` potential Byzantine actors, Krum selects the single client gradient `g_i` that minimises the sum of squared distances to its `n-f-2` nearest neighbours. This score is robust as long as `2f+2 < n`. Multi-Krum extends this by averaging the top-`m` scoring gradients instead of selecting just one, offering a bias-variance tradeoff between Krum (lower bias, higher variance) and FedAvg (lower variance, higher bias).

The Flower server's aggregation strategy is configured via `flwr.server.strategy.Strategy`. This issue replaces the default `flwr.server.strategy.FedAvg` with a custom `KrumStrategy` that extends `flwr.server.strategy.FedAvg` and overrides `aggregate_fit`.

Key concerns:
- **Correctness**: Krum must select the same client as a reference Python implementation for any fixed set of gradient vectors.
- **Performance**: distance computation for `n` clients with gradient vectors of dimension `D` must complete in O(n² × D). For n=100, D=100K parameters, this is ~10⁹ operations — profile and optimise if needed.
- **Configurability**: `f` must be validated at startup: `2f+2 < n` must hold given the expected minimum number of participating clients.

## Objectives

- [ ] Implement `KrumAggregator` class in `detection/federated/krum.py` with methods `krum_scores(gradients)` and `select(gradients, m=1)`.
- [ ] `krum_scores(gradients)` computes the Krum score for each gradient vector: `score_i = sum of squared distances to n-f-2 nearest neighbours`.
- [ ] `select(gradients, m=1)` returns the indices of the `m` lowest-scoring (most central) gradient vectors; `m=1` is standard Krum, `m>1` is Multi-Krum.
- [ ] Implement `KrumStrategy` in `detection/federated/server.py` extending `flwr.server.strategy.FedAvg`, overriding `aggregate_fit` to use `KrumAggregator`.
- [ ] `KrumStrategy.__init__` validates `2f+2 < min_clients` at construction; raises `ValueError` if not.
- [ ] Support `multi_krum_m: Optional[int]` parameter: if `None`, use standard Krum (m=1); if set, use Multi-Krum averaging over top-m selected updates.
- [ ] Log at INFO: which client indices were selected; which were excluded; their Krum scores.
- [ ] Implement `GET /admin/fl/aggregation` endpoint returning last round's Krum scores, selected indices, and excluded indices.
- [ ] Write `fl_aggregation_log` SQLite table recording per-round aggregation decisions.
- [ ] All new code covered by tests with ≥90% branch coverage.

## Technical Requirements

### `KrumAggregator` (`detection/federated/krum.py`)

```python
import numpy as np
from typing import List

class KrumAggregator:
    def __init__(self, f: int):
        """
        f: number of Byzantine clients to tolerate.
        Krum is valid when 2f+2 < n (enforced at selection time).
        """
        self.f = f

    def krum_scores(self, gradients: List[np.ndarray]) -> np.ndarray:
        """
        Compute Krum score for each gradient vector.
        score_i = sum of squared L2 distances to the (n - f - 2) nearest neighbours.
        
        Args:
            gradients: list of n flattened gradient vectors, each shape (D,)
        Returns:
            scores: shape (n,), lower is more central / trustworthy
        """
        n = len(gradients)
        assert 2 * self.f + 2 < n, f"Krum requires 2f+2 < n, got f={self.f}, n={n}"
        neighbours_to_sum = n - self.f - 2
        G = np.stack(gradients)             # (n, D)
        # Pairwise squared distances: (n, n)
        dists = np.sum((G[:, None, :] - G[None, :, :]) ** 2, axis=-1)
        scores = np.zeros(n)
        for i in range(n):
            row = np.sort(dists[i])         # sort distances from i to all others
            scores[i] = row[1:neighbours_to_sum + 1].sum()  # exclude self (row[0]=0)
        return scores

    def select(
        self,
        gradients: List[np.ndarray],
        m: int = 1,
    ) -> tuple[List[int], List[int], np.ndarray]:
        """
        Select m most central gradients via Krum.
        Returns (selected_indices, excluded_indices, scores).
        """
        scores = self.krum_scores(gradients)
        ranked = np.argsort(scores)
        selected = ranked[:m].tolist()
        excluded = ranked[m:].tolist()
        return selected, excluded, scores
```

### `KrumStrategy` (`detection/federated/server.py`)

```python
import flwr as fl
from flwr.common import FitRes, Parameters, parameters_to_ndarrays, ndarrays_to_parameters
from flwr.server.client_proxy import ClientProxy

class KrumStrategy(fl.server.strategy.FedAvg):
    def __init__(
        self,
        f: int,
        multi_krum_m: Optional[int] = None,
        min_fit_clients: int = 3,
        **kwargs,
    ):
        super().__init__(min_fit_clients=min_fit_clients, **kwargs)
        if 2 * f + 2 >= min_fit_clients:
            raise ValueError(
                f"Byzantine tolerance f={f} invalid: need 2f+2 < min_fit_clients={min_fit_clients}"
            )
        self.aggregator = KrumAggregator(f=f)
        self.m = multi_krum_m or 1

    def aggregate_fit(
        self,
        server_round: int,
        results: list[tuple[ClientProxy, FitRes]],
        failures: list,
    ) -> tuple[Optional[Parameters], dict]:
        if not results:
            return None, {}
        gradients = [parameters_to_ndarrays(r.parameters) for _, r in results]
        flat = [np.concatenate([g.flatten() for g in grads]) for grads in gradients]
        selected, excluded, scores = self.aggregator.select(flat, m=self.m)
        
        logger.info(
            "Krum round %d: selected=%s excluded=%s scores=%s",
            server_round, selected, excluded, scores.tolist()
        )
        self._log_aggregation(server_round, selected, excluded, scores)
        
        selected_params = [gradients[i] for i in selected]
        # Average selected (Multi-Krum) or use single (Krum)
        aggregated = [
            np.mean([p[layer] for p in selected_params], axis=0)
            for layer in range(len(selected_params[0]))
        ]
        return ndarrays_to_parameters(aggregated), {}
```

### SQLite aggregation log

```sql
CREATE TABLE IF NOT EXISTS fl_aggregation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    n_clients       INTEGER NOT NULL,
    f_tolerance     INTEGER NOT NULL,
    m_selected      INTEGER NOT NULL,
    selected_indices TEXT NOT NULL,   -- JSON array
    excluded_indices TEXT NOT NULL,   -- JSON array
    krum_scores     TEXT NOT NULL,    -- JSON array of floats
    recorded_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### API endpoint

```python
@router.get("/admin/fl/aggregation", response_model=AggregationStatus)
async def fl_aggregation_status(rounds: int = Query(10, le=100), ...):
    """Return last N rounds of Krum aggregation decisions."""
    ...

class AggregationStatus(BaseModel):
    rounds: List[AggregationRound]

class AggregationRound(BaseModel):
    round_number: int
    n_clients: int
    f_tolerance: int
    m_selected: int
    selected_indices: List[int]
    excluded_indices: List[int]
    krum_scores: List[float]
    recorded_at: datetime
```

### Configuration

```
FL_BYZANTINE_F=0            # Default: floor(n/3) computed at runtime if 0
FL_MULTI_KRUM_M=1           # 1 = standard Krum; >1 = Multi-Krum
FL_MIN_FIT_CLIENTS=3        # Minimum clients per round; validates 2f+2 < this
```

### Performance requirement

For `n=50` clients with gradient vectors of dimension `D=50,000`, `krum_scores()` must complete in <5 seconds on a single CPU core. Use `numpy` vectorised operations (avoid Python loops over `D`). If the vectorised pairwise distance computation exceeds available RAM for large `n`, implement a chunked fallback.

## Security Considerations

- **Krum score logging**: log only Krum scores (scalars) and client indices, never gradient vectors. Gradient vectors could be used to reconstruct client training data (privacy inversion attack).
- **f parameter validation**: `f = floor(n/3)` assumes at most 1/3 of clients are Byzantine. If `n` is small (n<6), `f=1` may not be achievable while satisfying `2f+2 < n` — the constructor must raise `ValueError` with a clear message rather than silently selecting `f=0`.
- **Client identity**: Flower client proxies have opaque IDs. Log client IDs in aggregation decisions so security incidents (a specific client consistently excluded) can be investigated.
- **Gradient poisoning detection**: beyond Krum exclusion, emit a `WARNING` log when the same client index is excluded in >50% of consecutive rounds — this may indicate a persistent Byzantine actor rather than a transient data quality issue.
- **Multi-Krum m selection**: `m` must satisfy `m <= n - f`; validate at construction.

## Testing Requirements

- **Unit — `krum_scores` correctness**: construct 5 gradient vectors where one is a clear outlier (scaled by 100x); assert the outlier has the highest Krum score.
- **Unit — `krum_scores` all identical**: all scores should be equal when all gradients are identical.
- **Unit — `select` m=1**: assert the single returned index corresponds to the minimum Krum score.
- **Unit — `select` Multi-Krum m=3**: assert 3 indices returned; assert excluded indices are the remaining.
- **Unit — `KrumStrategy` constructor validation**: assert `ValueError` when `2f+2 >= min_fit_clients`.
- **Unit — `aggregate_fit` integration**: construct mock Flower `FitRes` objects; assert aggregated parameters are the mean of only the selected client gradients.
- **Unit — performance**: 50 clients × 50K-dim gradients; assert `krum_scores` completes in <5s.
- **Unit — persistent exclusion warning**: mock a client excluded in 6/10 rounds; assert `WARNING` log emitted.
- **Integration — `GET /admin/fl/aggregation`**: run 3 mock rounds; assert response contains 3 entries with correct `selected_indices`.

## Documentation Requirements

- Docstrings on `KrumAggregator`, `krum_scores`, `select`, and `KrumStrategy`.
- New file `docs/byzantine_resilience.md` covering: Krum algorithm explanation, `f` parameter guidance, Multi-Krum tradeoffs, and the aggregation log schema.
- Update `README.md` ML/FL section mentioning Byzantine fault tolerance.
- Document `FL_BYZANTINE_F`, `FL_MULTI_KRUM_M` in `.env.example`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `KrumAggregator` implemented with correct `krum_scores` and `select` methods.
- [ ] `KrumStrategy` overrides `aggregate_fit` in Flower server and uses `KrumAggregator`.
- [ ] Constructor validates `2f+2 < min_fit_clients`.
- [ ] Multi-Krum (`m>1`) averages selected gradients correctly.
- [ ] `fl_aggregation_log` SQLite table populated per round.
- [ ] `GET /admin/fl/aggregation` endpoint live and admin-key gated.
- [ ] Persistent exclusion warning emitted after >50% exclusion rate.
- [ ] All unit and integration tests pass; ≥90% branch coverage on `krum.py` and `KrumStrategy`.
- [ ] Performance test: 50 clients × 50K-dim passes in <5s.
- [ ] `docs/byzantine_resilience.md` written.
- [ ] `.env.example` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have a solid grounding in Byzantine fault-tolerant distributed systems, ideally with hands-on experience implementing or using Krum, Multi-Krum, Median, or Trimmed-Mean aggregation rules in federated learning. You are comfortable with the Flower (flwr) strategy API and `numpy` vectorised computations. Familiarity with gradient poisoning attacks and the theoretical guarantees of Krum under `2f+2 < n` is essential for implementing this correctly.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., federated learning, Byzantine fault tolerance, distributed ML, security).
2. **Relevant experience**: Krum/robust aggregation implementations or federated learning deployments you have shipped; any relevant research background.
3. **Approach / thoughts**: how you would handle the edge case where `n` drops below `2f+2` mid-round (e.g., clients drop out after round start)? Would you fall back to FedAvg or abort the round?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
