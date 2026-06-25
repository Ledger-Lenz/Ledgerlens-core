.PHONY: install test lint mutation-test generate-data train serve

install:
	pip install -r requirements.txt

test:
	pytest

lint:
	ruff check .

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

# ── Chaos engineering ────────────────────────────────────────────────────────
# Requires Docker + Docker Compose. Starts Toxiproxy + Redis, runs chaos suite,
# then tears down. Set LEDGERLENS_ADMIN_API_KEY before running.
test-chaos:
	docker compose --profile chaos up -d --wait
	pytest tests/chaos/ -m chaos -v --tb=short --timeout=120 || (docker compose --profile chaos down && exit 1)
	docker compose --profile chaos down

# ── Documentation ────────────────────────────────────────────────────────────
docs:
	mkdocs build

docs-serve:
	mkdocs serve
