.DEFAULT_GOAL := help

RECALL_NUMBER ?= H-1230-2026
PORT ?= 8501
RUNTIME_DIR ?= .recallops-runtime-demo
LIVE_SMOKE ?= 0
LIVE_SMOKE_REPORT ?= $(PROJECT_ROOT)/live-smoke.json
ifneq ($(origin LIVE_MODEL_ADAPTER),undefined)
override LIVE_MODEL_ADAPTER := $(value LIVE_MODEL_ADAPTER)
export LIVE_MODEL_ADAPTER
endif

PROJECT_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
EVAL_DIR := $(PROJECT_ROOT)/data/evals
UV_PROJECT := uv run --project "$(PROJECT_ROOT)"
UV_LIVE := $(UV_PROJECT) $(if $(wildcard $(PROJECT_ROOT)/.env),--env-file "$(PROJECT_ROOT)/.env")
SCORECARD := $(EVAL_DIR)/scorecard.json
SUITE_REPORTS := \
	$(EVAL_DIR)/report.json \
	$(EVAL_DIR)/retrieval_report.json \
	$(EVAL_DIR)/orchestration_report.json

.PHONY: \
	help setup data-validate demo ui ui-stdio ui-openai \
	mcp-config mcp-smoke eval-fast eval-safety eval-retrieval \
	eval-orchestration eval eval-summary eval-model \
	demo-data notebooks diagrams \
	test test-unit test-integration test-e2e test-product \
	lint format security security-full ci verify

help:
	@printf '%s\n' \
		'RecallOps project commands' \
		'' \
		'Setup and application:' \
		'  make setup             Install locked dependencies and check Docker renderer' \
		'  make data-validate      Validate official and synthetic data artifacts' \
		'  make demo               Run the credential-free flagship CLI demo' \
		'  make ui                 Start the durable Streamlit command center' \
		'  make ui-stdio           Start Streamlit with real stdio MCP subprocesses' \
		'  make ui-openai          Start Streamlit with required OpenAI configuration' \
		'' \
		'MCP, evaluation, and generated artifacts:' \
		'  make mcp-config         Print the three-server MCP configuration' \
		'  make mcp-smoke          Test direct and stdio MCP server behavior' \
		'  make eval-fast          Verify artifacts plus the stable evaluation smoke subset' \
		'  make eval-safety        Regenerate and verify the R01-R21 safety report' \
		'  make eval-retrieval     Run and verify the 96-case retrieval ablation' \
		'  make eval-orchestration Run and verify the 24-case offline comparison' \
		'  make eval               Run all deterministic suites and build the scorecard' \
		'  make eval-summary       Validate and summarize the combined scorecard' \
		'  make eval-model         Run full OpenAI evaluation; LIVE_SMOKE=1 runs one separate case' \
		'  make demo-data          Regenerate the deterministic synthetic dataset' \
		'  make notebooks          Rebuild and execute all seven teaching notebooks' \
		'  make diagrams           Verify canonical double-render and SVG/PNG parity' \
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
		'  make ci                 Run credential-free hosted-CI verification gates' \
		'  make verify             Run the complete submission gate' \
		'' \
		'Optional variables:' \
		'  RECALL_NUMBER=$(RECALL_NUMBER)' \
		'  PORT=$(PORT)' \
		'  RUNTIME_DIR=$(RUNTIME_DIR)' \
		'  LIVE_SMOKE=1 LIVE_SMOKE_REPORT=/path/to/new-smoke.json (one case, separate report)' \
		'  LIVE_MODEL_ADAPTER=module:attribute (explicit live opt-in only)'

setup:
	uv sync --locked --all-groups
	npm ci
	./scripts/render_diagrams.sh --check-runtime

data-validate:
	uv run recallops data-validate

demo:
	uv run recallops demo --recall-number "$(RECALL_NUMBER)"

ui:
	RECALLOPS_RUNTIME_DIR="$(RUNTIME_DIR)" uv run streamlit run src/recallops/ui/app.py --server.port "$(PORT)"

ui-stdio:
	RECALLOPS_RUNTIME_DIR="$(RUNTIME_DIR)" RECALLOPS_MCP_TRANSPORT=stdio uv run streamlit run src/recallops/ui/app.py --server.port "$(PORT)"

ui-openai:
	@$(UV_LIVE) recallops llm-check
	RECALLOPS_RUNTIME_DIR="$(RUNTIME_DIR)" $(UV_LIVE) streamlit run "$(PROJECT_ROOT)/src/recallops/ui/app.py" --server.port "$(PORT)"

mcp-config:
	@uv run recallops mcp-config

mcp-smoke:
	uv run pytest -q tests/integration/test_mcp_servers.py

eval-fast:
	$(UV_PROJECT) recallops eval --report "$(EVAL_DIR)/report.json"
	$(UV_PROJECT) recallops eval-retrieval --report "$(EVAL_DIR)/retrieval_report.json"
	$(UV_PROJECT) recallops eval-orchestration --report "$(EVAL_DIR)/orchestration_report.json"
	$(UV_PROJECT) recallops eval-scorecard --scorecard "$(EVAL_DIR)/scorecard.json"
	$(UV_PROJECT) pytest -q "$(PROJECT_ROOT)/tests/unit/test_evaluation_ranking.py"

