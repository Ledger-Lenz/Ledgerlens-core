# Metric Naming Convention

All Prometheus metrics emitted by LedgerLens (API, detection, ingestion)
follow this convention. It is enforced in CI by
`scripts/lint_metric_names.py`. Run it locally with:

```bash
python scripts/lint_metric_names.py
```

## Rules

| Rule | Example | Enforced by lint |
|---|---|---|
| Prefix every name with `ledgerlens_` | `ledgerlens_scoring_latency_seconds` | yes |
| Use lower `snake_case` for names | `ledgerlens_webhook_deliveries_total` | yes |
| Put the subsystem after the prefix: `ledgerlens_<subsystem>_<what>_<unit>` | `ledgerlens_ingestion_queue_depth` | no (review) |
| Counters end with `_total`; nothing else does | `ledgerlens_ingestion_events_received_total` | yes |
| Histograms/summaries end with a unit suffix (`_seconds`, `_bytes`, `_ratio`, `_score`, `_blocks`, `_ledgers`) | `ledgerlens_http_request_duration_seconds` | yes |
| Use base units only: seconds, bytes (never `_ms`, `_minutes`, `_kb`) | `ledgerlens_pipeline_run_duration_seconds` | yes |
| Label names are lower `snake_case` and never `le`, `quantile`, `job`, `instance` | `asset_pair`, `status_code` | yes |
| Label values are bounded and contain no PII (wallets, tx hashes, keys) | `endpoint="/trades"` | no (review) |

### Standard label names

Reuse these rather than inventing synonyms:

| Label | Meaning |
|---|---|
| `status_code` | Numeric HTTP status code |
| `result` | Outcome of an operation: `success`, `failure`, `retry`, … |
| `endpoint` | Normalised URL path (see `ingestion.metrics._normalise_endpoint`) |
| `method` | HTTP method |
| `asset_pair` | Trading pair, e.g. `XLM/USDC` |
| `reason` / `error_class` | Bounded failure category |

## Audit of existing metrics

`api/metrics.py` and `ingestion/metrics.py` were audited against the rules above.
Every metric passes the lint-enforced rules. These inconsistencies remain and
are covered by the migration plan below:

| Current name / label | Target | Issue |
|---|---|---|
| `ledgerlens_http_requests_total`, `ledgerlens_http_request_duration_seconds`, `ledgerlens_http_rate_limit_hits_total`, `ledgerlens_http_retries_total` | `ledgerlens_ingestion_horizon_http_*` | Collide conceptually with API-side HTTP metrics; missing subsystem |
| `ledgerlens_dlq_entries_total`, `ledgerlens_dlq_depth` | `ledgerlens_ingestion_dlq_*` | Missing subsystem (the event bus has its own DLQ) |
| `ledgerlens_soroban_submissions_total{status}` | `{result}` | Non-standard outcome label |
| `ledgerlens_shadow_score_divergence` (histogram) | `ledgerlens_shadow_score_divergence_ratio` | Missing unit suffix (lint-exempt in `LEGACY_EXEMPT`) |

## Migration plan (deprecation period)

These metrics feed dashboards and alerts, so they are not renamed in place.

1. **Next minor release:** emit each target name alongside the old name
   (dual-write). Mark the old name `DEPRECATED:` in its help string.
2. **Same release:** move `monitoring/grafana/*.json`, `monitoring/alerts.yml` and
   recording rules to the new names.
3. **After two minor releases (at least 60 days):** remove the old names and
   their `LEGACY_EXEMPT` entries. Record the removal in `CHANGELOG.md`.

Do not add new entries to `LEGACY_EXEMPT`. Fix the name instead.
