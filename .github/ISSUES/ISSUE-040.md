---
title: "Implement AMM Pool Liquidity Concentration Anomaly Detection"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/amm_engine.py` to detect wash trading in Stellar AMM pools by identifying abnormal liquidity add/remove patterns that bookend fake trade volume. The core signal is a wallet (or coordinated cluster) that adds liquidity, executes a concentrated burst of self-dealing trades inflating pool volume, then immediately removes liquidity — a cycle that leaves no net economic exposure but produces fraudulent volume figures consumed by DEX aggregators and ranking services.

## Background & Context

Stellar AMMs (Automated Market Makers) track liquidity via pool shares. Unlike order-book wash trading, AMM-based manipulation is structurally different: an attacker must hold pool shares to benefit from fee capture, and the add→trade→remove lifecycle creates a distinct temporal fingerprint. Because AMM volume feeds directly into 24-hour volume rankings on aggregators (e.g., StellarExpert, Lobstr), inflating this metric is a high-value attack for token issuers seeking organic-looking traction.

Current LedgerLens graph and Benford engines operate on order-book trades. `ingestion/amm_loader.py` already ingests `liquidity_pool_deposit` and `liquidity_pool_withdraw` Horizon operations, but `detection/amm_engine.py` has no anomaly scoring logic. This issue closes that gap.

The three-phase attack pattern:
1. **Deposit phase** — wallet adds liquidity via `liquidity_pool_deposit`, obtaining pool shares
2. **Trade phase** — wallet (or linked wallets) execute `manage_buy_offer` / `manage_sell_offer` against the pool, generating volume with no genuine counterparty
3. **Withdraw phase** — wallet redeems pool shares within a short window (< configurable threshold, default 4 hours) via `liquidity_pool_withdraw`

Key numeric signals:
- **Liquidity tenure** — time between deposit and withdraw (shorter = more suspicious)
- **Volume-to-liquidity ratio** — trades executed during tenure / liquidity held (> 10x is anomalous)
- **Deposit/withdraw timing symmetry** — deposit and withdraw amounts that are near-identical indicate no fee earnings from genuine LPs
- **Counterparty concentration** — fraction of trades during tenure with a single counterparty

This engine should emit an `AMMPoolAnomaly` record per wallet/pool combination and contribute two new features to `feature_engineering.py`: `amm_tenure_ratio` and `amm_volume_concentration`.

## Objectives

- [ ] Implement `AMMSessionTracker` class in `detection/amm_engine.py` that reconstructs add/trade/remove sessions from AMM operation records
- [ ] Compute `liquidity_tenure_seconds`, `volume_to_liquidity_ratio`, `deposit_withdraw_symmetry`, and `counterparty_concentration` per session
- [ ] Emit `AMMPoolAnomaly` dataclass records for sessions exceeding configurable thresholds
- [ ] Add two new ML features (`amm_tenure_ratio`, `amm_volume_concentration`) to `detection/feature_engineering.py` and `FEATURE_NAMES`
- [ ] Expose detected AMM anomalies via `GET /amm-anomalies` in `api/main.py`
- [ ] Add `amm_anomaly_count` and `amm_max_volume_concentration` to the `RiskScore` metadata blob
- [ ] Write unit tests covering normal LP behaviour, wash-trade sessions, and edge cases (no trades during tenure, multiple concurrent sessions)
- [ ] Update `ingestion/amm_loader.py` to pass pool operation timestamps with microsecond precision

## Technical Requirements

### Data structures

```python
# detection/amm_engine.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class AMMSession:
    wallet: str
    pool_id: str
    deposit_time: datetime
    withdraw_time: Optional[datetime]
    deposited_amount_a: float
    deposited_amount_b: float
    withdrawn_amount_a: float
    withdrawn_amount_b: float
    trades_during_tenure: list[dict]  # raw trade records

    @property
    def tenure_seconds(self) -> float:
        if self.withdraw_time is None:
            return float("inf")
        return (self.withdraw_time - self.deposit_time).total_seconds()

    @property
    def volume_to_liquidity_ratio(self) -> float:
        liquidity = max(self.deposited_amount_a + self.deposited_amount_b, 1e-9)
        volume = sum(t["base_amount"] for t in self.trades_during_tenure)
        return volume / liquidity

    @property
    def deposit_withdraw_symmetry(self) -> float:
        """0.0 = asymmetric (genuine LP), 1.0 = perfectly symmetric (suspicious)."""
        delta_a = abs(self.deposited_amount_a - self.withdrawn_amount_a)
        delta_b = abs(self.deposited_amount_b - self.withdrawn_amount_b)
        norm = max(self.deposited_amount_a + self.deposited_amount_b, 1e-9)
        return 1.0 - min((delta_a + delta_b) / norm, 1.0)


