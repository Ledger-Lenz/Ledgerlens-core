# AMM Wash-Trading Detection

## Three-Phase Attack Pattern

Stellar AMM wash trading follows a distinct lifecycle that differs from
order-book manipulation:

1. **Deposit phase** — The attacker adds liquidity via `liquidity_pool_deposit`,
   obtaining pool shares.
2. **Trade phase** — The attacker (or linked wallets) executes
   `manage_buy_offer` / `manage_sell_offer` against the pool, generating
   volume with no genuine counterparty.
3. **Withdraw phase** — The attacker redeems pool shares within a short window
   (< 4 hours by default) via `liquidity_pool_withdraw`.

This cycle leaves no net economic exposure but inflates 24-hour volume
rankings on aggregators.

## Sub-Signals

### Liquidity Tenure (`tenure_seconds`)
Time between deposit and withdraw. Shorter tenure is more suspicious —
genuine LPs hold positions for hours to days, while wash traders aim to
minimize impermanent loss exposure.

- Default threshold: < 14,400 seconds (4 hours)

### Volume-to-Liquidity Ratio (`volume_to_liquidity_ratio`)
Total trade volume executed during the session divided by the liquidity
deposited. Ratios > 5x are unusual for genuine LP activity and indicate
the deposit exists purely to enable high-volume trading.

- Default threshold: > 5.0

### Deposit/Withdraw Symmetry (`deposit_withdraw_symmetry`)
Measures how closely the withdrawn amounts match the deposited amounts
(0.0 = asymmetric, 1.0 = perfectly symmetric). Genuine LPs accumulate
fees and impermanent loss, producing asymmetric withdrawals. Wash traders
who immediately withdraw show near-perfect symmetry.

- Default threshold: > 0.85

### Counterparty Concentration (`counterparty_concentration`)
Fraction of trades during the session executed against a single
counterparty. High concentration indicates self-dealing rather than
interaction with diverse market participants.

- Default threshold: > 0.7

## Anomaly Score

The composite `anomaly_score` is the arithmetic mean of four normalized
sub-signals. Each sub-signal is monotone (increasing the raw metric always
increases the anomaly score), preventing adversarial partial-signal gaming.

## New ML Features

Two features are added to the tabular ensemble:

| Feature | Description |
|---------|-------------|
| `amm_tenure_ratio` | Fraction of completed AMM sessions with tenure below the threshold |
| `amm_volume_concentration` | Max volume-to-liquidity ratio across sessions, normalized to [0, 1] |

## Known Limitations

- **Legitimate flash-LP strategies**: Some professional LPs use short-tenure
  positions around anticipated volatility events. These may trigger false
  positives. The `AMM_MAX_TENURE_SECONDS` threshold can be tuned down to
  reduce false positives at the cost of missing slower wash cycles.

- **Cross-wallet coordination**: The current engine tracks sessions per
  wallet. Coordinated wash trading across multiple wallets requires the
  graph engine's SCC detection to link the participants.

## Configuration

Set in `.env` or environment variables:

```
AMM_MAX_TENURE_SECONDS=14400
AMM_MIN_VOLUME_RATIO=5.0
AMM_MIN_SYMMETRY=0.85
AMM_MIN_COUNTERPARTY_CONCENTRATION=0.7
```
