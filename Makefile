.PHONY: install install-dev install-test install-docs install-fuzz install-chain install-ml \
        lock lint test test-e2e test-chaos mutation-test \
        generate-data train serve \
        fuzz-quick docs docs-serve \
        benchmark-check \
        license audit audit-py audit-rust audit-go audit-ts

# ── Installation ─────────────────────────────────────────────────────────────

# Base runtime deps (matches the container image)
install:
	pip install --upgrade pip==24.0
	pip install -r requirements/base.txt
	pip install -e . --no-deps

# Test surface (used in CI)
install-test:
	pip install --upgrade pip==24.0
	pip install -r requirements/test.txt
	pip install -e . --no-deps

# Full local dev: test + lint + fuzz + chain + ML + GraphQL + causal
install-dev:
	pip install --upgrade pip==24.0
	pip install -r requirements/dev.txt
	pip install -e . --no-deps

install-docs:
	pip install --upgrade pip==24.0
	pip install -r requirements/docs.txt
	pip install -e . --no-deps

install-fuzz:
	pip install --upgrade pip==24.0
	pip install -r requirements/fuzz.txt
	pip install -e . --no-deps

install-chain:
	pip install --upgrade pip==24.0
	pip install -r requirements/chain.txt
	pip install -e . --no-deps

install-ml:
	pip install --upgrade pip==24.0
	pip install -r requirements/ml.txt
	pip install -e . --no-deps

# ── Lock-file management ─────────────────────────────────────────────────────
# Regenerates all requirements/*.txt from their *.in sources.
# Run this whenever you change pyproject.toml version constraints or *.in files,
# then commit the updated *.txt files.
#
# Requires: pip install pip-tools
lock:
	bash requirements/compile.sh

# Dry-run check — same check that CI's lock-check job runs.
lock-check:
	@pip-compile --dry-run --quiet --output-file requirements/base.txt  requirements/base.in
	@pip-compile --dry-run --quiet --output-file requirements/test.txt  requirements/test.in
	@pip-compile --dry-run --quiet --output-file requirements/dev.txt   requirements/dev.in
	@pip-compile --dry-run --quiet --output-file requirements/docs.txt  requirements/docs.in
	@pip-compile --dry-run --quiet --output-file requirements/fuzz.txt  requirements/fuzz.in
	@pip-compile --dry-run --quiet --output-file requirements/chain.txt requirements/chain.in
	@echo "All lockfiles are up to date."

# ── Linting ──────────────────────────────────────────────────────────────────
lint:
	ruff check .

# ── Tests ────────────────────────────────────────────────────────────────────
test:
	pytest

mutation-test:
	mutmut run --paths-to-mutate detection/benford_engine.py,detection/graph_engine.py,detection/model_inference.py
	@echo "=== Mutation Results ==="
	mutmut results --all

generate-data:
	python3 cli.py generate-data

train:
	python3 cli.py train

serve:
	python3 cli.py serve --reload

# ── End-to-end tests ─────────────────────────────────────────────────────────
test-e2e:
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

test-chaos:
	docker compose --profile chaos up -d --wait
	timeout $(CHAOS_TEST_TIMEOUT) pytest tests/chaos/ -m chaos -v --tb=short --timeout=120; \
	  status=$$?; \
	  docker compose --profile chaos down; \
	  exit $$status

# ── Documentation ─────────────────────────────────────────────────────────────
docs:
	mkdocs build

docs-serve:
	mkdocs serve

# ── Benchmark ─────────────────────────────────────────────────────────────────
benchmark-check:
	pytest -m benchmark -q --no-header 2>&1 || true

# ── Fuzz testing ──────────────────────────────────────────────────────────────
# Runs each Atheris harness for 30 seconds — a quick pre-merge smoke check.
# Requires: pip install atheris  (or: make install-fuzz)
fuzz-quick:
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
license:
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
audit-py:
	@mkdir -p reports
	osv-scanner --lockfile requirements/base.txt --format table

# Rust
audit-rust:
	cargo audit

# Go
audit-go:
	cd go && govulncheck ./...

# TypeScript SDK
audit-ts:
	cd sdk && npm audit --audit-level=high

# Run all audits
audit: audit-py audit-rust audit-go audit-ts
