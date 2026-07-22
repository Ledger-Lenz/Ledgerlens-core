# Uncertainty Quantification via Conformal Prediction

LedgerLens uses **split Conformal Prediction (CP)** to provide valid,
distribution-free prediction intervals alongside every risk score. This
document explains what CP is, why we use it, and how to interpret the
uncertainty fields — written for compliance, legal, and operations teams
who need to trust the numbers.

## The Problem

Every risk score is a **point estimate**: "Wallet GABCD… has a risk score
of 72." But a point estimate without uncertainty is misleading:

- **72 ± 3** → the model is highly confident (narrow interval)
- **72 ± 40** → the model is guessing (wide interval)

Without uncertainty, an analyst cannot distinguish these two cases.
Regulators require this distinction.

## What is Conformal Prediction?

Conformal prediction is a **distribution-free** framework that produces
prediction sets (or intervals) with a **guaranteed coverage rate**.

> **Guarantee**: At the 90 % coverage level, the true label (wash trade
> or clean) is contained in the prediction set for **at least 90 % of
> examples** — regardless of the data distribution.

This is different from Bayesian credible intervals or bootstrap
confidence intervals, which rely on distributional assumptions that
rarely hold in practice.

### Key Properties

| Property | CP | Bayesian | Bootstrap |
|----------|----|----------|-----------|
| Distribution-free | Yes | No | No |
| Finite-sample validity | Yes | Asymptotic | Asymptotic |
| Works on any model | Yes | Requires prior | Requires resampling |
| Auditable | Yes | No | Partially |

## How LedgerLens Implements CP

### Calibration Phase (during training)

1. **Reserve 10 % of the labelled data** as a calibration set (never
   seen during training)
2. For each example in the calibration set, compute the **nonconformity
   score**: `1 - softmax_score[true_class]`
3. Take the `(1 - α)` quantile of these scores ⇒ **q_hat** (the
   nonconformity threshold)
4. The calibration artifact (a JSON file containing `q_hat`, `α`, and a
   SHA-256 integrity digest) is stored alongside the model file

### Inference Phase (during scoring)

1. For a new wallet, compute the softmax probabilities from the ensemble
2. The **prediction set** includes all classes `j` where
   `1 - softmax_score[j] ≤ q_hat`
3. The **prediction interval** on the 0-100 risk score is:
   - `lower = max(0, score - q_hat × 100)`
   - `upper = min(100, score + q_hat × 100)`

## Reading the Uncertainty Fields

Each risk score now includes four additional fields:

| Field | Type | Meaning |
|-------|------|---------|
| `score_lower` | float (0-100) | Lower bound of the 90 % prediction interval |
| `score_upper` | float (0-100) | Upper bound of the 90 % prediction interval |
| `prediction_set` | list[int] | Class labels in the conformal set (0 = clean, 1 = wash trade). An empty set is maximally uncertain. |
| `coverage_guarantee` | float (0-1) | The target coverage level (typically 0.90). This is a configurable parameter, not the empirical coverage on your data. |

### Interpretation Examples

| Scenario | score | score_lower | score_upper | Interpretation |
|----------|-------|-------------|-------------|----------------|
| High confidence | 85 | 82 | 88 | Narrow interval: model is certain |
| Low confidence | 55 | 15 | 95 | Wide interval: model is uncertain |
| Borderline | 72 | 67 | 77 | Moderate interval: some uncertainty |

## Security

Calibration artifacts are protected by a **SHA-256 digest** embedded in
the JSON file. On load, the digest is verified against the content. If
the file is tampered with (e.g., to artificially narrow an interval),
`CalibrationIntegrityError` is raised and the service falls back to
maximally conservative bounds (0-100).

## Fallback Behaviour

If no calibration artifact is present (first run, or artifact deleted),
`score_with_uncertainty` returns:

- `score_lower = 0.0`
- `score_upper = 100.0`
- `coverage_guarantee = 1.0`

This is the **maximally conservative** behaviour — the system refuses
to state a tighter bound than the trivial [0, 100] interval. A warning
is logged so operators are alerted.

## When to Worry

Wide intervals occur when:

1. **Distribution shift**: the current data differs from the training
   distribution (see also: drift monitoring via PSI)
2. **Low model agreement**: ensemble members disagree (low confidence
   score in the existing risk output)
3. **Out-of-distribution features**: e.g., trade volumes far outside
   the training range

In all cases, a wide interval is **correct behaviour** — it is the model
telling you "I don't know." This is much safer than a confidently wrong
point estimate.

---

## Multi-Class Extension: RAPS Prediction Sets (Issue-109)

### Three-Class Risk Taxonomy

LedgerLens now maps the 0-100 risk score to three risk classes:

| Class | Label | Score Range |
|-------|-------|-------------|
| 0 | `clean` | 0–33 |
| 1 | `suspicious` | 34–66 |
| 2 | `wash` | 67–100 |

### RAPS Algorithm

RAPS (Regularised Adaptive Prediction Sets, Angelopoulos et al. 2021)
extends split conformal prediction to multi-class settings. It reduces
prediction set sizes via a regularisation term λ while maintaining marginal
coverage guarantees.

**Nonconformity score:**
```
s(x, y) = Σ_{j: π_j ≥ π_y} π_j  +  λ · max(o(y) − k_reg, 0)
```
where `o(y)` is the 1-indexed rank of class y in the sorted softmax, λ = 0.2,
and k_reg = 2 (regularisation kicks in only for the third class and beyond).

**Coverage guarantee:** The prediction set `C(x)` contains the true class with
probability ≥ 1 − α for any test point, regardless of the data distribution.

### Reading the Prediction Set

