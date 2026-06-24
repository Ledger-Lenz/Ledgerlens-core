---
title: "Add Per-Feature PSI Trend Alerting and Escalation to Drift Monitor"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary

Extend `detection/drift_monitor.py` with per-feature PSI time-series tracking, configurable per-feature alert thresholds, and a three-tier escalation system: log `WARNING` at PSI > 0.10, log `ERROR` at PSI > 0.20, and trigger a webhook notification at PSI > 0.25. Add a `GET /admin/drift-reports/latest` endpoint that returns the most recent per-feature PSI values, trend direction, and escalation status.

## Background & Context

The existing `drift_monitor.py` computes PSI across all features and triggers a global retraining decision when ≥ 3 features exceed the threshold. This is too coarse: a single feature drifting severely (PSI = 0.40) while the rest are stable is a very different signal from 5 features all mildly drifting (PSI = 0.12). The former suggests a targeted evasion attack on a specific feature; the latter suggests a general market regime change.

Per-feature tracking adds three capabilities:
1. **Per-feature thresholds**: some features are inherently noisier (e.g., `intra_minute_clustering` varies with market activity) and should have higher alert thresholds. Others (e.g., `wash_ring_membership`) should have very low thresholds because even small drift there is operationally significant.
2. **Trend direction**: a PSI of 0.12 is different if it has been stable for 30 days vs if it jumped from 0.02 to 0.12 in 24 hours. Compute `psi_delta_24h` and `psi_trend` (rising/stable/falling).
3. **Webhook escalation**: critical drift (PSI > 0.25) on any feature should immediately notify operators, not wait for the next retrain-check cron job.

## Objectives

- [ ] Extend the `DriftReport` dataclass to include per-feature PSI, threshold, escalation level, trend direction, and `psi_delta_24h`
- [ ] Implement `PerFeaturePSIConfig` allowing per-feature override thresholds (YAML-configurable)
- [ ] Implement the three-tier escalation logic in `DriftMonitor._escalate(feature, psi)`
- [ ] Store per-feature PSI history in a new SQLite table `feature_psi_history` for trend computation
- [ ] Implement `DriftMonitor.compute_trend(feature)` returning `(psi_delta_24h, trend_direction)`
- [ ] Trigger webhook POST when any feature exceeds PSI > 0.25 (using `webhook_queue.py`)
- [ ] Expose `GET /admin/drift-reports/latest` endpoint with full per-feature breakdown
- [ ] Write tests for each escalation tier and trend direction computation

## Technical Requirements

### Updated DriftReport

```python
# detection/drift_monitor.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

class EscalationLevel(str, Enum):
    OK       = "ok"       # PSI < 0.10
    WARNING  = "warning"  # 0.10 <= PSI < 0.20
    ERROR    = "error"    # 0.20 <= PSI < 0.25
    CRITICAL = "critical" # PSI >= 0.25

class TrendDirection(str, Enum):
    RISING  = "rising"    # psi_delta_24h > +0.02
    STABLE  = "stable"    # abs(psi_delta_24h) <= 0.02
    FALLING = "falling"   # psi_delta_24h < -0.02

@dataclass
class FeaturePSIRecord:
    feature_name: str
    psi: float
    threshold: float              # per-feature threshold
    escalation: EscalationLevel
    psi_delta_24h: float
    trend: TrendDirection
    n_production: int
    n_reference: int

@dataclass
class DriftReport:
    computed_at: datetime
    model_version: str
    drift_detected: bool
    n_drifted_features: int
    min_drifted_features: int
    feature_records: list[FeaturePSIRecord]
    global_psi: float                         # mean PSI across all features
    promoted: bool = False
    forced_retrain: bool = False

    @property
    def critical_features(self) -> list[str]:
        return [r.feature_name for r in self.feature_records
                if r.escalation == EscalationLevel.CRITICAL]
```

### Per-feature PSI configuration

