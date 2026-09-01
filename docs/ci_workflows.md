# CI Workflows Reference

This page summarises every GitHub Actions workflow under
[`.github/workflows/`](https://github.com/Ledger-Lenz/Ledgerlens-core/tree/main/.github/workflows).
It is meant as a quick orientation for contributors debugging a failing check:
what each workflow does, what triggers it, and roughly how long it takes.

Every summary below was written by reading the workflow YAML directly.

| Workflow | File | Triggers | Typical duration |
|----------|------|----------|------------------|
| CI | `ci.yml` | push to `main`, every PR | ~15–25 min (PR, incl. fuzz smoke) |
| Deploy Docs | `docs.yml` | push to `main`, manual | ~3–6 min |
| OpenAPI Schema Drift Check | `schema.yml` | push to `main`, every PR | ~2–4 min |
| CD | `cd.yml` | push to `main`, manual | ~6–12 min (no-op without Docker secrets) |
| Cost Monitoring Validation | `cost-monitoring-validation.yml` | PR / push to `main` touching monitoring files | ~3–5 min |
| License &amp; Vulnerability Scan | `license-vuln-scan.yml` | push/PR touching dependency files, nightly 04:00 UTC, manual | ~6–12 min |
| Chaos Engineering | `chaos.yml` | weekly Mon 02:00 UTC, manual | ~15–30 min |
| Cross-Repo E2E Tests | `cross_repo_e2e.yml` | weekly Sun 00:00 UTC, manual | ~10–20 min |
| Nightly Fuzzing (Rust) | `fuzz-nightly.yml` | nightly 02:00 UTC, manual | ~1–3 h |
| Nightly Python Fuzz Campaign | `nightly_fuzz.yml` | nightly 03:30 UTC, manual | up to 45 min |
| Nightly Red-Team Campaign | `nightly_red_team.yml` | nightly 03:00 UTC, manual | up to 60 min |

---

## Pull-request and push gates

These run on every PR (and/or every push to `main`) and are the checks that
normally block a merge.

### `ci.yml` — CI

**Triggers:** push to `main`, every pull request. Concurrency-grouped per ref
with `cancel-in-progress`, so a new push supersedes an in-flight run.

**Jobs:**

- **Lock-file freshness (`lock-check`)** — runs `pip-compile --dry-run` for
  `base`, `test`, `dev`, `docs`, `fuzz`, and `chain` and fails if any
  `requirements/*.txt` is stale relative to its `.in` source or
  `pyproject.toml`.
- **Python `3.10` / `3.11` / `3.12` (`test`)** — installs from
  `requirements/test.txt`, runs `ruff check .`, then runs a curated stable
  smoke subset of the pytest suite (`test_settings`, `test_risk_score`,
  `test_features`, `test_graph_engine`, `test_benford_engine`, `test_storage`,
  `test_event_bus`, `test_webhook_queue`, `test_http_client`, `test_exceptions`)
  with coverage over `detection`, `ingestion`, and `config`.
- **Go SDK (`go-sdk`)** — `go vet ./...` and `go test ./... -race -count=1` in
  `go/`.
- **Benchmark regression (`benchmark`)** — runs `make benchmark-check` to guard
  scoring-pipeline performance against a regression threshold.
- **Rust SDK (`rust-sdk`)** — in `crates/ledgerlens-sdk`: `cargo check` (with
  and without the `zk-verify` feature), `cargo test`, `cargo clippy -D
  warnings`, and `cargo fmt --check`.
- **Contract fuzz (PR smoke) (`fuzz`)** — pull requests only. Runs
  `cargo +nightly fuzz` for 120 s against each of the `oracle_aggregator` and
  `zk_verifier` fuzz targets; uploads crash artifacts on failure.

**Duration:** roughly 15–25 min on a PR; the Rust SDK job and the fuzz smoke are
the long poles. The `test` job has a 60 min timeout as a backstop.

### `docs.yml` — Deploy Docs

**Triggers:** push to `main`, manual (`workflow_dispatch`).

**What it does:** checks out full history, installs `requirements/docs.txt` plus
the project (`-e . --no-deps`), and runs `mkdocs build --strict` — so a broken
link, a missing nav entry, or an unresolved `mkdocstrings` reference fails the
build. The `deploy` job publishes the built site to GitHub Pages, but only on
manual dispatch or when the `DEPLOY_PAGES` repo variable is `true`.

**Duration:** ~3–6 min.

### `schema.yml` — OpenAPI Schema Drift Check

**Triggers:** push to `main`, every pull request.

**What it does:** exports the live OpenAPI schema with
`python -m cli api export-schema` and `diff`s it against the committed
`docs/openapi.json`. If they differ, the job fails and prints the command to
regenerate and commit the schema.

**Duration:** ~2–4 min.

### `cost-monitoring-validation.yml` — Cost Monitoring Validation

**Triggers:** pull requests and pushes to `main` that touch
`monitoring/recording_rules_cost.yml`, `monitoring/alerts.yml`,
`monitoring/grafana/**`, `config/cost_exporter.py`, or
`tests/test_cost_metrics.py`.

**Jobs:**

- **Validate Prometheus Rules** — `promtool check rules` on the cost recording
  rules and on `alerts.yml`.
- **Validate Grafana Dashboard** — checks `cost_capacity_dashboard.json` is
  well-formed JSON, contains the expected panels, and references the expected
  recording-rule metric names.
- **Test Cost Metrics Exporter** — runs `tests/test_cost_metrics.py` against
  `config.cost_exporter`.

**Duration:** ~3–5 min.

---

## Deployment

### `cd.yml` — CD

**Triggers:** push to `main`, manual (`workflow_dispatch`).

**What it does:** if `DOCKER_USERNAME` and `DOCKER_PASSWORD` secrets are
configured, builds and pushes the Docker image tagged with the commit SHA, then
runs `helm upgrade --install ledgerlens ./helm/ledgerlens` with canary enabled.
If the secrets are absent every step is skipped and the workflow is a no-op.

**Duration:** ~6–12 min when deploying; seconds when skipped.

---

## Security and supply chain

### `license-vuln-scan.yml` — License &amp; Vulnerability Scan

**Triggers:** push to `main` and pull requests that touch dependency manifests
(`requirements/**`, `pyproject.toml`, `go/go.mod`, `go/go.sum`, `Cargo.toml`,
`Cargo.lock`, `sdk/package.json`); nightly at 04:00 UTC to catch new CVEs
against unchanged deps; manual.

**Jobs:**

- **Python license inventory** — generates a `pip-licenses` report and fails on
  GPL / AGPL / LGPL / CC-BY-SA.
- **Python vulnerability scan (OSV)** — `osv-scanner` over every
  `requirements/*.txt`. Report-only (does not fail the build).
- **Rust vulnerability scan** — `cargo audit`. Report-only.
- **Go vulnerability scan** — `govulncheck ./...` in `go/`. Report-only.
- **TypeScript vulnerability scan** — `npm audit --audit-level=high` in `sdk/`.
  Report-only.

**Duration:** ~6–12 min.

---

## Scheduled / on-demand suites

These never run on a PR. Trigger them manually from the Actions tab when a
change touches the relevant area.

### `chaos.yml` — Chaos Engineering

**Triggers:** weekly on Monday at 02:00 UTC, manual.

**What it does:** brings up the Toxiproxy + Redis + API stack with
`docker compose --profile chaos`, waits for `/health`, then runs
`pytest tests/chaos/ -m chaos`. Collects docker logs on failure and always
tears the stack down. 30 min job timeout.

### `cross_repo_e2e.yml` — Cross-Repo E2E Tests

**Triggers:** weekly on Sunday at 00:00 UTC, manual.

**What it does:** checks out `ledgerlens-api` and `ledgerlens-contracts`
alongside this repo and runs `pytest -m cross_repo_e2e tests/e2e_cross_repo/`.
Kept off PRs because it depends on sibling-repo availability and build time.

### `fuzz-nightly.yml` — Nightly Fuzzing (Rust)

**Triggers:** nightly at 02:00 UTC, manual.

**What it does:** matrix over `oracle_aggregator` and `zk_verifier`; deep-fuzzes
every `cargo-fuzz` target for 30 minutes each (`-max_len=4096`,
`-rss_limit_mb=4096`). Uploads crash artifacts on failure (90-day retention)
and reports corpus statistics. Runtime scales with the target count — expect
one to three hours.

### `nightly_fuzz.yml` — Nightly Python Fuzz Campaign

**Triggers:** nightly at 03:30 UTC, manual.

**What it does:** restores the fuzz corpus cache, installs
`requirements/fuzz.txt`, and runs each `fuzz/fuzz_*.py` Atheris harness for
300 s. Uploads crash / timeout / oom inputs and fails the run if any were
found. 45 min job timeout.

### `nightly_red_team.yml` — Nightly Red-Team Campaign

**Triggers:** nightly at 03:00 UTC, manual.

**What it does:** trains a small model, then runs
`python cli.py red-team --n-samples 200` and uploads the campaign report. Fails
the run if the evasion gate is breached (an attack type exceeds a 5% evasion
rate). 60 min job timeout.

---

## See also

- [`CONTRIBUTING.md`](https://github.com/Ledger-Lenz/Ledgerlens-core/blob/main/CONTRIBUTING.md) — "Before opening a PR" lists the checks you should run locally before CI does.
- [`docs/dependency_policy.md`](dependency_policy.md) — the license deny-list enforced by `license-vuln-scan.yml`.
- [`monitoring/README.md`](https://github.com/Ledger-Lenz/Ledgerlens-core/blob/main/monitoring/README.md) — context for `cost-monitoring-validation.yml`.