eval-safety:
	@set -eu; \
	temporary=$$(mktemp "$(EVAL_DIR)/.report.json.XXXXXX"); \
	trap 'rm -f "$$temporary"' EXIT; \
	$(UV_PROJECT) python -c 'import asyncio, sys; from pathlib import Path; from recallops.evaluation.runtime_executor import run_recallops_evaluations; from recallops.evaluation.scorecard import load_safety_report; report = asyncio.run(run_recallops_evaluations(scenario_path=sys.argv[1], output_path=sys.argv[2])); verified = load_safety_report(Path(sys.argv[2]), Path(sys.argv[1])); raise SystemExit(0 if report.gate_passed and verified.gate_passed else 1)' "$(EVAL_DIR)/scenarios.json" "$$temporary"; \
	$(UV_PROJECT) recallops eval --report "$$temporary"; \
	mv "$$temporary" "$(EVAL_DIR)/report.json"; \
	trap - EXIT

eval-retrieval:
	@set -eu; \
	temporary=$$(mktemp "$(EVAL_DIR)/.retrieval_report.json.XXXXXX"); \
	trap 'rm -f "$$temporary"' EXIT; \
	$(UV_PROJECT) python -c 'import asyncio, sys; from pathlib import Path; from recallops.evaluation.retrieval_benchmark import run_retrieval_benchmark; report = asyncio.run(run_retrieval_benchmark(Path(sys.argv[1]), Path(sys.argv[2]))); raise SystemExit(0 if report.gate_passed else 1)' "$(EVAL_DIR)/retrieval_cases.json" "$$temporary"; \
	$(UV_PROJECT) recallops eval-retrieval --cases "$(EVAL_DIR)/retrieval_cases.json" --report "$$temporary"; \
	mv "$$temporary" "$(EVAL_DIR)/retrieval_report.json"; \
	trap - EXIT

eval-orchestration:
	@set -eu; \
	temporary=$$(mktemp "$(EVAL_DIR)/.orchestration_report.json.XXXXXX"); \
	trap 'rm -f "$$temporary"' EXIT; \
	$(UV_PROJECT) python -c 'import asyncio, sys; from pathlib import Path; from recallops.evaluation.orchestration_benchmark import run_orchestration_benchmark; report = asyncio.run(run_orchestration_benchmark(Path(sys.argv[1]), Path(sys.argv[2]))); raise SystemExit(0 if report.gate_passed else 1)' "$(EVAL_DIR)/orchestration_cases.json" "$$temporary"; \
	$(UV_PROJECT) recallops eval-orchestration --cases "$(EVAL_DIR)/orchestration_cases.json" --report "$$temporary"; \
	mv "$$temporary" "$(EVAL_DIR)/orchestration_report.json"; \
	trap - EXIT

$(SCORECARD): $(SUITE_REPORTS)
	$(UV_PROJECT) python -c 'import sys; from pathlib import Path; from recallops.evaluation.scorecard import build_scorecard; root = Path(sys.argv[1]); build_scorecard(root / "report.json", root / "retrieval_report.json", root / "orchestration_report.json", root / "scorecard.json")' "$(EVAL_DIR)"

eval: eval-safety eval-retrieval eval-orchestration $(SCORECARD)
	$(UV_PROJECT) recallops eval-scorecard --scorecard "$(EVAL_DIR)/scorecard.json"

eval-summary: $(SCORECARD)
	$(UV_PROJECT) recallops eval-scorecard --scorecard "$(EVAL_DIR)/scorecard.json"

eval-model:
ifeq ($(LIVE_SMOKE),1)
	@$(UV_LIVE) recallops eval-model-smoke --report "$(LIVE_SMOKE_REPORT)"
else
	@set -eu; \
	$(UV_LIVE) recallops eval-orchestration --run --live --cases "$(EVAL_DIR)/orchestration_cases.json" --report "$(EVAL_DIR)/orchestration_report.json"; \
	$(UV_PROJECT) python -c 'import sys; from pathlib import Path; from recallops.evaluation.scorecard import build_scorecard; root = Path(sys.argv[1]); build_scorecard(root / "report.json", root / "retrieval_report.json", root / "orchestration_report.json", root / "scorecard.json")' "$(EVAL_DIR)"; \
	$(UV_PROJECT) recallops eval-scorecard --scorecard "$(EVAL_DIR)/scorecard.json"
endif

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

PIP_AUDIT_VERSION := 2.10.1
BANDIT_VERSION := 1.9.4

define audit_locked_production_python
	@set -eu; \
	requirements="$$(mktemp "$${TMPDIR:-/tmp}/recallops-production-requirements.XXXXXX")"; \
	trap 'rm -f "$$requirements"' EXIT; \
	uv export --locked --no-dev --no-emit-project --format requirements-txt \
		--output-file "$$requirements" >/dev/null; \
	uvx --from "pip-audit==$(PIP_AUDIT_VERSION)" pip-audit \
		--requirement "$$requirements" --require-hashes --disable-pip
endef

security:
	$(audit_locked_production_python)
	uvx --from "bandit==$(BANDIT_VERSION)" bandit -q -r src -ll
	npm audit --omit=dev

security-full:
	@status=0; \
	requirements="$$(mktemp "$${TMPDIR:-/tmp}/recallops-production-requirements.XXXXXX")"; \
	trap 'rm -f "$$requirements"' EXIT; \
	uv export --locked --no-dev --no-emit-project --format requirements-txt \
		--output-file "$$requirements" >/dev/null || status=1; \
	uvx --from "pip-audit==$(PIP_AUDIT_VERSION)" pip-audit \
		--requirement "$$requirements" --require-hashes --disable-pip || status=1; \
	uvx --from "bandit==$(BANDIT_VERSION)" bandit -q -r src || status=1; \
	npm audit --omit=dev || status=1; \
	npm audit || status=1; \
	exit $$status

ci: data-validate lint test diagrams mcp-smoke eval-fast security

verify: data-validate lint test notebooks diagrams mcp-smoke eval security
