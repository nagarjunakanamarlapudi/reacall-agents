# RecallOps Command Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, usable, and submission-ready food-recall response command center that demonstrates the full Week 3 agentic AI curriculum.

**Architecture:** An explicit outer LangGraph controls a deterministic recall lifecycle and durable HITL, while an optional Deep Agent supervisor plans and delegates to fixed specialists. Three Python FastMCP servers isolate public recall data, synthetic traceability data, and approval-gated simulated operations.

**Tech Stack:** Python 3.12, uv, Pydantic 2, LangChain, LangGraph, Deep Agents, MCP/FastMCP, langchain-mcp-adapters, HTTPX, Streamlit, Pandas, Jupyter, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-30-recallops-design.md`

## Global Constraints

- Default execution is offline and requires no API key, Docker, or network.
- Python is exactly `>=3.12,<3.13` and dependencies are locked by `uv.lock`.
- All internal operational records display `SYNTHETIC — ACADEMIC DEMO`.
- Agents never execute operational writes directly; only approved graph nodes can call write tools.
- Every write requires approval, actor, justification, expected case version, and idempotency key.
- No A2A protocol, real customer PII, notifications, or production-system writes.
- Every notebook is self-contained and imports no `recallops` production module.

---

### Task 1: Project foundation and domain contracts

**Files:** `pyproject.toml`, `.python-version`, `.env.example`, `Makefile`, `src/recallops/{__init__,config,paths,models}.py`, `tests/unit/test_models.py`

**Produces:** Typed `RecallRecord`, `RecallPredicate`, `Product`, `Lot`, `TraceEvent`, `Reconciliation`, `ProposedAction`, `ApprovalDecision`, `AuditReceipt`, and `RecallCaseState` contracts.

- [ ] Write tests that reject invalid provenance, negative quantities, and malformed decisions.
- [ ] Run the focused tests and confirm missing imports fail.
- [ ] Add the minimal project metadata, settings, paths, and Pydantic models.
- [ ] Run focused and full tests; format and commit.

### Task 2: Reproducible public snapshot and synthetic digital twin

**Files:** `data/public/**`, `data/synthetic/northstar_demo/**`, `scripts/generate_demo_data.py`, `src/recallops/data/{__init__,loaders,generator}.py`, `tests/unit/test_data.py`

**Produces:** `load_recall_snapshot()`, `load_demo_dataset()`, `generate_demo_dataset(seed=20260830)`, and manifest/checksum validation.

- [ ] Test expected counts, referential integrity, source labels, deterministic checksums, exact/ambiguous/control lots, and quantity equation fixtures.
- [ ] Confirm the tests fail before files and loaders exist.
- [ ] Store the official openFDA `H-1230-2026` record with retrieval metadata; implement seeded Northstar data generation.
- [ ] Generate the data twice, prove identical checksums, run tests, and commit.

### Task 3: Domain services and actual MCP servers

**Files:** `src/recallops/services/{recall_registry,traceability,operations}.py`, `src/recallops/mcp/{common,recall_registry_server,traceability_server,operations_server,gateway}.py`, `tests/unit/test_services.py`, `tests/integration/test_mcp_servers.py`

**Produces:** Dedicated read and write service methods; three FastMCP stdio entry points; `DirectGateway` and `StdioMCPGateway` with matching async methods.

- [ ] Test recall lookup/fallback, candidate scoring, lot classification, forward/backward trace, reconciliation, approval rejection, stale version rejection, and idempotent receipts.
- [ ] Confirm focused failures.
- [ ] Implement services first, then thin MCP tool/resource wrappers, then the adapters client gateway.
- [ ] Start each server through stdio, discover its exact tool names, invoke one read tool and one approved simulated write, run tests, and commit.

### Task 4: Middleware and safety policies

**Files:** `src/recallops/agents/{middleware,policies,telemetry}.py`, `tests/unit/test_middleware.py`

**Produces:** `with_retry`, `CallBudget`, `CircuitBreaker`, `ApprovalGuard`, `mask_sensitive`, `require_provenance`, `ProgressWatchdog`, and structured trace events.

- [ ] Write tests for transient retry, permanent failure, open circuit, tool budget, PII masking, unapproved write, stale approval, missing provenance, and repeated progress signatures.
- [ ] Verify red; implement minimal composable policies around real callables.
- [ ] Run the full test file and commit.

### Task 5: Specialists and Deep Agent supervisor

**Files:** `src/recallops/agents/{specialists,planner,deep_supervisor,prompts}.py`, `tests/unit/test_specialists.py`

**Produces:** Deterministic planner and four specialist functions with Pydantic outputs; `build_deep_supervisor()` using `create_deep_agent`, fixed subagents, middleware, and no operational write tools.

- [ ] Test bounded todo plan, exact/ambiguous match rationale, trace coverage, evidence-cited containment drafts, and absence of write tools.
- [ ] Verify red; implement deterministic specialists and optional live Deep Agents adapter.
- [ ] Run tests and import-smoke the Deep Agent factory without invoking a provider; commit.

### Task 6: LangGraph orchestration, durable HITL, writes, and closure

**Files:** `src/recallops/agents/{state,workflow,runtime}.py`, `tests/integration/test_workflow.py`

**Produces:** `build_workflow()`, `RecallOpsRuntime.start_case()`, `.resume_case()`, `.get_case()`, `.inject_failure()`.

- [ ] Test the exact node sequence, review interrupt payload, same-thread resume, approve/edit/reject/escalate branches, approval-gated writes, monitoring, blocked closure, successful closure, and restart with SQLite checkpointer.
- [ ] Verify red; implement graph nodes and conditional routes with all side effects after interrupts.
- [ ] Run integration tests twice against a fresh temporary checkpoint database; commit.

### Task 7: Evaluation runner and failure matrix

**Files:** `data/evals/scenarios.json`, `src/recallops/evals/{__init__,metrics,runner}.py`, `tests/e2e/test_evaluations.py`

**Produces:** `run_evaluations()` and `data/evals/report.json` with route, match, trace, reconciliation, approval, idempotency, closure, fallback, and loop-control metrics.

- [ ] Write scenario/metric tests and a failing global safety gate.
- [ ] Implement the runner over fresh cases with deterministic decisions.
- [ ] Generate the report, enforce 100% safety-critical pass rate, run tests, and commit.

### Task 8: Streamlit command center and CLI

**Files:** `src/recallops/ui/{__init__,presenters,theme,app}.py`, `src/recallops/cli.py`, `tests/ui/test_presenters.py`, `tests/e2e/test_cli.py`

**Produces:** Five-view command center and `recallops demo/eval/mcp-config/data-validate` commands.

- [ ] Test source badges, quantity/gap presentation, review packet, decision validation, exact demo output, and CLI exit codes.
- [ ] Implement reusable presenters, session-state runtime, reviewer form, trace/eval tables, and failure selector.
- [ ] Run Streamlit headless smoke and CLI demo; commit.

### Task 9: Six self-contained teaching notebooks

**Files:** `scripts/build_notebooks.py`, `notebooks/01_*.ipynb` through `06_*.ipynb`, `tests/notebooks/test_notebooks.py`

**Produces:** Six offline notebooks with embedded data, executable assertions, visible outputs, concept summaries, and no project imports or install cells.

- [ ] Test exact notebook list, labels, required concepts, forbidden project/network dependencies, and execution output markers.
- [ ] Build notebooks from deterministic `nbformat` source.
- [ ] Execute all notebooks headlessly and run notebook tests; commit.

### Task 10: Product documentation and reproducible diagrams

**Files:** `README.md`, `PROPOSAL.md`, `docs/{ARCHITECTURE,DATA_SOURCES,MCP_AND_TOOLS,MIDDLEWARE_AND_HITL,OPERATIONS,EVALUATION,DEMO_WALKTHROUGH,SUBMISSION_CHECKLIST,BACKLOG}.md`, `docs/images/*.mmd`, `docs/images/*.svg`, `scripts/render_diagrams.sh`, `tests/docs/test_documentation.py`

**Produces:** Source-boundary, system architecture, orchestration, MCP/tool safety, middleware lifecycle, HITL lifecycle, and demo-story diagrams plus exact narration and copy-paste prompts.

- [ ] Test that every promised artifact exists, diagrams contain scope-critical labels, docs contain no placeholder language, and demo commands/inputs match the CLI/UI.
- [ ] Write Mermaid sources, render SVG with pinned Mermaid CLI, and visually inspect every SVG.
- [ ] Write docs from implemented behavior, including honest limitations and evaluator Q&A; run tests and commit.

### Task 11: Verification, security, and final scope audit

**Files:** `docs/VERIFICATION.md`, `data/evals/report.json`, `uv.lock`

**Produces:** Fresh evidence for install, data validation, tests, lint, notebooks, MCP smoke, CLI demo, UI smoke, evals, and dependency/security checks.

- [ ] Run `uv sync --all-groups`, `make data-validate`, `make test`, `make lint`, `make notebooks`, `make mcp-smoke`, `make eval`, `make demo`, and `make security`.
- [ ] Compare every design-spec inclusion/exclusion and Week 3 topic against code, tests, docs, and diagrams.
- [ ] Record exact counts, command outcomes, environment, warnings, and honest limitations in `docs/VERIFICATION.md`.
- [ ] Review the final diff for secrets, accidental real-world claims, missing provenance, and unapproved write paths; commit the verified state.

