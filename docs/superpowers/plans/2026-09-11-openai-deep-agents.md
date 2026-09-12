# OpenAI Deep Agents Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the flagship RecallOps UI run a genuine OpenAI-powered, read-only Deep Agents investigation and expose measured model evidence without weakening LangGraph, HITL, or MCP write controls.

**Architecture:** A focused provider module loads validated local configuration and builds `ChatOpenAI`. A live-reasoning service invokes the existing sealed Deep Agents supervisor, captures only sanitized execution metadata, and decorates the durable deterministic case after its independent verification; it never receives Operations MCP authority. UI, evaluation, documentation, diagrams, and demo material consume one stable live-run summary contract.

**Tech Stack:** Python 3.12, uv, LangChain OpenAI, Deep Agents 0.7, LangGraph 1.2, FastMCP, Pydantic 2, Streamlit, pytest, Mermaid.

**Spec:** `docs/superpowers/specs/2026-09-11-openai-deep-agents-design.md`

## Global Constraints

- All changes are made inline on `main`; no worktree or feature branch.
- `.env` is Git-ignored and no secret value may enter state, traces, errors, reports, tests, screenshots, commits, or documentation.
- OpenAI is the only reasoning engine shown in primary product and architecture diagrams.
- Live agents receive read-only Recall Registry MCP and Traceability MCP tools; Operations MCP remains graph-only.
- Two HITL interrupts and independent verification remain authoritative.
- Credential-free CI and `make verify` remain deterministic.
- Live failures are visible and sanitized; fallback may not be represented as a live success.

---

### Task 1: Provider configuration and safe model construction

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/recallops/llm/__init__.py`
- Create: `src/recallops/llm/config.py`
- Create: `src/recallops/llm/openai_provider.py`
- Modify: `.env.example`
- Test: `tests/unit/test_llm_config.py`

**Interfaces:**
- Produces: `LLMSettings(mode, provider, model, embedding_model, timeout_seconds, max_retries)`
- Produces: `load_project_env(path: Path = PROJECT_ROOT / ".env") -> None`
- Produces: `get_llm_settings() -> LLMSettings`
- Produces: `build_chat_model(settings: LLMSettings) -> BaseChatModel`
- Produces: `sanitize_llm_error(error: BaseException) -> tuple[str, str]`

- [ ] Write failing tests proving deterministic defaults, OpenAI credential/model validation, process-environment precedence over `.env`, and secret redaction.
- [ ] Run `uv run pytest -q tests/unit/test_llm_config.py` and confirm failure because the interfaces do not exist.
- [ ] Add direct locked dependencies on `langchain-openai` and `python-dotenv`; implement strict configuration with `load_dotenv(..., override=False)`.
- [ ] Build `ChatOpenAI(model=settings.model, api_key=SecretStr(...), timeout=settings.timeout_seconds, max_retries=settings.max_retries)` only in OpenAI mode and map authentication, rate-limit, timeout, invalid-response, and generic failures to sanitized categories.
- [ ] Update `.env.example` with `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_EMBEDDING_MODEL`, and `RECALLOPS_MODEL_MODE`, with no sample secret.
- [ ] Run the focused tests, `uv lock --check`, `uv pip check`, Ruff, and commit.

### Task 2: Read-only live Deep Agents invocation

**Files:**
- Create: `src/recallops/llm/live_reasoning.py`
- Modify: `src/recallops/agents/deep_supervisor.py`
- Test: `tests/unit/test_live_reasoning.py`
- Test: `tests/unit/test_specialists.py`

**Interfaces:**
- Produces: `LiveReasoningSummary` with provider, model, status, plan, specialist sequence, read-tool sequence, response summary, token counts, duration, fallback flag, and sanitized error category.
- Produces: `LiveReasoningService.run(question: str, *, transport: Literal["direct", "stdio"]) -> LiveReasoningSummary`
- Consumes: `build_deep_supervisor(model: BaseChatModel, read_gateway: DirectGateway | StdioMCPGateway)`

- [ ] Write failing tests using scripted fake chat models for the required `write_todos` call, four ordered `task` delegations, structured final response, usage capture, and sanitized failure.
- [ ] Add safe callbacks/event projection that records model/tool names, order, status, latency, and aggregate token usage but not prompts, raw messages, chain-of-thought, credentials, or raw tool payloads.
- [ ] Invoke `supervisor.graph.ainvoke({"messages": [HumanMessage(content=question)]}, config=...)` with bounded recursion and validate the fixed four-role completion contract.
- [ ] Assert the compiled parent/subagent tool union excludes every Operations MCP tool and the returned specialist sequence is exact, unique, and complete.
- [ ] Return a typed failed summary rather than raising provider text across the UI boundary.
- [ ] Run focused unit tests and commit.

### Task 3: Durable product integration and explicit fallback

**Files:**
- Modify: `src/recallops/ui/adapter.py`
- Modify: `src/recallops/ui/app.py`
- Modify: `src/recallops/ui/presenters.py`
- Modify: `src/recallops/ui/theme.py`
- Test: `tests/ui/test_runtime_adapter.py`
- Test: `tests/ui/test_presenters.py`
- Test: `tests/ui/test_app_contract.py`

**Interfaces:**
- `DurableRuntimeAdapter(..., live_reasoning: LiveReasoningService | None, llm_settings: LLMSettings)`
- Case projection fields: `reasoning_mode`, `llm_status`, and `llm_run` containing only `LiveReasoningSummary.model_dump(mode="json")`.

- [ ] Write failing adapter tests showing OpenAI reasoning runs exactly once before the authoritative durable investigation, successful summaries survive projection/reload, and failed summaries trigger a visibly labelled deterministic fallback.
- [ ] Load project `.env` before building the Streamlit adapter; inject settings and the live service in OpenAI mode.
- [ ] Run live reasoning in `run_investigation`, run the existing durable graph, and attach the sanitized live summary without changing pending interrupts, versions, action digests, receipts, or verification.
- [ ] Rename the header metric to `Reasoning mode`; show `OpenAI · <model>`, readiness/run status, plan, four specialist cards, tool-call trail, token usage, latency, verifier handoff, and a prominent fallback warning.
- [ ] Remove copy that calls the flagship workflow an offline planner while preserving official/synthetic data labels.
- [ ] Run UI tests plus the existing runtime/HITL regression tests and commit.

### Task 4: Shared live-model evaluation lane

**Files:**
- Create: `src/recallops/evaluation/openai_live_adapter.py`
- Modify: `src/recallops/evaluation/orchestration_benchmark.py`
- Modify: `src/recallops/evaluation/orchestration_schema.py`
- Modify: `src/recallops/evaluation/scorecard.py`
- Modify: `src/recallops/cli.py`
- Modify: `Makefile`
- Test: `tests/e2e/test_orchestration_benchmark.py`
- Test: `tests/e2e/test_evaluations.py`
- Test: `tests/e2e/test_makefile.py`

**Interfaces:**
- Produces: `build_openai_live_factory(settings: LLMSettings) -> LiveRunnerFactory`.
- `make eval-model` loads the repository `.env` and uses the built-in OpenAI factory unless `LIVE_MODEL_ADAPTER` explicitly supplies a reviewed custom factory.
- `make ui-openai` requires `RECALLOPS_MODEL_MODE=openai` and starts the same Streamlit application.

- [ ] Write failing tests for the built-in factory, live task/delegation/tool/evidence metrics, usage/latency projection, missing-credential exit, and Make targets.
- [ ] Adapt `LiveReasoningService` observations to the existing sealed `LiveCapture` evaluation contract and preserve the read-only tool surface.
- [ ] Extend the scorecard with a separate live-model section without allowing live variance to change deterministic release-gate status.
- [ ] Implement `make ui-openai` and update `make eval-model` while leaving `make verify` credential-free.
- [ ] Run focused evaluation and Makefile contract tests and commit.

### Task 5: LLM-first diagrams, documentation, notebooks, and demo runbook

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/MIDDLEWARE_AND_HITL.md`
- Modify: `docs/DEMO_WALKTHROUGH.md`
- Modify: `docs/diagrams/*.mmd`
- Modify: `docs/images/*.svg`
- Modify: `docs/images/*.png`
- Modify: `scripts/build_notebooks.py`
- Modify: generated `notebooks/*.ipynb`
- Test: `tests/docs/test_documentation.py`
- Test: `tests/notebooks/test_notebooks.py`

