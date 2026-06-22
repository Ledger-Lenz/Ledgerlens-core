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

## References

- Angelopoulos, A. N. & Bates, S. (2023). *Conformal Prediction: A
  Gentle Introduction.* Foundations and Trends in Machine Learning.
  https://arxiv.org/abs/2107.07511
- Romano, Y., Sesia, M., & Candès, E. J. (2020). *Classification with
  Valid and Adaptive Prediction Sets.*
  https://arxiv.org/abs/2004.09150
