.PHONY: install dev test lint smoke clean

install:
	pip install -e ".[judges,langfuse,pdf]"

dev:
	pip install -e ".[judges,langfuse,pdf,dev]"

test:
	pytest -q

lint:
	ruff check mdk_eval tests

smoke:
	mdk-eval run --target mock --dataset datasets/sample.jsonl --runs 2 --output ./results --no-judges

clean:
	rm -rf results/ .pytest_cache/ .ruff_cache/ build/ dist/ *.egg-info