@dataclass
class AMMPoolAnomaly:
    wallet: str
    pool_id: str
    session_start: datetime
    tenure_seconds: float
    volume_to_liquidity_ratio: float
    deposit_withdraw_symmetry: float
    counterparty_concentration: float
    anomaly_score: float          # 0–1 composite
    detected_at: datetime = field(default_factory=datetime.utcnow)
```

### Core engine interface

```python
class AMMEngine:
    def __init__(
        self,
        max_tenure_seconds: float = 14_400,   # 4 hours
        min_volume_ratio: float = 5.0,
        min_symmetry: float = 0.85,
        min_counterparty_concentration: float = 0.7,
    ): ...

    def ingest_operations(
        self,
        operations: list[dict],   # from amm_loader.py
        trades: list[dict],       # from horizon_streamer / historical_loader
    ) -> list[AMMPoolAnomaly]:
        """
        Build sessions, score them, return anomalies above threshold.
        Must be idempotent — calling twice with the same input must not
        produce duplicate AMMPoolAnomaly records.
        """
        ...

    def get_features(self, wallet: str) -> dict[str, float]:
        """
        Return {'amm_tenure_ratio': float, 'amm_volume_concentration': float}
        for injection into feature_engineering.py.
        """
        ...
```

### Feature integration

```python
# detection/feature_engineering.py  (additions)
FEATURE_NAMES = [
    # ... existing 35 features ...
    "amm_tenure_ratio",            # Feature 36
    "amm_volume_concentration",    # Feature 37
]

def _compute_amm_features(wallet: str, amm_engine: AMMEngine) -> dict:
    feats = amm_engine.get_features(wallet)
    return {
        "amm_tenure_ratio": feats.get("amm_tenure_ratio", 0.0),
        "amm_volume_concentration": feats.get("amm_volume_concentration", 0.0),
    }
```

### API endpoint

```python
# api/main.py
@router.get("/amm-anomalies")
async def list_amm_anomalies(
    min_score: float = Query(0.5, ge=0.0, le=1.0),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
) -> list[AMMPoolAnomalyResponse]:
    """Return AMM anomalies ordered by anomaly_score DESC."""
    ...
