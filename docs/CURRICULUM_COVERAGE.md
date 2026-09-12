# Week 3 Curriculum Coverage

This map points to implemented, demonstrable evidence rather than slide-only terminology.

| Week 3 topic | Concrete RecallOps implementation | Demo/test evidence |
|---|---|---|
| Agent loop | Explicit LangGraph investigation nodes plus conditional action/recovery routes | Investigation and Audit timelines; `tests/integration/test_workflow.py` |
| Planning | OpenAI `write_todos` plans four fixed roles before sequential delegation; middleware requires each successful typed result before the next | Live plan projection; real compiled-graph authority tests |
| State and checkpoints | JSON-only `RecallOpsGraphState`, `AsyncSqliteSaver`, immutable `RuntimeResult`, checkpoint ID, `thread_id` | Restart at action/execution/recovery/closure interrupts |
| Memory | Durable per-case checkpoint state is working memory; source evidence and Operations SQLite remain truth; no cross-case model memory is claimed | Reopen same database paths; `get_case`/history tests |
| HITL | `interrupt()` action review, separate execution confirmation, write recovery, and closure review; exact `Command(resume=...)` binding | Two consent cycles in **Human Review**; `06_hitl_closure.png` |
| Supervisor | Live OpenAI Deep Agents graph with four context-bound LLM roles, sealed reads and no inner checkpoint persistence | Live model/tool trail; factory/capability/privacy tests |
| Multi-agent specialists | Plan-driven sequential pipeline of Regulatory Intake, Product & Lot Matching, Traceability/Reconciliation, and Containment; independent verifier outside the specialist context | Executed-order specialist/critic rows; dispatcher loop tests |
| Planner–executor–reflection | LLM plans and delegates; policy-based RAG critic checks coverage; safe claims pass the independent source verifier; only the human-approved graph node executes | `03_orchestration.png`; workflow/evaluator routes |
| Agentic RAG | Source-aware plan/route, BM25 + LSA, RRF, rerank, evidence critic, bounded rewrite, serializable cursor | Investigation retrieval trace; retrieval unit/MCP tests |
| MCP | Three FastMCP servers, resources, direct and stdio adapters, parity tests | `mcp-config`; stdio demo/evaluator R01 |
| Middleware | Retry, circuit breaker, budgets, structured/provenance validation, masking, approval, idempotency/version, watchdog, telemetry, cross-store fencing | `05_middleware_lifecycle.png`; unit and red-team probes |
| Failures and recovery | Semantic stop without fallback; provider/transport/budget failure may discard partial claims and use labelled fallback; cancellation, stale/digest/key rejection, lost-response same-key recovery | Separate resilience diagram; scripted fixtures are not provider-outage evidence |
| Cost, latency, and reliability | Two-hop/four-query/eight-read retrieval budget, one-attempt writes, bounded model calls and one child completion correction; usage only when returned | Latest failed smoke: 195.583s / 83,995 tokens; successful live E2E metrics unavailable |
| Observability and evaluation | Node/tool traces, source/mode, warnings, human history, receipts, 21 safety scenarios, 96 retrieval ablations, 24 orchestration comparisons, and hard safety rates/counters | **Audit & Evaluation**; evaluation reports; notebook 07 |
| Human evaluation limits | Anchored human presentation rubric for correctness/citations, completeness, uncertainty, actionability, and clarity; deterministic safety remains authoritative | `docs/EVALUATION_RUBRIC.md`; notebook 07 |
| A2A | Intentionally excluded. LangGraph has one coordinator; MCP is vertical capability access, not peer-agent messaging | Architecture diagram and presenter narration |

## Core story

The primary architecture uses OpenAI for planning and four sequential LLM specialists. Deterministic retrieval policy, child completion checks, exact compiled hook identity and independent source verification are control infrastructure. The latest real attempt observed the plan and recall reads, then failed at matching after one bounded correction with no released claims or writes. A separate deterministic control demonstration pauses for one exact human decision, pauses again before one simulated write, advances one version, and refuses closure while gaps remain. Whole-agent live proof must connect successful model work to that durable lifecycle; the smoke alone does not execute HITL. Never combine these lanes into an unsupported live-success story.
