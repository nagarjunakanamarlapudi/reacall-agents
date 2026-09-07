# RecallOps Command Center — Submission Handout

## Project overview

RecallOps is an evidence-first, human-governed food-recall response command center. It turns official notice `H-1230-2026` into a durable simulated retailer case: agentic retrieval, bounded planning, product/lot classification, forward/backward trace, quantity reconciliation, human-reviewed containment, one simulated operation per version, acknowledgements, audit, and safe closure gating.

The [business-domain guide](BUSINESS_DOMAIN.md), [business recall lifecycle](images/08_business_recall_lifecycle.svg), and [domain evidence model](images/09_domain_evidence_model.svg) explain the real operating problem. The presentation visuals below lead with the honest data boundary and then show the AI implementation.

![Official openFDA evidence and synthetic retailer data remain visibly separate](images/recallops-data-boundary.png)

![RecallOps architecture: evidence, approval, and safe closure](images/recallops-system-architecture.png)

The source-controlled [provenance](images/01_data_provenance.svg) and [technical architecture](images/02_system_architecture.svg) diagrams are the reproducible detail views.

The differentiator is inspectable authority: RAG is advisory; structured evidence and transactions are authoritative; agents draft; the human approves one action; a second confirmation permits one approved graph-node write; Operations rechecks the request in SQLite.

## Datasets used

| Dataset | Actual scale/use | Boundary |
|---|---|---|
| Frozen openFDA response | Five enforcement records; flagship recall predicate/citations | Official snapshot, SHA-256 pinned |
| Optional openFDA lookup | Exact Food Enforcement endpoint on `api.fda.gov` | Allowlisted host; bounded timeout; labelled fallback |
| FDA/GS1 policy corpus | Five paraphrased reference documents | Official context; not a compliance ruling |
| Northstar digital twin | 48 products, 144 lots, 18 facilities, 577 events, 216 positions, 144 shipments, 18 acknowledgement seeds | `SYNTHETIC — ACADEMIC DEMO`; fictional |
| Hybrid knowledge corpus | 1,175 documents: 10 official, 1,165 synthetic | Per-record origin, URL, timestamp, hash, citation ID |

No You.com or general web search is used. A public recall does not establish that Northstar is involved.

## Technical architecture

- **Control:** LangGraph `StateGraph`, immutable public results, JSON-only typed state, conditional routes, `interrupt()`, `Command(resume=...)`, SQLite checkpointing.
- **Retrieval:** BM25 sparse + local TF-IDF/SVD LSA dense → reciprocal-rank fusion → deterministic rerank → evidence critic; source routing and 2-hop/4-query/8-read bounds.
- **Agents:** deterministic planner; Regulatory Intake, Product & Lot Matching, Traceability/Reconciliation, Containment; independent verifier; optional real Deep Agents supervisor with `write_todos` and no Operations tools.
- **MCP:** Recall Registry, Traceability, and Recall Operations FastMCP servers; direct and stdio parity.
- **Middleware:** context, structured output, retry, circuit breaker, budgets, provenance, masking, approval, version, idempotency, receipt validation, progress watchdog, telemetry, checkpoint-owner/head/request fencing.
- **Product:** five Streamlit views, CLI, failure injection, 21-scenario safety evaluator, 96-case retrieval ablation, 24-case orchestration comparison, seven notebooks, nine source-controlled diagrams.

There is no A2A. LangGraph coordinates all agents; MCP is the vertical data/action interface.

## Human and action contract

Human Review exposes **Review required** and the exact action, digest, current version, evidence, gaps, and remaining lifecycle. The form uses **Decision**, **Actor**, and **Justification** with decisions `approve`, `edit`, `reject`, `escalate`. The visible controls are **Approve**, **Edit**, **Reject**, and **Escalate**.

Approval performs zero writes. A separate execution-confirmation interrupt enables **Simulate approved actions**. The flagship records `create_case` v0→v1, then requires a fresh review/confirmation for `apply_inventory_hold` v1→v2. Each successful receipt shows **Simulated action recorded**. Later safe cases use the same loop for disposition, tasks, repeated acknowledgements, closure review, and `close_case`.

For the recovery proof, use a separate fresh runtime: approve `create_case`, select **lost write response → same-key replay** in **Audit & Evaluation**, click **Run failure fixture**, execute with **Simulate approved actions**, then use **Recover recorded outcome (same key)**. The visible invariant is one logical receipt and one version increment.

Copy/paste values:

- **Recall number:** `H-1230-2026`
- **Decision:** `approve`
- **Actor:** `Food-safety manager`
- **Justification:** `Authorize simulated containment for confirmed scope; retain ambiguous lot for review.`
- Escalation alternative: `Do not close while acknowledgement, ambiguity, or reconciliation gaps remain.`

## Evaluation design

R01–R21 are all safety-critical and cover source fallback, four-way matching, causal lineage, seven-component reconciliation, missing evidence, retries/circuit, model fallback, no-approval writes, changed/exact idempotent replay, stale version, task/ack coverage, positive closure, restart, watchdog, closure race, ambiguous hold, and direct/stdio parity. The hard gate requires every rate to be 1.0 and unsafe/duplicate/false-close/receipt-integrity counters to be zero. The pinned integrated report passes 21/21 scenarios and 320 assertions, with every required rate at 1.0 and all four unsafe counters at zero. Artifact hashes and execution provenance live in [Verification](VERIFICATION.md).

