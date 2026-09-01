.DEFAULT_GOAL := help

.PHONY: install install-dev install-test install-docs install-fuzz install-chain install-ml \
        lock lock-check lint test test-e2e test-chaos mutation-test \
        generate-data train serve \
        fuzz-quick docs docs-serve \
        benchmark-check \
        license audit audit-py audit-rust audit-go audit-ts \
        clean help

# ── Installation ─────────────────────────────────────────────────────────────

install: ## Install base runtime deps (matches the container image)
	pip install --upgrade pip==24.0
	pip install -r requirements/base.txt
	pip install -e . --no-deps

install-test: ## Install test dependencies (used in CI)
	pip install --upgrade pip==24.0
	pip install -r requirements/test.txt
	pip install -e . --no-deps

install-dev: ## Install full local dev dependencies (test + lint + fuzz + chain + ML)
	pip install --upgrade pip==24.0
	pip install -r requirements/dev.txt
	pip install -e . --no-deps

install-docs: ## Install documentation build dependencies
	pip install --upgrade pip==24.0
	pip install -r requirements/docs.txt
	pip install -e . --no-deps

install-fuzz: ## Install fuzzing harness dependencies
	pip install --upgrade pip==24.0
	pip install -r requirements/fuzz.txt
	pip install -e . --no-deps

install-chain: ## Install chain integration dependencies
	pip install --upgrade pip==24.0
	pip install -r requirements/chain.txt
	pip install -e . --no-deps

install-ml: ## Install ML tooling dependencies
	pip install --upgrade pip==24.0
	pip install -r requirements/ml.txt
	pip install -e . --no-deps

# ── Lock-file management ─────────────────────────────────────────────────────
# Regenerates all requirements/*.txt from their *.in sources.
# Run this whenever you change pyproject.toml version constraints or *.in files,
# then commit the updated *.txt files.
#
# Requires: pip install pip-tools
lock: ## Regenerate requirements/*.txt lockfiles from *.in sources
	bash requirements/compile.sh

lock-check: ## Dry-run check that all lockfiles are up to date
	@pip-compile --dry-run --quiet --output-file requirements/base.txt  requirements/base.in
	@pip-compile --dry-run --quiet --output-file requirements/test.txt  requirements/test.in
	@pip-compile --dry-run --quiet --output-file requirements/dev.txt   requirements/dev.in
	@pip-compile --dry-run --quiet --output-file requirements/docs.txt  requirements/docs.in
	@pip-compile --dry-run --quiet --output-file requirements/fuzz.txt  requirements/fuzz.in
	@pip-compile --dry-run --quiet --output-file requirements/chain.txt requirements/chain.in
	@echo "All lockfiles are up to date."

# ── Linting ──────────────────────────────────────────────────────────────────
lint: ## Run ruff lint checks
	ruff check .

# ── Tests ────────────────────────────────────────────────────────────────────
test: ## Run the full pytest suite
	pytest

mutation-test: ## Run mutmut mutation testing on core detection modules
	mutmut run --paths-to-mutate detection/benford_engine.py,detection/graph_engine.py,detection/model_inference.py
	@echo "=== Mutation Results ==="
	mutmut results --all

generate-data: ## Generate synthetic training data
	python3 cli.py generate-data

train: ## Train detection models
	python3 cli.py train

serve: ## Start the local API server with reload
	python3 cli.py serve --reload

# ── End-to-end tests ─────────────────────────────────────────────────────────
test-e2e: ## Run end-to-end tests
	pytest tests/e2e/ -m e2e -v --tb=short --timeout=300

# ── Chaos engineering ─────────────────────────────────────────────────────────
# CHAOS_TEST_TIMEOUT bounds the whole pytest run so that if the compose stack
# comes up but a service never becomes fully ready, the target fails fast
# instead of hanging a local shell or CI job indefinitely. 15m is chosen as
# roughly 5-7x the observed local runtime of the chaos suite (~2-3m) — generous
# enough to absorb slow image pulls and loaded CI runners, tight enough that a
# genuine hang is caught well inside the workflow's own 30m job timeout
# (.github/workflows/chaos.yml). Override per-invocation, e.g.
#   make test-chaos CHAOS_TEST_TIMEOUT=5m
# `timeout` exits 124 when the limit is hit. The compose stack is torn down on
# every exit path (pass, fail, or timeout) so no containers are left orphaned,
# and the original pytest exit status is propagated to the caller.
CHAOS_TEST_TIMEOUT ?= 15m