```

### Configuration (`.env.example` additions)

```
AMM_MAX_TENURE_SECONDS=14400
AMM_MIN_VOLUME_RATIO=5.0
AMM_MIN_SYMMETRY=0.85
AMM_MIN_COUNTERPARTY_CONCENTRATION=0.7
```

### Session reconstruction algorithm

Sessions are keyed on `(wallet, pool_id)`. The algorithm must handle:
- Multiple concurrent deposits to the same pool by the same wallet (stack by deposit time)
- Partial withdrawals (reduce session share proportionally)
- Sessions that span ledger-fetch boundaries (persist open sessions to SQLite `amm_open_sessions` table)
- Operations arriving out-of-order due to Horizon pagination — sort by `paging_token` before processing

## Security Considerations

- **SSRF via pool_id**: pool IDs from Horizon are opaque hashes — validate they match the regex `[0-9a-f]{64}` before use in any DB query
- **Integer overflow**: AMM amounts on Stellar are `i64` stroops; use Python `Decimal` with `ROUND_HALF_EVEN` for all arithmetic to avoid floating-point precision loss on large pools
- **Denial of service**: a wallet with pathological session history (10k+ operations) must not block the event loop; enforce `MAX_SESSIONS_PER_WALLET = 1000` with oldest-first eviction
- **Score manipulation**: the `anomaly_score` composite must be monotone in all four sub-signals — verify this property in a unit test to prevent adversarial partial-signal gaming
- **Log injection**: wallet addresses and pool IDs included in log messages must be sanitised (strip newlines, truncate to 100 chars)

## Testing Requirements

- [ ] `tests/test_amm_engine.py` — unit tests for `AMMSession` property calculations with hand-computed expected values
- [ ] Test: normal LP session (tenure 72h, low volume ratio) → `anomaly_score < 0.3`
- [ ] Test: classic wash session (tenure 30min, volume ratio 20x, symmetry 0.98) → `anomaly_score > 0.8`
- [ ] Test: partial withdrawal mid-session — session remains open, score is recomputed on final withdrawal
- [ ] Test: idempotency — `ingest_operations` called twice with same data produces no duplicate anomalies
- [ ] Test: `get_features` returns `0.0` for a wallet with no AMM history (cold-start safe)
- [ ] Integration test in `tests/test_api.py`: `GET /amm-anomalies?min_score=0.5` returns correct shape
- [ ] Benchmark: `ingest_operations` with 50,000 operations completes in < 2 seconds on a single core (add `@pytest.mark.benchmark`)

## Documentation Requirements

- [ ] Docstrings on all public classes and methods following Google style
- [ ] Update `README.md` feature table to include the two new AMM features
- [ ] Add `docs/amm_detection.md` explaining the three-phase attack model, each sub-signal, threshold rationale, and known limitations (legitimate flash-LP strategies that may trigger false positives)
- [ ] Inline comments on the session-reconstruction loop explaining the ordering invariant
- [ ] Update `.env.example` with the four new configuration variables and their default rationale

## Definition of Done

- [ ] `detection/amm_engine.py` implements `AMMSession`, `AMMPoolAnomaly`, and `AMMEngine` with all methods above
- [ ] `detection/feature_engineering.py` exports `amm_tenure_ratio` and `amm_volume_concentration` in `FEATURE_NAMES`
- [ ] `api/main.py` exposes `GET /amm-anomalies` with correct pagination and filtering
- [ ] All unit and integration tests pass (`pytest tests/test_amm_engine.py tests/test_api.py`)
- [ ] Benchmark passes (< 2s for 50k operations)
- [ ] No new Ruff/flake8 lint errors
- [ ] `docs/amm_detection.md` exists and covers the three-phase pattern
- [ ] `.env.example` updated with AMM config variables
- [ ] PR description calls out any `RiskScore` schema changes for cross-repo sync

## For Contributors

**Ideal contributor profile**: You have experience building event-sourcing or session-reconstruction systems (think: reconstructing user sessions from clickstream logs), preferably in a financial or blockchain context. You understand AMM mechanics (constant-product or Stellar's AMM variant), know how Stellar Horizon operation records are structured, and are comfortable writing numerically precise Python. Familiarity with `pytest-benchmark` and Pydantic v2 is a plus.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "AMM/DeFi protocol internals", "Python event-sourcing systems", "Stellar Horizon data pipeline"
2. **Relevant experience** — specific projects or work where you built session-reconstruction, AMM analytics, or fraud detection pipelines
3. **Approach / initial thoughts** — how you would tackle the session-reconstruction algorithm for out-of-order operations; any concerns about the proposed data model
4. **Estimated time** — realistic calendar estimate broken down by phase (design, implementation, tests, docs)
