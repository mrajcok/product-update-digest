.PHONY: venv install test lint clean

PYTHON  := python3.13
VENV    := .venv
BIN     := $(VENV)/bin

venv:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	$(BIN)/playwright install chromium
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