test-chaos: ## Run chaos-engineering tests (requires Docker)
	docker compose --profile chaos up -d --wait
	timeout $(CHAOS_TEST_TIMEOUT) pytest tests/chaos/ -m chaos -v --tb=short --timeout=120; \
	  status=$$?; \
	  docker compose --profile chaos down; \
	  exit $$status

# ── Documentation ─────────────────────────────────────────────────────────────
docs: ## Build the MkDocs documentation site
	mkdocs build

docs-serve: ## Serve the MkDocs documentation site locally
	mkdocs serve

# ── Benchmark ─────────────────────────────────────────────────────────────────
benchmark-check: ## Run benchmark tests
	pytest -m benchmark -q --no-header 2>&1 || true

# ── Fuzz testing ──────────────────────────────────────────────────────────────
# Runs each Atheris harness for 30 seconds — a quick pre-merge smoke check.
# Requires: pip install atheris  (or: make install-fuzz)
fuzz-quick: ## Run each Atheris fuzz harness for 30 seconds
	@echo "Running fuzz harnesses for 30s each..."
	@failed=0; \
	for harness in fuzz/fuzz_*.py; do \
	  name=$$(basename "$$harness" .py); \
	  mkdir -p fuzz/corpus/$$name; \
	  echo "  $$name ..."; \
	  python "$$harness" "fuzz/corpus/$$name" -max_total_time=30 -print_final_stats=1 2>&1 || failed=1; \
	  if find "fuzz/corpus/$$name" -name 'crash-*' | grep -q .; then \
	    echo "  CRASH detected in $$name"; \
	    failed=1; \
	  fi; \
	done; \
	if [ "$$failed" -eq 1 ]; then \
	  echo "fuzz-quick: one or more harnesses reported a crash. See fuzz/README.md to reproduce."; \
	  exit 1; \
	fi
	@echo "fuzz-quick: all harnesses completed without crashes."

# ── License inventory ─────────────────────────────────────────────────────────
# Generates reports/licenses-python.csv and prints a summary table.
# Requires: make install (pip-licenses is bundled in the dev extra)
license: ## Generate the Python license inventory into reports/
	@mkdir -p reports
	pip-licenses \
	  --format=csv \
	  --output-file=reports/licenses-python.csv \
	  --ignore-packages ledgerlens-core
	pip-licenses \
	  --format=plain-vertical \
	  --ignore-packages ledgerlens-core
	@echo ""
	@echo "Full inventory written to reports/licenses-python.csv"

# ── Vulnerability audits ──────────────────────────────────────────────────────
# Python (osv-scanner must be installed separately: https://github.com/google/osv-scanner)
audit-py: ## Run Python vulnerability audit (requires osv-scanner)
	@mkdir -p reports
	osv-scanner --lockfile requirements/base.txt --format table

# Rust
audit-rust: ## Run Rust vulnerability audit (requires cargo-audit)
	cargo audit

# Go
audit-go: ## Run Go vulnerability audit (requires govulncheck)
	cd go && govulncheck ./...

# TypeScript SDK
audit-ts: ## Run TypeScript SDK audit (npm audit)
	cd sdk && npm audit --audit-level=high

audit: audit-py audit-rust audit-go audit-ts ## Run all vulnerability audits (py + rust + go + ts)

# ── Housekeeping ─────────────────────────────────────────────────────────────

clean: ## Remove generated artifacts (caches, build output, coverage, reports)
	rm -rf .pytest_cache htmlcov site target test_snapshots drift_reports fuzz/artifacts fuzz/corpus
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -type f -delete
	find . -name '*.pyo' -type f -delete
	rm -f .coverage
	# Keep the tracked reports/.gitkeep; remove generated report files.
	find reports -type f ! -name .gitkeep -delete 2>/dev/null || true
	find reports -depth -type d -empty -delete 2>/dev/null || true
	@echo "Clean complete."

help: ## Show this help message
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'