```python
# config/drift_thresholds.yml  (new file)
# Default threshold is 0.20; per-feature overrides below
feature_thresholds:
  wash_ring_membership:            0.10   # very sensitive
  network_centrality:              0.10
  intra_minute_clustering:         0.30   # naturally noisy
  off_hours_activity_ratio:        0.30
  temporal_anomaly_score:          0.15
  gnn_wash_ring_prob:              0.12
  # all other features use the global default (DRIFT_PSI_THRESHOLD env var)


class PerFeaturePSIConfig:
    def __init__(self, config_path: str = "config/drift_thresholds.yml"): ...

    def threshold_for(self, feature: str) -> float:
        """Return per-feature threshold or global default."""
        ...
```

### PSI history table

```sql
CREATE TABLE IF NOT EXISTS feature_psi_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_name TEXT NOT NULL,
    psi          REAL NOT NULL,
    model_version TEXT NOT NULL,
    computed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fph_feature_time
    ON feature_psi_history(feature_name, computed_at DESC);
-- Retention: prune rows older than 90 days during each compute
```

### Escalation logic

```python
class DriftMonitor:
    def _escalate(self, feature: str, psi: float, threshold: float) -> EscalationLevel:
        if psi < 0.10:
            return EscalationLevel.OK
        elif psi < 0.20:
            logger.warning("Feature drift WARNING: %s PSI=%.4f", feature, psi)
            return EscalationLevel.WARNING
        elif psi < 0.25:
            logger.error("Feature drift ERROR: %s PSI=%.4f", feature, psi)
            return EscalationLevel.ERROR
        else:
            logger.error("Feature drift CRITICAL: %s PSI=%.4f — triggering webhook", feature, psi)
            self._trigger_drift_webhook(feature, psi)
            return EscalationLevel.CRITICAL

    def _trigger_drift_webhook(self, feature: str, psi: float) -> None:
        """
        Enqueue a webhook notification via webhook_queue.py.
        Event type: 'feature_drift_critical'
        Payload: {feature, psi, model_version, timestamp}
        Only notifies subscribers registered for 'drift' events.
        """
        from detection.webhook_queue import WebhookQueue
        WebhookQueue.enqueue(event_type="feature_drift_critical", payload={
            "feature": feature,
            "psi": psi,
            "model_version": self._model_version,
        })

    def compute_trend(self, feature: str) -> tuple[float, TrendDirection]:
        """
        Query feature_psi_history for the PSI 24 hours ago.
        psi_delta_24h = current_psi - psi_24h_ago.
        Returns (psi_delta_24h, TrendDirection).
        """
        ...
```

### API endpoint

```python
@router.get("/admin/drift-reports/latest")
async def latest_drift_report(
    x_admin_key: str = Header(..., alias="X-LedgerLens-Admin-Key"),
) -> DriftReportResponse:
    """
    Returns the most recent DriftReport with full per-feature breakdown.
    Includes escalation levels, trend directions, and critical_features list.
    """
    ...

@dataclass
class DriftReportResponse:
    computed_at: str
    model_version: str
    drift_detected: bool
    global_psi: float
    critical_features: list[str]
    feature_breakdown: list[FeaturePSIRecordResponse]
```

### Configuration

```
DRIFT_PSI_THRESHOLD=0.20                    # global default
DRIFT_THRESHOLDS_CONFIG=config/drift_thresholds.yml
DRIFT_CRITICAL_WEBHOOK_ENABLED=true
DRIFT_HISTORY_RETENTION_DAYS=90
DRIFT_TREND_DELTA_THRESHOLD=0.02            # |delta| > this = rising/falling
```

## Security Considerations

