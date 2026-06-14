.PHONY: sync test lint clean deploy-mcp

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

deploy-mcp:
	sudo cp src/hermes/digest_mcp.py /opt/digest/digest_mcp.py
	sudo chown root:digest /opt/digest/digest_mcp.py
	sudo chmod 754 /opt/digest/digest_mcp.py
	@echo "digest_mcp.py deployed to /opt/digest/"
