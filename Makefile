PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
UV := $(shell command -v uv 2>/dev/null)

# uv venvs ship without pip, so install through uv when it is available and
# fall back to pip otherwise. Both paths refuse a Python older than 3.11 before
# creating anything, instead of leaving a wrong-version .venv that later runs
# would silently reuse.
ifdef UV
PIP_INSTALL := $(UV) pip install -q --python $(BIN)/python
MAKE_VENV := $(UV) venv -q --python '>=3.11' $(VENV)
else
PIP_INSTALL := $(BIN)/python -m pip install -q
MAKE_VENV := $(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "slurm-mcp needs Python >= 3.11; run: make PY=python3.12 install (or install uv)")' \
	&& $(PY) -m venv $(VENV) && $(BIN)/python -m pip install -q --upgrade pip
endif
CHECK_VENV := $(BIN)/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "$(VENV) is Python %d.%d; run make clean, then make install" % sys.version_info[:2])'

.PHONY: help install surface demo footprint serve smoke test lint typecheck check clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# The braces matter. The pip branch's MAKE_VENV is an `a && b && c` chain, and
# sh gives `||` and `&&` equal precedence, left to right, so without them an
# existing venv skipped only the version check and still re-ran `$(PY) -m venv`
# over it. tests/test_makefile.py runs this recipe with stub Pythons.
$(BIN)/slurm-mcp: pyproject.toml
	@test -x $(BIN)/python || { $(MAKE_VENV); }
	@$(CHECK_VENV)
	@$(PIP_INSTALL) -e ".[dev]"
	@touch $(BIN)/slurm-mcp

install: $(BIN)/slurm-mcp ## Create the venv and install

surface: install ## Print the read-only allowlist and what is refused
	@$(BIN)/slurm-mcp surface

demo: install ## Full overview against the hand-written fixtures — no cluster needed
	@$(BIN)/slurm-mcp --fixtures overview

footprint: install ## Reproduce the progressive-disclosure context measurement
	@$(BIN)/slurm-mcp footprint

serve: install ## Run the MCP server over stdio (installs the [server] extra)
	@$(PIP_INSTALL) -e ".[server]"
	@$(BIN)/slurm-mcp serve

smoke: install ## MCP handshake and tool calls over real stdio, against fixtures
	@$(PIP_INSTALL) -e ".[server]"
	@$(BIN)/python scripts/stdio_smoke.py

test: install ## Run the test suite (transport tests run if [server] is installed)
	@$(BIN)/python -m pytest -q

lint: install ## ruff check + ruff format --check, exactly as CI runs them
	@$(BIN)/ruff check .
	@$(BIN)/ruff format --check .

typecheck: install ## mypy --strict
	@$(BIN)/mypy

# Not the whole guard CI job: CI also asserts the MCP SDK is absent and checks
# the CLI's exit codes. And once `make serve` or `make smoke` has put [server]
# into .venv, this also type-checks against the SDK and runs the transport tests.
check: lint typecheck test ## ruff, ruff format, mypy --strict, pytest (CI's lint/type/test steps)

clean: ## Remove venv and caches
	@rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .hypothesis
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
