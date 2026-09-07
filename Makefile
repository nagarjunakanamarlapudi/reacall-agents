.DEFAULT_GOAL := help

RECALL_NUMBER ?= H-1230-2026
PORT ?= 8501
RUNTIME_DIR ?= .recallops-runtime-demo

.PHONY: \
	help setup data-validate demo ui ui-stdio \
	mcp-config mcp-smoke eval eval-summary \
	demo-data notebooks diagrams \
	test test-unit test-integration test-e2e test-product \
	lint format security security-full verify

help:
	@printf '%s\n' \
		'RecallOps project commands' \
		'' \
		'Setup and application:' \
		'  make setup             Install locked Python and Node dependencies' \
		'  make data-validate      Validate official and synthetic data artifacts' \
		'  make demo               Run the credential-free flagship CLI demo' \
		'  make ui                 Start the durable Streamlit command center' \
		'  make ui-stdio           Start Streamlit with real stdio MCP subprocesses' \
		'' \
		'MCP, evaluation, and generated artifacts:' \
		'  make mcp-config         Print the three-server MCP configuration' \
		'  make mcp-smoke          Test direct and stdio MCP server behavior' \
		'  make eval               Regenerate and summarize the 21-scenario report' \
		'  make eval-summary       Summarize the committed evaluation report' \
		'  make demo-data          Regenerate the deterministic synthetic dataset' \
		'  make notebooks          Rebuild and execute all six teaching notebooks' \
		'  make diagrams           Verify Mermaid double-render and SVG parity' \
		'' \
		'Quality gates:' \
		'  make test               Run the complete test suite' \
		'  make test-unit          Run unit tests' \
		'  make test-integration   Run integration tests' \
		'  make test-e2e           Run end-to-end tests' \
		'  make test-product       Run UI, notebook, and documentation tests' \
		'  make lint               Check formatting, lint, lock, and dependencies' \
		'  make format             Apply Ruff formatting' \
		'  make security           Run the production dependency/security gate' \
		'  make security-full      Report all findings, including dev dependencies' \
		'  make verify             Run the complete submission gate' \
		'' \
		'Optional variables:' \
		'  RECALL_NUMBER=$(RECALL_NUMBER)' \
		'  PORT=$(PORT)' \
		'  RUNTIME_DIR=$(RUNTIME_DIR)'

setup:
	uv sync --locked --all-groups
	npm ci

data-validate:
	uv run recallops data-validate

demo:
	uv run recallops demo --recall-number "$(RECALL_NUMBER)"

ui:
	RECALLOPS_RUNTIME_DIR="$(RUNTIME_DIR)" uv run streamlit run src/recallops/ui/app.py --server.port "$(PORT)"

ui-stdio:
	RECALLOPS_RUNTIME_DIR="$(RUNTIME_DIR)" RECALLOPS_MCP_TRANSPORT=stdio uv run streamlit run src/recallops/ui/app.py --server.port "$(PORT)"

mcp-config:
	uv run recallops mcp-config

mcp-smoke:
	uv run pytest -q tests/integration/test_mcp_servers.py

eval:
	uv run python -c 'import asyncio; from recallops.evaluation.runtime_executor import run_recallops_evaluations; asyncio.run(run_recallops_evaluations(scenario_path="data/evals/scenarios.json", output_path="data/evals/report.json"))'
	uv run recallops eval --report data/evals/report.json

eval-summary:
	uv run recallops eval --report data/evals/report.json

demo-data:
	uv run python scripts/generate_demo_data.py

notebooks:
	uv run python scripts/build_notebooks.py
	uv run pytest -q tests/notebooks/test_notebooks.py

diagrams:
	./scripts/render_diagrams.sh --verify

test:
	uv run pytest -q

test-unit:
	uv run pytest -q tests/unit

test-integration:
	uv run pytest -q tests/integration

test-e2e:
	uv run pytest -q tests/e2e

test-product:
	uv run pytest -q tests/ui tests/notebooks tests/docs

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv lock --check
	uv pip check

format:
	uv run ruff format .

security:
	uvx --from pip-audit pip-audit
	uvx --from bandit bandit -q -r src -ll
	npm audit --omit=dev

security-full:
	@status=0; \
	uvx --from pip-audit pip-audit || status=1; \
	uvx --from bandit bandit -q -r src || status=1; \
	npm audit --omit=dev || status=1; \
	npm audit || status=1; \
	exit $$status

verify: data-validate lint test notebooks diagrams mcp-smoke eval-summary security
