# Causal Inference in LedgerLens

## Why Causal Inference Instead of SHAP?

LedgerLens uses an ensemble of ML classifiers (Random Forest, XGBoost, LightGBM) to
score wallets for wash-trading risk. SHAP (SHapley Additive exPlanations) is used to
explain which features contributed most to each score.

SHAP is excellent for _feature attribution_, but it is fundamentally correlational.
Consider a wash-trader who is simultaneously in a trading ring (`wash_ring_membership=1`)
**and** has non-Benford transaction amounts (`chi_sq_24h` is high). Because these two
signals are correlated — wash bots generate both ring patterns and non-Benford digit
distributions — SHAP divides the credit between them.

**A regulator asking "would this wallet still be flagged if it fixed its Benford
distribution?" cannot be answered by SHAP.** SHAP tells you what contributed to the
score; it cannot tell you what _caused_ it.

Causal inference answers a different question: **"If we intervene on feature X (set it
to a specific value by force, holding everything else constant), what would happen to
the risk score?"** This is the `do(X=x)` operator from Pearl's do-calculus.

## The Causal DAG

LedgerLens encodes domain knowledge about wash-trading into a **causal directed acyclic
graph (DAG)**. Each directed edge `A → B` means "A causally influences B".

```
wash_activity (latent)
    │
    ├──► wash_ring_membership ──► volume_to_unique_counterparty_ratio ──► risk_score
    │           │                                                          ▲
    │           ├──► round_trip_trade_frequency ───────────────────────────┤
    │           │                                                          │
    │           └─────────────────────────────────────────────────────────►│
    │                                                                      │
    ├──► round_trip_trade_frequency ────────────────────────────────────── │
    │                                                                      │
    ├──► chi_sq_24h ────────────────────────────────────────────────────── │
    │                                                                      │
    └──► cycle_volume_ratio ─────────────────────────────────────────────► │
                                                                           │
account_age_days ──► wash_ring_membership                                  │
network_centrality ─► wash_ring_membership ────────────────────────────── │
gnn_wash_ring_prob ────────────────────────────────────────────────────── │
```

### Edge Justifications

| Edge                                                         | Justification                                                                                                              |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `wash_activity → wash_ring_membership`                       | Latent coordination is the root cause of observable ring membership                                                        |
| `wash_activity → round_trip_trade_frequency`                 | Self-dealing directly inflates round-trip counts independent of ring detection                                             |
| `wash_activity → chi_sq_24h`                                 | Wash bots use fixed lot sizes, causing non-Benford digit distributions                                                     |
| `wash_activity → cycle_volume_ratio`                         | Coordinated wash volume flows through ring cycles, driving the ratio up                                                    |
| `wash_ring_membership → volume_to_unique_counterparty_ratio` | Wallets in rings repeatedly trade with the same small set of counterparties                                                |
| `wash_ring_membership → round_trip_trade_frequency`          | Ring membership structurally implies round-trip patterns                                                                   |
| `account_age_days → wash_ring_membership`                    | New accounts are cheap to create (Sybil attacks); older accounts are costlier to Sybil and less likely to be in wash rings |
| `network_centrality → wash_ring_membership`                  | High-centrality nodes act as hubs that enable ring formation                                                               |
| `wash_ring_membership → risk_score`                          | The single strongest direct causal driver — ring membership is the most actionable indicator                               |
| `round_trip_trade_frequency → risk_score`                    | Direct causal path independent of ring detection                                                                           |
| `chi_sq_24h → risk_score`                                    | Benford anomaly is a direct causal contributor via the Benford sub-score                                                   |
| `cycle_volume_ratio → risk_score`                            | High cycle fraction is directly suspicious independent of explicit ring membership                                         |
| `volume_to_unique_counterparty_ratio → risk_score`           | Counterparty concentration is a direct risk indicator                                                                      |
| `network_centrality → risk_score`                            | High-centrality nodes are structurally suspicious                                                                          |
| `account_age_days → risk_score`                              | New accounts receive a direct score penalty                                                                                |
| `gnn_wash_ring_prob → risk_score`                            | The GNN's latent-space embedding is a direct input to the ensemble score                                                   |

