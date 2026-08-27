# config/

This directory holds all runtime configuration for LedgerLens: environment
variable schemas, structured logging, distributed tracing, request correlation,
and cost-metric export. Every file in this directory is loaded at process
startup; nothing here is optional or lazy-loaded.

| File | Purpose | Reads `.env`? | Hot-reload? |
|------|---------|:-------------:|:-----------:|
| [`settings.py`](#settingspy) | Central pydantic-settings schema — single source of truth for all env vars | ✅ | ❌ (restart required) |
| [`logging_config.py`](#logging_configpy) | structlog JSON logging — call once at startup | ❌ | ❌ |
| [`telemetry.py`](#telemetrypy) | OpenTelemetry SDK init (OTLP or console exporter) | ✅ (`OTEL_*`) | ❌ |
| [`correlation.py`](#correlationpy) | Correlation ID middleware + wallet address masking | ❌ | ❌ |
| [`cost_exporter.py`](#cost_exporterpy) | Prometheus gauges for cost coefficients | via `settings` | ❌ |
| [`filter_config.yaml.example`](#filter_config-yaml) | Example hot-reload filter pipeline config | N/A | ✅ (file-watch) |

---

## settings.py

The central configuration object, built with
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/).
At import time, every field is validated against type and range constraints. A
missing required field or an out-of-range value **aborts startup immediately**
with a human-readable error listing every problem at once.

```python
from config.settings import settings

db_path = settings.ledgerlens_db_path
```

**Key design decisions:**

- `populate_by_name=True` — both the Python attribute name and the
  `LEDGERLENS_*` env var name are accepted (Pydantic v2 migration).
- `strict=False` globally — Horizon returns datetimes and numeric values as
  JSON strings, so those fields must remain coercible.
- Numeric `mode="before"` validators reject booleans, non-finite floats, and
  malformed text before constrained validation.
- `extra="ignore"` — unrecognised env vars are silently dropped, keeping the
  process compatible with additive environment changes.

See `.env.example` in the repo root for the full list of available settings
with inline documentation.

---

## logging_config.py

Configures [structlog](https://www.structlog.org/) for structured JSON logging.
Call `configure_logging()` once at process startup:

```python
from config.logging_config import configure_logging
configure_logging(service_name="ledgerlens", log_level="INFO")
```

Every log record is emitted as valid JSON with at least these fields:
`timestamp` (ISO-8601 UTC), `level`, `logger`, `correlation_id`, and
`trace_id`. The trace ID is read from the active OpenTelemetry span (if any);
otherwise it defaults to 32 zeros.

`configure_logging` must be called **before** `init_telemetry` so that the OTel
SDK's own log output is also captured in structured form.

---

## telemetry.py

Initialises the [OpenTelemetry](https://opentelemetry.io/) SDK. Call
`init_telemetry()` once at startup:

```python
from config.telemetry import init_telemetry, shutdown_telemetry
init_telemetry(service_name="ledgerlens")
# ... run the application ...
shutdown_telemetry()   # flush pending spans on graceful shutdown
```

**Exporter selection (automatic):**

| Condition | Exporter |
|-----------|----------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` is set | OTLP gRPC |
| Endpoint unreachable | Falls back to `ConsoleSpanExporter` (warning logged) |
| Env var not set | `ConsoleSpanExporter` |

mTLS is supported when `OTEL_EXPORTER_OTLP_CERTIFICATE`,
`OTEL_EXPORTER_OTLP_CLIENT_KEY`, and `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE`
are all provided. Unreadable certificate files fall back to console rather than
crashing.

---

## correlation.py

Provides two utilities:

1. **`CorrelationIDMiddleware`** — a Starlette middleware that reads
   `X-Correlation-ID` from incoming requests (or generates a UUID4) and
   propagates it in the response header. Used by `api/main.py`.

2. **`mask_wallet(addr)`** — truncates a 56-character Stellar wallet address
   to `GABC1234...WXYZ` format for safe inclusion in log output. All log
   formatters in LedgerLens call this before logging wallet addresses.

```python
from config.correlation import mask_wallet, get_correlation_id
safe_addr = mask_wallet("GABCDE...XYZ1234")
```

The correlation ID is stored in a `ContextVar` so it is correctly isolated
per async request even under concurrent load.

---

## cost_exporter.py

Registers three Prometheus gauges for cost coefficients read from `settings`:

| Gauge | Description |
|-------|-------------|
| `ledgerlens_cost_per_vcpu_hour_usd` | Cost per vCPU-hour |
| `ledgerlens_cost_per_gb_memory_hour_usd` | Cost per GB memory-hour |
| `ledgerlens_cost_per_gb_storage_month_usd` | Cost per GB storage per month |

The gauges are set once at startup and remain static until the process
restarts. Call `init_cost_metrics()` in the application startup hook:

```python
from config.cost_exporter import init_cost_metrics
init_cost_metrics()   # idempotent — safe to call multiple times
```

The gauges are consumed by Prometheus recording rules in
`monitoring/recording_rules_cost.yml` to compute budget utilisation metrics.

---

## filter_config.yaml

`filter_config.yaml.example` is the template for the hot-reload trade filter
pipeline. Copy it to `config/filter_config.yaml` and edit to activate filters:

```bash
cp config/filter_config.yaml.example config/filter_config.yaml
```

The file supports:
- **Asset pair whitelist / blacklist** — include or exclude specific pairs
- **Minimum volume threshold** — drop trades below a configured XLM amount
- **Asset type filter** — restrict to native, issued, or specific assets
- **Account exclusion list** — ignore known market maker or test accounts

All filters default to `enabled: false` (pass-through), so the pipeline runs
out of the box without any filter file. The `ingestion/filters.py` module
watches `config/filter_config.yaml` for changes and reloads without a restart.

See [docs/filter-pipeline.md](../docs/filter-pipeline.md) for full
documentation, YAML schema reference, and rejection-storage behaviour.

---

## Startup order

For correct initialisation, call these in order:

```python
configure_logging()    # 1. structlog first so OTel SDK logs are structured
init_telemetry()       # 2. OTel SDK (uses structlog output)
init_cost_metrics()    # 3. Prometheus gauges (reads settings)
```

`settings` is a module-level singleton and is available immediately on import.
`CorrelationIDMiddleware` is added to the FastAPI app at construction time and
does not require explicit initialisation.

---

## Further reading

- [docs/filter-pipeline.md](../docs/filter-pipeline.md) — full filter pipeline
  documentation
- [docs/observability.md](../docs/observability.md) — structured logging,
  correlation IDs, and OTel tracing in production
- [docs/metrics.md](../docs/metrics.md) — full Prometheus metric catalogue
- [docs/cost_and_capacity.md](../docs/cost_and_capacity.md) — cost model and
  capacity planning using the exported cost gauges
- `.env.example` — annotated list of all available environment variables
