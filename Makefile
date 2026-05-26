.PHONY: venv install test lint clean

VENV := .venv
BIN  := $(VENV)/bin

venv:
	uv venv --python 3.13
	uv pip install -r requirements.txt
	@echo "Venv ready. Activate with: source .venv/bin/activate"

install: venv

test:
	$(BIN)/pytest tests/ -v

lint:
	$(BIN)/python -m py_compile $$(find . -name "*.py" -not -path "./.venv/*")
	@echo "Syntax OK"

clean:
	rm -rf $(VENV) __pycache__ .pytest_cache
	find . -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
