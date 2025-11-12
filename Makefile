.PHONY: install run lint test

install:
	python -m pip install -e .[dev]

run:
	python -m clickup_agent --dry-run

lint:
	ruff check src

test:
	python -m pytest
