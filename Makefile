.PHONY: install test lint generate-data train serve benchmark benchmark-check

install:
	pip install -r requirements.txt

test:
	pytest

lint:
	ruff check .

generate-data:
	python3 cli.py generate-data

train:
	python3 cli.py train

serve:
	python3 cli.py serve --reload

# Re-measures the scoring pipeline benchmarks and overwrites
# benchmarks/baseline.json with the result. Run this after an intentional
# performance change so future CI runs compare against the new baseline.
benchmark:
	python3 benchmarks/benchmark_scoring.py --update-baseline

# Measures the scoring pipeline benchmarks and compares against the
# committed benchmarks/baseline.json, failing if p99 regresses > 20% or
# batch scoring stops scaling linearly. This is what CI runs.
benchmark-check:
	python3 benchmarks/benchmark_scoring.py