## Vibe-coding prompts and briefs

The build was coordinated from explicit acceptance briefs rather than an open-ended “make an agent” prompt. Representative user constraints were: use the Week 2 project structure and `uv`; make notebooks self-contained; document the business domain with diagrams; use LangGraph, planning, multi-agent/Deep Agents, MCP, middleware, HITL, RAG with sparse+dense fusion and reranking; provide reasonable data volume; build backend/frontend/tests; and provide an exact demo. Runtime/UI/evaluator briefs converted those constraints into immutable action/version, screen-literal, and 21-scenario contracts.

Tool/model attribution is documented in [AI Coding Log](AI_CODING_LOG.md). **Codex (GPT-5 family; exact host alias not surfaced to this task)** coordinated the work; known delegated workers used `gpt-5.6-terra` and `gpt-5.6-luna`. Claude Code and Grok were available but are not claimed as executed where no invocation evidence exists.

## Iterations tried

1. Business-domain-first scope: separate official recall scope, fictional retailer operations, internal closure, and FDA termination.
2. Larger deterministic twin: expand beyond six anchors while preserving exact/probable/ambiguous/rejected and gap/zero-gap controls.
3. Hybrid-to-agentic RAG: add BM25 + LSA, RRF/reranking, then source routing, critic, rewrite, citations, and restartable budgets.
4. Multi-agent boundary hardening: fixed specialists, context quarantine, optional Deep Agents, independent verifier, no Operations capability.
5. Durable two-stage consent: split approve from execute, one action/version, exact pending binding, cross-store head/request fencing, same-key recovery.
6. Product/evaluation closure: implement five UI views and pressure-test 21 safety-critical routes including restart and concurrency.
7. Documentation/diagram contract: derive commands, labels, data counts, and demo from machine-readable artifacts and render SVGs twice.

## Learnings and observations

- Agentic RAG is safer when retrieved context is visibly advisory and deterministic data controls remain authoritative.
- Sparse and dense retrieval solve different misses; preserving their component ranks makes fusion explainable.
- Human approval is too broad unless it binds action digest, case/version, actor, and justification—and approval should still not execute.
- A task is not an acknowledgement; an acknowledgement is not disposition evidence; an internal close is not FDA termination.
- SQLite persistence alone is insufficient for safe resume. The checkpoint and Operations heads must bind one exact human command across both stores.
- A blocked closure is a stronger flagship than a happy path because it proves the system will preserve inconvenient evidence.

## Reproduce

```bash
uv sync --locked --all-groups
uv run recallops data-validate
uv run recallops demo --recall-number H-1230-2026
uv run streamlit run src/recallops/ui/app.py
```

## Timed video walkthrough

![RecallOps flagship walkthrough from investigation to blocked closure](images/recallops-five-minute-demo.png)

The visual is the presentation overview. [`demo_contract.json`](demo_contract.json), the table below, and the [reproducible demo diagram](images/07_demo_story.svg) define the exact 4:35 sequence.

| Time | Presenter narration | Screen/action |
|---|---|---|
| 00:00 | “Official scope and fictional operations are visibly separate; no public record proves Northstar involvement.” | **Command Center** → **Open case** for `H-1230-2026`. |
| 00:35 | “Bounded agentic RAG and four specialists return cited evidence; the independent critic verifies it.” | **Investigation** → **Run investigation**; retrieval/specialist/tool trace. |
| 01:20 | “The seven-part equation retains 50 unaccounted exact-lot units and ambiguity.” | **Reconciliation** → equation, rows, gaps. |
| 02:00 | “The first packet proposes only `create_case` at version zero.” | **Human Review** → **Review required**; exact form values. |
| 02:35 | “Approve writes nothing; separate confirmation records one case receipt.” | **Approve** → **Simulate approved actions** → **Simulated action recorded**, v1. |
| 03:05 | “The new version invalidates approval and requires a fresh confirmed-only hold review.” | Second `apply_inventory_hold` packet at v1. |
| 03:40 | “A second review/confirmation records exactly one simulated hold and reaches v2.” | **Approve** → **Simulate approved actions** → second receipt. |
| 04:10 | “Audit and evaluation show decisions, receipts, restart, stale rejection, and same-key recovery.” | **Audit & Evaluation** → timeline/report/recovery evidence. |
| 04:35 | “Containment does not imply closure; unresolved evidence keeps this case open.” | **Request closure** → **Open — closure blocked**. |

The full click-by-click script and optional R17 positive-close proof are in [Demo Walkthrough](DEMO_WALKTHROUGH.md).

## Scope and limitations

RecallOps is an academic simulation. It has no real ERP/WMS/POS access, real hold/notification, production identity control, real customer PII, autonomous public-health authority, deployment, 24/7 monitoring, A2A, You.com, or general web-search dependency. It is not legal or food-safety advice.