The `prediction_set` field lists the class indices that are statistically
compatible with the observed features at the chosen confidence level (90%):

| `prediction_set` | Meaning |
|-----------------|---------|
| `[0]` | Model is confident the wallet is **clean** |
| `[2]` | Model is confident the wallet is **wash trading** |
| `[1, 2]` | Borderline between suspicious and wash |
| `[0, 1, 2]` | Model is maximally uncertain |

An analyst should take borderline sets (`[0, 1]`, `[1, 2]`) as a signal for
closer manual review before acting on a prediction.

### Security Notes for RAPS Artifacts

- `q_hat` is validated as a finite positive float on load
- A corrupted `conformal_calibration.json` with `q_hat=Inf` includes all
  classes in every prediction set (useless but not harmful); a WARNING is
  logged and the system continues
- Calibration set labels are never returned via the API; only aggregate
  statistics (`q_hat`, `alpha`, `achieved_coverage`) are persisted

---

## Staying Valid Under Drift (Issue-385)

The coverage guarantee `P(Y ∈ C(X)) ≥ 1-α` rests on an **exchangeability**
assumption between the calibration set and the live test distribution. That
assumption silently breaks under concept drift — and until now, nothing
connected the conformal calibrator to any drift signal, fast or slow.

### The gap this closes

- `q_hat` was frozen at training time (a static JSON artifact loaded once
  per process) and never revisited based on any runtime signal.
- The only drift-awareness mechanism was PSI-histogram batch comparison
  (`detection/drift_monitor.py`), triggered manually or via cron
  (`cli.py retrain-check`) — detection latency bounded only by operator
  discipline, potentially days or weeks.

See [`docs/drift_monitor.md`](drift_monitor.md) for the new streaming
ADWIN + Page-Hinkley detectors (`detection/drift_detectors.py`) that close
the *detection* half of this gap with 100-200-observation latency. This
section covers the other half: what happens to the conformal calibrator
once drift is detected.

### Response mechanism: online threshold adaptation

Rather than a full offline retrain, or switching every request onto a
continuously-adapting alpha (which would need the historical calibration
score set retained and persisted indefinitely), LedgerLens uses a
lightweight **online quantile-tracking** update — the practical form of
Adaptive Conformal Inference (Gibbs & Candès, NeurIPS 2021), closely related
to "Conformal PID Control" (Angelopoulos et al., 2023):

```
q_hat_{t+1} = q_hat_t + gamma * (error_t - alpha)
```

where `error_t = 0` if the true label was covered by the prediction set
returned for that observation, else `1`. A run of misses widens `q_hat`
(larger, more conservative intervals); a run of hits narrows it back toward
the nominal calibration level. This is implemented as
`ConformalCalibrator.adapt_online()` in `detection/conformal.py`.

**Why this over full recalibration:** analyst feedback labels arrive on a
delay (via `POST /feedback`), so a fresh, sufficiently-large labelled
calibration window isn't available at the moment drift is detected. Online
quantile-tracking needs only one labelled outcome at a time and provides an
approximately-valid marginal coverage guarantee even under ongoing
(including adversarial) distribution shift, per Gibbs & Candès' Theorem 1 —
without blocking on a retrain cycle.

### How detection and adaptation are coupled

1. Every real-time scoring call feeds its feature vector to the streaming
   drift detectors (cheap, no label needed) — see
   `detection.model_inference.score_with_uncertainty`.
2. When analyst feedback arrives with a ground-truth label
   (`POST /feedback` → `detection.model_inference.record_feedback_and_adapt`),
   adaptation **only** runs if the drift registry considers drift "recently
   active" (`DriftDetectorRegistry.is_active()`, default: within the last
   200 observations). Under stationary conditions the static, training-time
   `q_hat` is left untouched — this avoids needless alpha churn when
   exchangeability isn't suspected to be broken.
3. The adapted `q_hat` is persisted back to the same JSON calibration
   artifact (`ConformalCalibrator.save()`), so the next scoring pipeline run
   — a separate process — picks it up automatically on its next
   `load_calibration()` call. No new distributed state is introduced; this
   reuses the existing load-from-disk artifact architecture.

### Observability

`GET /health/drift` exposes whether drift is currently considered active
(the same flag gating adaptation) alongside per-feature detector state. The
calibration artifact itself gains two new fields, `n_adaptations` and
`last_adapted_at`, so operators can see how often and how recently a given
model's threshold has been nudged away from its training-time value.

## References

- Angelopoulos, A. N. & Bates, S. (2023). *Conformal Prediction: A
  Gentle Introduction.* Foundations and Trends in Machine Learning.
  https://arxiv.org/abs/2107.07511
- Angelopoulos, A. N., Bates, S., Jordan, M. I., & Malik, J. (2021).
  *Uncertainty Sets for Image Classifiers using Conformal Prediction.*
  (RAPS algorithm) https://arxiv.org/abs/2009.14193
- Romano, Y., Sesia, M., & Candès, E. J. (2020). *Classification with
  Valid and Adaptive Prediction Sets.*
  https://arxiv.org/abs/2004.09150
- Gibbs, I. & Candès, E. J. (2021). *Adaptive Conformal Inference Under
  Distribution Shift.* NeurIPS 2021. https://arxiv.org/abs/2106.00170
- Angelopoulos, A. N., Candès, E. J., & Tibshirani, R. J. (2023).
  *Conformal PID Control for Time Series Prediction.*
  https://arxiv.org/abs/2307.16895
- Bifet, A. & Gavaldà, R. (2007). *Learning from Time-Changing Data with
  Adaptive Windowing.* SDM 2007 (ADWIN).
- Page, E. S. (1954). *Continuous Inspection Schemes.* Biometrika
  (Page-Hinkley test).
