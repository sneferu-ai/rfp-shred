.PHONY: setup demo test quality build

setup:
	uv sync --frozen --extra dev --python 3.12

demo:
	uv run --frozen --extra dev python -m scripts.demo

test:
	uv run --frozen --extra dev pytest -q

quality:
	uv run --frozen --extra dev pytest -q --cov=app --cov-report=term --cov-fail-under=80
	uvx ruff check --select E9,F63,F7,F82 app tests scripts

build:
	uv build
