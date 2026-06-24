---
title: "Implement Benford Baseline Calibration Against Market-Wide Stellar Trade Distributions"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary
The Benford engine compares a wallet's digit distribution against the theoretical Benford distribution, but theoretical Benford may not reflect the actual digit distribution of legitimate Stellar SDEX trading. Calibrating expected frequencies against a rolling market-wide baseline reduces false positives caused by systematic SDEX price-level biases that affect all traders equally.

## Background & Context
Benford's Law describes the digit distribution of naturally occurring datasets. Financial transaction amounts often deviate from theoretical Benford due to price quantisation, psychological round-number preferences, and minimum-lot-size effects. On SDEX, XLM/USDC trades cluster around specific price levels set by market makers, creating systematic digit-frequency biases that affect all participants.

Without a market baseline, the Benford engine flags wallets for patterns that are normal for the asset pair being traded, producing false positives. By computing an asset-pair-specific baseline from the last N days of all trades and using it as the expected distribution, conformity tests become relative to the market — isolating wallets whose patterns are anomalous compared to peers, not just theory.

## Objectives
- [ ] Build `BenfordBaselineCalibrator` that consumes historical trade snapshots and computes per-asset-pair digit-frequency baselines
- [ ] Store baselines in the feature store (SQLite `benford_baselines` table) with a timestamp and trade-count for staleness tracking
- [ ] Modify `BenfordEngine.analyse(wallet, asset_pair)` to use the asset-pair baseline when available, falling back to theoretical Benford
- [ ] Add a `cli.py benford calibrate --days 30` command to recompute baselines from the Parquet snapshot store
- [ ] Expose baseline metadata via `GET /benford/baselines` endpoint

## Technical Requirements
```python
@dataclass
class BenfordBaseline:
    asset_pair: str          # e.g., "XLM/USDC"
    digit_freqs: list[float] # 9-element observed frequency array
    trade_count: int
    computed_at: datetime
    window_days: int

class BenfordBaselineCalibrator:
    def calibrate(self, asset_pair: str, window_days: int = 30) -> BenfordBaseline: ...
    def load(self, asset_pair: str) -> Optional[BenfordBaseline]: ...
```

Baseline recomputation should run as a nightly scheduled job (cron expression: `0 2 * * *`).

## Definition of Done
- [ ] Baselines computed and stored for all active asset pairs
- [ ] False-positive rate on known-legitimate test wallets drops vs theoretical Benford baseline
- [ ] `cli.py benford calibrate` runs end-to-end without error
- [ ] Stale baseline (> 7 days old) triggers a warning log and falls back to theoretical

## For Contributors
Statistical background in empirical distribution fitting and chi-square test modifications preferred. Share your thoughts on minimum trade count thresholds for a reliable baseline.