- **Admin key requirement**: `GET /admin/drift-reports/latest` must require the `X-LedgerLens-Admin-Key` header. Return 503 (not 401) if the key is unset, to avoid revealing the endpoint exists to unauthenticated callers
- **Webhook payload sanitisation**: the `feature` name in the webhook payload is sourced from `FEATURE_NAMES` — a hardcoded list. No user input flows into this payload. Document this explicitly to prevent future injection vectors if the payload is ever extended with external data
- **PSI history retention**: 90 days × 41 features × 4 runs/day = ~15,000 rows — trivial. The retention pruning is precautionary. Never prune rows younger than 48 hours (to preserve trend calculation data)
- **YAML config injection**: `config/drift_thresholds.yml` is loaded from the filesystem. Validate that all threshold values are floats in `[0.0, 1.0]` and all feature names are in `FEATURE_NAMES` after loading. Reject invalid config at startup
- **Webhook flood protection**: if 10+ features are CRITICAL simultaneously (e.g., after a major data pipeline outage), the webhook queue must deduplicate: send one aggregate notification rather than 10 individual ones. Implement a 60-second cooldown per event type

## Testing Requirements

- [ ] `tests/test_drift_monitor.py` — unit and integration tests
- [ ] Test: PSI = 0.08 → EscalationLevel.OK, no log warning
- [ ] Test: PSI = 0.15 → EscalationLevel.WARNING, logger.warning called
- [ ] Test: PSI = 0.22 → EscalationLevel.ERROR, logger.error called
- [ ] Test: PSI = 0.28 → EscalationLevel.CRITICAL, logger.error called, webhook enqueued
- [ ] Test: `compute_trend` returns RISING when current PSI > 24h-ago PSI by > 0.02
- [ ] Test: `compute_trend` returns STABLE when |delta| ≤ 0.02
- [ ] Test: `compute_trend` returns STABLE when no historical data (cold start)
- [ ] Test: `PerFeaturePSIConfig` returns per-feature threshold for configured features
- [ ] Test: `PerFeaturePSIConfig` returns global default for unconfigured features
- [ ] Test: YAML config with out-of-range threshold raises ValueError at load
- [ ] Integration test: `GET /admin/drift-reports/latest` returns correct schema with all feature records

## Documentation Requirements

- [ ] Docstrings on `DriftMonitor`, `PerFeaturePSIConfig`, `DriftReport`, `FeaturePSIRecord`
- [ ] Update `README.md` Drift Detection section to describe the three-tier escalation system
- [ ] Update `docs/adversarial_robustness.md` to describe how CRITICAL drift triggers adversarial investigation
- [ ] Add comments in `config/drift_thresholds.yml` explaining the rationale for each per-feature threshold
- [ ] Document `feature_psi_history` table in `docs/database_schema.md`
- [ ] Update `.env.example` with five new configuration variables

## Definition of Done

- [ ] Three-tier escalation implemented and tested
- [ ] `PerFeaturePSIConfig` YAML-driven configuration implemented
- [ ] `feature_psi_history` table created via migration
- [ ] Trend computation implemented and tested
- [ ] Webhook trigger on CRITICAL escalation implemented and tested
- [ ] `GET /admin/drift-reports/latest` endpoint live
- [ ] All tests pass including all four escalation-tier tests
- [ ] `config/drift_thresholds.yml` created with documented rationale

## For Contributors

**Ideal contributor profile**: You have experience building observability and alerting systems for ML models in production — model monitoring, data drift detection, or feature store health dashboards. You understand PSI and other drift metrics (KL divergence, KS test) and can reason about when per-feature thresholds add value over global thresholds. Familiarity with Python logging, webhook delivery patterns, and YAML configuration is expected.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "ML model monitoring / observability", "data drift detection", "production ML operations"
2. **Relevant experience** — drift monitoring systems you have built; tools you have used (Evidently AI, Alibi Detect, Grafana ML); experience with per-feature alerting
3. **Approach / initial thoughts** — your thoughts on PSI vs Wasserstein distance for trend detection; concerns about the CRITICAL-tier webhook flood scenario; alternative trend metrics to `psi_delta_24h`
4. **Estimated time** — breakdown by component (escalation logic, per-feature config, history table, trend, webhook, API, tests, docs)
