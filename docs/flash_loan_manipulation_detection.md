# Flash-Loan Price-Manipulation Detector

## Motivation

Decentralised lending protocols (Aave, dYdX, Solend) and AMM-based price
oracles (Uniswap V3 TWAP, Curve EMA) rely on pool spot prices as data feeds.
A flash-loan-funded attacker can temporarily distort a pool's spot price — by
borrowing a large amount of capital, executing a single large swap against the
target pool, and reversing the position within the same block — to trigger a
downstream liquidation, mint/burn at an artificial rate, or extract value from
a price-dependent contract.

This detection module (`detection/oracle_manipulation_engine.py`) identifies
these single-block, capital-disproportionate round-trips, complementing the
existing wash-trade and sandwich-attack detectors.

## How It Differs from Existing Detectors

| Detector | Pattern | Scope |
|---|---|---|
| Wash trading (`amm_engine.py`) | Add-liquidity → trade-burst → remove-liquidity | Inflates volume, not price |
| Sandwich (`sandwich_engine.py`) | front-run → victim → back-run | Requires a victim's pending tx |
| **Flash-loan manipulation** (this module) | Large swap → price distortion → same-block reversal | Single-block atomic round-trip with near-zero capital exposure |

## Detection Algorithm

The detector scans each pool's trade sequence in ledger/operation order and
flags a candidate when all of the following conditions are met:

### 1. Trailing Volatility Band

For each trade, a trailing mean and standard deviation of the executed price
are computed over the preceding N trades (default 50). A trade is flagged if
its price deviates by more than `FLASH_LOAN_PRICE_DEVIATION_SIGMA` (default
4.0) standard deviations from this band.

```
deviation_sigma = |trade_price - trailing_mean| / trailing_std
```

This adapts to naturally volatile pools and avoids over-flagging assets with
high baseline price variance.

### 2. Prior Position Ratio

The account's net position (buys minus sells) before the flagged trade must be
near-zero relative to the trade's size (default threshold: 10%). This
distinguishes a flash-loan attacker (no prior exposure) from a legitimate large
trader adjusting an existing position.

```
prior_position_ratio = |net_position_before| / trade_amount
```

### 3. Pool Depth Share

The manipulating trade must move at least `FLASH_LOAN_MIN_POOL_SHARE_PCT`
(default 10%) of the pool's total liquidity depth. This prevents flagging
small trades that happen to move a thin pool's price through normal volatility.

```
pool_share = trade_amount / pool_depth
```

When pool depth data is unavailable (`pool_id_to_depth` is `None`), this check
is skipped.

### 4. Same-Block (or Tight-Window) Reversal

A same-account trade reversing the position must occur within
`FLASH_LOAN_REVERSAL_WINDOW_BLOCKS` (default 1) blocks/ledgers. The reversal
must return net exposure to within `FLASH_LOAN_REVERSAL_TOLERANCE_PCT`
(default 5%) of the pre-manipulation level.

### 5. Confidence Score

Each candidate receives a confidence score in [0, 1] combining:

- **Price deviation signal**: normalised sigma deviation (40% weight)
- **Reversal signal**: same-block = 1.0, 1-block gap = 0.9, else 0.6 (30% weight)
- **Pool share signal**: fraction of pool depth moved (30% weight)

Only candidates with confidence >= `min_confidence` (default 0.5) are returned.

## Data Structure

```python
@dataclass
class FlashLoanManipulationCandidate:
    account: str
    pool_id: str
    manipulating_trade_id: str
    price_before: float
    price_at_peak: float
    price_deviation_sigma: float
    reversal_trade_id: str | None
    reversed_within_blocks: int | None
    prior_position_ratio: float
    confidence: float
```

## Configuration

All settings are in `.env.example` and controlled via `config/settings.py`:

| Variable | Default | Description |
|---|---|---|
| `FLASH_LOAN_PRICE_DEVIATION_SIGMA` | `4.0` | Sigma threshold for price deviation |
| `FLASH_LOAN_REVERSAL_WINDOW_BLOCKS` | `1` | Max blocks for reversal detection |
| `FLASH_LOAN_REVERSAL_TOLERANCE_PCT` | `0.05` | Tolerance for net position after reversal |
| `FLASH_LOAN_MIN_POOL_SHARE_PCT` | `0.10` | Minimum pool depth fraction for manipulation |

## ML Feature Integration

The `flash_loan_manipulation_score` feature is added to the
`AMM_FEATURE_NAMES` group in `detection/feature_engineering.py`. It is computed
per account as the maximum confidence of any detected flash-loan candidate
involving that account.

When the oracle manipulation module cannot be loaded (e.g., due to missing
dependencies), the feature defaults to `0.0`.

## API

### `GET /v1/amm/flash-loan-alerts`

Returns detected flash-loan manipulation alerts, most recent first.

**Query Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `wallet` | `str` | `None` | Filter by wallet address |
| `limit` | `int` | `100` | Max results (1–1000) |
| `offset` | `int` | `0` | Pagination offset |

**Response (200):**

```json
[
  {
    "alert_type": "FLASH_LOAN_MANIPULATION",
    "wallet": "GABCDEF...",
    "asset_pair": "XLM/USDC",
    "pool_id": "a1b2c3d4...",
    "detail": {
      "manipulating_trade_id": "50",
      "price_before": 1.0,
      "price_at_peak": 1.5,
      "price_deviation_sigma": 8.2,
      "reversal_trade_id": "51",
      "reversed_within_blocks": 0,
      "prior_position_ratio": 0.0,
      "confidence": 0.95
    },
    "timestamp": "2026-07-27T10:00:00+00:00"
  }
]
```

## Usage

### Standalone Detection

```python
import pandas as pd
from detection.oracle_manipulation_engine import (
    detect_flash_loan_manipulation,
    build_pool_depth_map,
)

# trades is a DataFrame matching the Trade schema
candidates = detect_flash_loan_manipulation(
    trades=trades,
    pool_id_to_depth={"pool_id_1": 100000.0},
    price_deviation_sigma=4.0,
)
```

### Alert Persistence

```python
from detection.oracle_manipulation_engine import candidates_to_alerts
from detection.storage import save_alerts

alerts = candidates_to_alerts(candidates, asset_pair="XLM/USDC")
save_alerts(alerts)
```

## Security Considerations

- **False-positive risk**: A genuine large but organic trader must not be
  conflated with a flash-loan attacker. The `prior_position_ratio` and
  pool-depth-share requirements exist specifically to bound false-positive
  risk. If the account had meaningful prior exposure (>10% of the trade size),
  the trade is assumed to be organic.
- **Intra-block ordering**: Detection depends on accurate same-block ordering
  from the EVM/Solana adapters (`ingestion/evm_loader.py`,
  `ingestion/unspash_adapter.py`, etc.). These must preserve intra-block
  transaction order to distinguish "manipulate then reverse in the same block"
  from "two unrelated trades."
- **Cross-chain correlation**: This pattern is most relevant for EVM/Solana
  chains where flash loans are a native primitive. Stellar SDEX has no flash
  loan concept, so this detector applies primarily to cross-chain pool data.
