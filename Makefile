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
