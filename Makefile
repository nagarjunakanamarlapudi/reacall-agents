.PHONY: test lint format demo-data

test:
	uv run --group dev pytest

lint:
	uv run --group dev ruff check .

format:
	uv run --group dev ruff format .

demo-data:
	uv run python scripts/generate_demo_data.py
