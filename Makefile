.PHONY: sync test lint clean

sync:
	uv sync --extra dev

test:
	uv run pytest

lint:
	uv run python -m py_compile $$(find . -name "*.py" -not -path "./.venv/*")
	@echo "Syntax OK"

clean:
	rm -rf .venv __pycache__ .pytest_cache
	find . -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
