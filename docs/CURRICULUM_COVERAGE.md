# Week 3 Curriculum Coverage

This map points to implemented, demonstrable evidence rather than slide-only terminology.

| Week 3 topic | Concrete RecallOps implementation | Demo/test evidence |
|---|---|---|
| Agent loop | Explicit LangGraph investigation nodes plus conditional action/recovery routes | Investigation and Audit timelines; `tests/integration/test_workflow.py` |
| Planning | Bounded `plan_investigation` tasks drive the runtime role dispatcher; Deep Agents separately uses real `write_todos` with exactly four delegations | Reorder/fail-closed planner tests; specialist plan cards |
| State and checkpoints | JSON-only `RecallOpsGraphState`, `AsyncSqliteSaver`, immutable `RuntimeResult`, checkpoint ID, `thread_id` | Restart at action/execution/recovery/closure interrupts |
| Memory | Durable per-case checkpoint state is working memory; source evidence and Operations SQLite remain truth; no cross-case model memory is claimed | Reopen same database paths; `get_case`/history tests |
| HITL | `interrupt()` action review, separate execution confirmation, write recovery, and closure review; exact `Command(resume=...)` binding | Two consent cycles in **Human Review**; `06_hitl_closure.svg` |
| Supervisor | Real optional Deep Agents graph with fixed read-only subagents; default deterministic runtime uses same four-role contract | Factory/tool-manifest tests; architecture narration |
| Multi-agent specialists | Plan-driven sequential pipeline of Regulatory Intake, Product & Lot Matching, Traceability/Reconciliation, and Containment; independent verifier outside the specialist context | Executed-order specialist/critic rows; dispatcher loop tests |
| Planner–executor–reflection | Planner produces dependency-checked tasks; the dispatcher consumes plan order one role at a time; verifier/agentic-RAG critic expose gaps; approved graph node executes | `03_orchestration.svg`; workflow/evaluator routes |
| Agentic RAG | Source-aware plan/route, BM25 + LSA, RRF, rerank, evidence critic, bounded rewrite, serializable cursor | Investigation retrieval trace; retrieval unit/MCP tests |
| MCP | Three FastMCP servers, resources, direct and stdio adapters, parity tests | `mcp-config`; stdio demo/evaluator R01 |
| Middleware | Retry, circuit breaker, budgets, structured/provenance validation, masking, approval, idempotency/version, watchdog, telemetry, cross-store fencing | `05_middleware_lifecycle.svg`; unit and red-team probes |
| Failures and recovery | Snapshot fallback, deterministic model fallback, circuit open, durable restart, stale/digest/key rejection, lost-response same-key recovery | Failure selector; R02/R08–R14/R18–R20 |
| Cost, latency, and reliability | Two-hop/four-query/eight-read retrieval budget, one-attempt writes, bounded retry/model calls, per-scenario latency budget | RAG bounds, traces, evaluator metrics |
| Observability and evaluation | Node/tool traces, source/mode, warnings, human history, receipts, 21 safety scenarios, 96 retrieval ablations, 24 orchestration comparisons, and hard safety rates/counters | **Audit & Evaluation**; evaluation reports; notebook 07 |
| Human evaluation limits | Anchored human presentation rubric for correctness/citations, completeness, uncertainty, actionability, and clarity; deterministic safety remains authoritative | `docs/EVALUATION_RUBRIC.md`; notebook 07 |
| A2A | Intentionally excluded. LangGraph has one coordinator; MCP is vertical capability access, not peer-agent messaging | Architecture diagram and presenter narration |

## Core story

The submission story is: an explicit workflow gathered cited evidence, used bounded specialist reasoning, retained uncertainty, paused for one exact human decision, paused again before one simulated write, advanced one version, and refused closure while authoritative gaps remained. That makes state, planning, multi-agent work, agentic RAG, MCP, middleware, HITL, recovery, and evaluation observable in one coherent product.