**Interfaces:**
- Primary architecture narrative: OpenAI planner → Deep Agents supervisor → agentic RAG → four LLM specialists → critic/rewrite → structured synthesis → verifier/HITL control plane.
- Demo entry points: `make ui-openai`, `make eval-model`, and exact flagship case `H-1230-2026`.

- [ ] Write failing documentation tests requiring the LLM-first architecture language, live setup commands, model proof points, two evaluation lanes, and the complete presenter sequence.
- [ ] Update the primary diagrams so only OpenAI/LLM components perform reasoning; render LangGraph, validation, HITL, and MCP authorization as control infrastructure.
- [ ] Keep model-failure fallback only in a separate resilience diagram and operations text.
- [ ] Rewrite the demo runbook with prerequisites, exact commands, screen-by-screen actions, expected model/agent/tool evidence, HITL review and execution steps, evaluation narration, fallback recovery, reset instructions, troubleshooting, and a timed talk track.
- [ ] Update README, architecture, operations, middleware/HITL, and notebook teaching material to match the implemented product contract.
- [ ] Regenerate notebooks and PNG/SVG artifacts, run documentation/notebook/diagram tests, visually inspect every changed PNG, and commit.

### Task 6: Live smoke, E2E proof, security, and publication

**Files:**
- Modify: `docs/DEMO_WALKTHROUGH.md` only if the observed live workflow differs from documented copy.
- Modify: `README.md` only if final verified commands or evidence differ.

**Interfaces:**
- Completion evidence: one real OpenAI flagship run, deterministic full suite, security gates, generated artifact parity, clean Git state, and pushed `main`.

- [ ] Verify `.env` remains ignored and no tracked file contains an API-key-shaped value.
- [ ] Run a real OpenAI smoke investigation for `H-1230-2026` and assert provider/model status, four ordered specialists, read-only MCP calls, successful verifier handoff, pending action-review HITL, and zero pre-approval receipts.
- [ ] Exercise approve → execution confirmation → simulated write receipt, then the second action review and closure-blocked path in an isolated runtime directory.
- [ ] Run `make eval-model` with a bounded live corpus or one explicit smoke case and retain only sanitized metrics.
- [ ] Run `make verify`, production security, full tests, notebook execution, and diagram double-render/parity.
- [ ] Run an independent code-review gate, fix all correctness/security/documentation findings, and rerun affected tests.
- [ ] Confirm `git status --short --branch` is clean, commit any evidence-only documentation correction, push `main`, and verify local HEAD equals `origin/main`.