### The Latent Variable: `wash_activity`

`wash_activity` is an **unobserved common cause** (latent variable). It represents
the latent coordination signal behind wash trading — the actual human or bot decision to
engage in self-dealing. We cannot observe it directly, but it explains why
`wash_ring_membership`, `round_trip_trade_frequency`, `chi_sq_24h`, and
`cycle_volume_ratio` tend to co-occur.

DoWhy handles latent variables by setting `observed = 0` in the GML graph definition.
This prevents the backdoor criterion from conditioning on `wash_activity` (since we
can't observe it) and correctly identifies the causal effects of observable features.

## Do-Calculus: How Interventions Work

Standard probability: `P(Y | X=x)` — "given that we _observe_ X=x, what is Y?"

Do-calculus: `P(Y | do(X=x))` — "if we _force_ X to be x (regardless of what caused it),
what is Y?"

The difference matters: if we condition on observing `wash_ring_membership=0`, we might
be selecting for wallets that are clean for other reasons (e.g., very new accounts that
haven't had time to form rings). If we _do_(`wash_ring_membership=0`) — we surgically
remove ring membership while holding everything else constant — we get the pure causal
effect.

LedgerLens uses the **backdoor criterion** to identify causal effects. Given the DAG,
the backdoor criterion finds a set of observable variables Z such that conditioning on Z
blocks all backdoor paths from treatment X to outcome Y, enabling identification of
`P(Y | do(X=x))` from observational data.

## Average Treatment Effect (ATE)

The **Average Treatment Effect** is defined as:

```
ATE(X) = E[risk_score | do(X=1)] - E[risk_score | do(X=0)]
```

A positive ATE means the feature causally _increases_ risk scores. The
`/scores/{wallet}/causal-explanation` endpoint returns the ATE for each observable
feature in the DAG.

### Interpreting the ATE Table

| Feature                               | Typical ATE                     | Interpretation                                                                           |
| ------------------------------------- | ------------------------------- | ---------------------------------------------------------------------------------------- |
| `wash_ring_membership`                | Large positive (e.g. +30–50)    | The dominant causal driver — ring membership directly causes high scores                 |
| `round_trip_trade_frequency`          | Moderate positive (e.g. +10–20) | Self-dealing pattern has strong direct effect                                            |
| `chi_sq_24h`                          | Moderate positive (e.g. +5–15)  | Benford non-conformity has real causal impact, not just correlation with ring membership |
| `cycle_volume_ratio`                  | Moderate positive               | High cycle fraction is causally suspicious                                               |
| `gnn_wash_ring_prob`                  | Large positive                  | GNN directly inputs into the ensemble                                                    |
| `network_centrality`                  | Small–moderate positive         | Network position has a real but smaller causal effect                                    |
| `volume_to_unique_counterparty_ratio` | Small positive                  | Partially mediated through ring membership                                               |
| `account_age_days`                    | Small negative                  | Older accounts are slightly less risky on average                                        |

> **Note**: SHAP values for `wash_ring_membership` and `chi_sq_24h` tend to be
> inflated for both features (shared credit). The ATE correctly assigns the larger
> share to `wash_ring_membership` because it is higher in the causal graph.

## Counterfactual Scores

The `counterfactual_score` in the API response answers: _"What would this wallet's
score be if feature X were set to value Y?"_

This is implemented via linear structural equations fitted from the scored-wallet
dataset. For the flagged wallet example:

```
counterfactual_score(wallet, {"wash_ring_membership": 0.0})
```

returns the predicted score if the wallet were not in a wash ring, while keeping all
other feature values unchanged. This is the direct answer to a regulator's question.

## Estimation Method

By default, LedgerLens uses **linear regression (backdoor adjustment)**:

```
risk_score = α + β₁·wash_ring_membership + β₂·round_trip_trade_frequency + ...
             + β_confounders·confounders + ε
```

The ATE is then `β_treatment × (treatment_value - control_value)`.

For nonlinear effects, set `CAUSAL_ESTIMATION_METHOD=backdoor.econml.dml.DML` in your
`.env` (requires the `econml` package).

## Refutation Tests

Before serving the ATE table, LedgerLens runs three DoWhy refutation tests to validate
the causal model:

| Test                        | What it checks                                                                   | Pass condition |
| --------------------------- | -------------------------------------------------------------------------------- | -------------- |
| `random_common_cause`       | Adds a random confounder to the data; ATE should not change significantly        | p-value > 0.05 |
| `placebo_treatment_refuter` | Replaces the treatment with random noise; estimated effect should collapse to ~0 | p-value > 0.05 |
| `data_subset_refuter`       | Re-estimates on a 70% random subset; ATE should remain stable                    | p-value > 0.05 |

### Coverage: every causally-identified feature, not just one treatment

`CausalEngine.all_feature_refutation_tests()` runs the three tests above **for every
feature in `feature_ate_table` that has a genuine DoWhy-identified causal estimate** —
not only `wash_ring_membership`. (`CausalEngine.refutation_tests(treatment_feature=...)`
still exists as a lower-level, single-treatment primitive, defaulting to
`wash_ring_membership` for backward compatibility; `all_feature_refutation_tests` is
what the API actually calls.)

Features whose ATE fell back to the correlational OLS estimator (see "Estimation
Provenance" below) are skipped: there is no identified DoWhy estimand to refute for
them, so running refutation tests against them would be meaningless. This skip is not
silent — it is exactly what `estimation_method`/`estimation_notes` in the API response
already flags.

### The rejection gate

The endpoint pools every p-value from every refutation test across every
causally-identified feature and computes:

```
failing_fraction = (# p-values < 0.05) / (total # of refutation tests run)
```

If `failing_fraction > 1/3` (`_MAX_FAILING_REFUTATION_FRACTION` in `api/main.py`), the
endpoint returns **HTTP 503** with a descriptive error instead of serving the ATE table.
If zero refutation tests were run at all (every feature fell back to the correlational
path — e.g. DoWhy is not installed), the gate does not fire: there is no causal claim
being made in that case, so there is nothing to refute, and the honestly-labeled
fallback table is served instead.

> **Fixed defect**: the original implementation ran exactly 3 refutation tests, always
> against the single hardcoded `wash_ring_membership` treatment, and compared the failing
> *count* against a threshold of exactly 3 (`if all_failing > 3`). Since the count could
> only ever range over `{0, 1, 2, 3}`, the condition `> 3` was mathematically unreachable
> — the gate silently passed on every request regardless of how badly the model failed
> refutation. The fraction-based design above remains meaningful at any coverage size
> (it scales with however many features/tests are actually run) instead of being tied to
> a fixed count that happened to equal its own ceiling.

### Estimation Provenance: `causal` vs. `correlational_fallback`

`CausalEngine.estimate_ate()` / `feature_ate_table()` retain their original float-only
signatures for backward compatibility. The provenance-aware equivalents —
`CausalEngine._estimate_ate_detailed()` and `feature_ate_table_detailed()` — return an
`ATEEstimate(value, method, identified, reason)` per feature, and this is what the API
now uses to populate three additive response fields:

| Field                | Meaning                                                                                          |
| --------------------- | ------------------------------------------------------------------------------------------------ |
| `estimation_method`   | Per-feature `"causal"` (genuine DoWhy-identified estimate) or `"correlational_fallback"` (plain OLS coefficient substituted). |
| `estimation_notes`    | Per-feature reason string, present only for `"correlational_fallback"` entries (e.g. `non_identifiable: ...`, `estimation_error: ...`, `dowhy_not_installed`). |
| `refutation_coverage` | The features that actually underwent refutation testing this request — the `"causal"` subset of `estimation_method`. |

A fallback occurs when any of the following happens for a given feature:

1. **DoWhy is not installed** — `reason = "dowhy_not_installed"`.
2. **The effect is non-identifiable** — DoWhy's `identify_effect()` is now called with
   `proceed_when_unidentifiable=False` (previously `True`), so a genuine
   non-identifiability raises instead of being silently masked and passed through to
   estimation. The resulting exception is caught and surfaced as
   `reason = "non_identifiable: <DoWhy's message>"`.
3. **DoWhy's `estimate_effect()` itself fails** (e.g. a singular design matrix) —
   `reason = "estimation_error: <exception message>"`.

In all three cases the fallback ATE value is still the OLS coefficient (so the endpoint
can serve *something*), but it is now impossible for a client to mistake it for a
genuine causal estimate — the two are never presented under an indistinguishable shape.

> **Fixed defect**: the original `estimate_ate()` wrapped DoWhy's
> `identify_effect`/`estimate_effect` calls in a bare `except Exception`, silently
> substituting the OLS coefficient on any failure — including non-identifiability, which
> was itself suppressed via `identify_effect(proceed_when_unidentifiable=True)`. Only a
> `logger.warning` (never surfaced to the API caller) marked the difference; an analyst
> reading the API response had no way to distinguish a real causal estimate from a
> correlational fallback presented as one.

This is a safety gate: a causal model that fails refutation may be misspecified and
should not be served to analysts as if its ATEs were reliable causal estimates.

## Configuration

| Variable                   | Default                      | Description                                     |
| -------------------------- | ---------------------------- | ----------------------------------------------- |
| `CAUSAL_ESTIMATION_METHOD` | `backdoor.linear_regression` | DoWhy estimation method                         |
| `CAUSAL_REFUTATION_RUNS`   | `100`                        | Number of simulations per refutation test       |
| `CAUSAL_MIN_SAMPLE_SIZE`   | `500`                        | Minimum scored wallets before fitting the model |

## SQLite ATE Cache

The fitted ATE table is cached in the `causal_ate_cache` SQLite table to avoid
re-fitting the structural equations on every API request:

```sql
CREATE TABLE IF NOT EXISTS causal_ate_cache (
    model_version TEXT NOT NULL,
    feature_name  TEXT NOT NULL,
    ate           REAL NOT NULL,
    computed_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (model_version, feature_name)
);
```

The `model_version` key is set via the `LEDGERLENS_MODEL_VERSION` environment variable
(default: `"default"`). After retraining the ML ensemble, update this variable to
invalidate the cache and trigger a fresh ATE estimation.

## Known Limitations

1. **`wash_activity` is unobserved**: the latent common cause is inferred from the DAG
   structure, not measured directly. If the DAG is misspecified (e.g., an edge is
   missing), the ATEs will be biased. The refutation tests provide a partial check.

2. **Linear structural equations**: the default estimation method assumes linear
   relationships. Wash-trading effects are likely nonlinear at extremes (e.g., very
   high ring membership saturates the score). Use `econml.dml.DML` for nonlinear
   estimation.

3. **No temporal structure**: the current DAG is static (cross-sectional). Causal
   effects that unfold over time (e.g., ring formation over weeks) are not modelled.

4. **Selection bias**: the training data only contains wallets that have been scored.
   If flagging is non-random (which it is — we score suspicious wallets first), the
   fitted structural equations may not generalise to the full wallet population.

5. **Counterfactual extrapolation**: `counterfactual_score` is reliable only when the
   override values are within the support of the training data. Setting
   `wash_ring_membership=0` for a wallet with all other features maximally suspicious is
   an extrapolation outside the training distribution.

## References

- Pearl, J. (2009). _Causality: Models, Reasoning, and Inference_. Cambridge University Press.
- Sharma, A., & Kiciman, E. (2020). [DoWhy: An End-to-End Library for Causal Inference](https://arxiv.org/abs/2011.04216).
- Bang, H., & Robins, J. M. (2005). Doubly robust estimation in missing data and causal inference models. _Biometrics_, 61(4), 962–973.
- Lundberg, S. M., & Lee, S.-I. (2017). [A unified approach to interpreting model predictions (SHAP)](https://arxiv.org/abs/1705.07874). NeurIPS.
