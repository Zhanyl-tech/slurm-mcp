PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help install surface demo footprint serve test lint typecheck check clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/slurm-mcp: pyproject.toml
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	@$(BIN)/python -m pip install -q --upgrade pip
	@$(BIN)/python -m pip install -q -e ".[dev]"
	@touch $(BIN)/slurm-mcp

install: $(BIN)/slurm-mcp ## Create the venv and install

surface: install ## Print the read-only allowlist and what is denied
	@$(BIN)/slurm-mcp surface

demo: install ## Full overview against recorded fixtures — no cluster needed
	@$(BIN)/slurm-mcp --fixtures overview

footprint: install ## Reproduce the progressive-disclosure context measurement
	@$(BIN)/slurm-mcp footprint

serve: install ## Run the MCP server over stdio (needs the [server] extra)
	@$(BIN)/python -m pip install -q -e ".[server]"
	@$(BIN)/slurm-mcp serve

test: install ## Run the test suite (no cluster, no MCP transport needed)
	@$(BIN)/python -m pytest -q

lint: install ## ruff check + ruff format --check, exactly as CI runs them
	@$(BIN)/ruff check .
	@$(BIN)/ruff format --check .

typecheck: install ## mypy --strict
	@$(BIN)/mypy

check: lint typecheck test ## Everything CI runs

clean: ## Remove venv and caches
	@rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
