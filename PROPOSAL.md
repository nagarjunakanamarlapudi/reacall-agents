# RecallOps Command Center — Project Proposal

**Submission:** GenAI Academy, Mastering Agentic AI — Week 3  
**Domain:** food-recall response and traceability
**Product:** evidence-first, human-governed simulated operations

Read the [business-domain guide](docs/BUSINESS_DOMAIN.md), [business recall lifecycle](docs/images/08_business_recall_lifecycle.png), and [domain evidence model](docs/images/09_domain_evidence_model.png) first. They distinguish an FDA recall from a fictional retailer’s internal case and make human authority explicit.

## Problem worth solving

A recall coordinator must turn an imperfect notice into a precise predicate, locate potentially affected products and lots, reconstruct where units moved, reconcile quantities, contain confirmed scope, collect facility acknowledgements, and preserve an audit trail. Speed matters, but an unsupported match, lost unit, stale action, or premature closure can create more risk than delay.

RecallOps makes that work inspectable. It starts from official openFDA evidence, retrieves only cited policy and synthetic operational context, delegates bounded analysis to specialists, and keeps structured matching/reconciliation and transactional Operations checks authoritative. Agents draft; humans authorize; an approved graph node records simulated effects.

## Why this is a strong Week 3 project

| Course concept | Product evidence |
|---|---|
| State and agent loop | Explicit LangGraph nodes, conditional routes, JSON-only state, SQLite checkpoints, `thread_id`, and durable resume. |
| Planning and multi-agent work | OpenAI `write_todos` planning, a Deep Agents supervisor, four sequential context-bound LLM specialists, and a separate independent source verifier. |
| Agentic RAG | Source-aware planning, BM25 + local LSA retrieval, RRF, reranking, evidence critique, one rewrite, and hard budgets. |
| MCP | Three real FastMCP stdio servers plus an equivalent direct gateway. |
| Middleware | Retry, circuit breaker, budgets, structured validation, provenance, masking, approval, version, idempotency, fencing, telemetry, and watchdog. |
| Human in the loop | Action review and separate execution confirmation for each version; closure has its own human review. |
| Recovery | Frozen fallback, restart-safe interrupts, same-key unknown-write recovery, stale/digest rejection, and fail-closed evidence handling. |
| Evaluation | 21 offline safety scenarios, 96 retrieval cases, 24 orchestration comparisons, plus separate measured live smoke and whole-agent lifecycle proof requirements. |

## Data strategy

The official boundary is the checksummed openFDA snapshot for `H-1230-2026` plus four neighboring records and five paraphrased FDA/GS1 reference records. The operational boundary is a seeded Northstar Grocers digital twin with 48 products, 144 lots, 18 facilities, 577 events, 216 inventory positions, 144 supplier shipments, and 18 acknowledgement seeds. Together they create a 1,175-document retrieval corpus: 10 official and 1,165 synthetic.

The frozen case is reproducible offline. An optional allowlisted live lookup calls only `api.fda.gov`, has a two-second default timeout, and labels fallback. There is no You.com or general-search dependency. Public data defines scope; synthetic data exists only to demonstrate internal matching, lineage, reconciliation, and simulated operations. It never establishes retailer involvement.

## Product promise

Given a recall number and question, RecallOps makes five answers reviewable:

1. What product, UPC, plant, Julian-date, geography, and hazard predicate applies?
2. Which synthetic products/lots are exact, probable, ambiguous, or rejected matches?
3. Which facilities and events are in each lot’s forward and backward lineage?
4. Does `received = on_hand + quarantined + sold + returned + disposed + unaccounted` reconcile with source evidence?
5. What may be simulated now, and which authoritative gate prevents closure?

## Differentiating safety design

- RAG is advisory. Retrieved prose never overrides structured recall fields, lot classifications, reconciliation, or Operations transactions.
- OpenAI is the sole reasoning model in the primary product lane. Deep Agents supervises bounded LLM specialists, not write authority; none receive Operations tools. Each child must satisfy its typed completion and evidence-read contract, with at most one fixed correction before stopping.
- Approval is not execution. Every write requires action review and a second execution confirmation.
- One receipt advances exactly one case version. The deterministic flagship demonstrates `create_case` v0→v1, then `apply_inventory_hold` v1→v2; this is the expected verified-live route, not an achieved live result.
- Later actions—disposition, facility tasks, one acknowledgement at a time, and closure—use the same review/confirm/write loop.
- The checkpoint database and Operations database share a persistent owner identity and compare exact checkpoint heads/request digests before a resume can mutate state.

## Users and retained decisions

| User | RecallOps contribution | Human authority retained |
|---|---|---|
| Recall coordinator | Predicate, matches, trace, reconciliation, and gaps | Review or escalate investigation scope and request closure |
| Food-safety manager | Evidence packet and scoped proposed action | Approve, edit rationale, reject, escalate, and separately confirm execution |
| Distribution/store operations | Facility tasks and acknowledgement/disposition evidence | Confirm local observations; no autonomous inference |
| Auditor/evaluator | Checkpoints, decisions, receipts, routes, citations, and scenario report | Judge evidence and policy compliance |

## Flagship and positive control

The deterministic flagship mixed-scope case is intentionally not a happy path. `LOT-EXACT-170` retains 50 unaccounted units and `LOT-AMBIG-175` remains ambiguous, so that demonstrated lane ends **Open — closure blocked** even after two approved simulated writes. The evaluator’s scoped `LOT-PROBABLE-160` positive control has zero unaccounted units and exercises the complete disposition/tasks/acknowledgements/closure-review lifecycle. The latest real OpenAI attempt instead stopped at matching after one bounded correction: no released claims, no human review and no writes. See the [current evidence](README.md#what-is-implemented), not the expected workflow, for live status.

## Scope boundary

Included: pinned local evidence, allowlisted openFDA lookup/fallback, hybrid and agentic RAG, LangGraph control, OpenAI planning and Deep Agents LLM specialists, MCP, middleware, durable HITL, simulated Operations writes, Streamlit, CLI, evaluator, notebooks, diagrams, and reproducible docs. Credential-free deterministic demonstrations and provider fallback are separate labelled lanes, never substitute live-success evidence.

Excluded: real ERP/WMS/POS/supplier connections, real inventory action, notifications, real PII, production identity/authorization, deployment, 24/7 monitoring, autonomous health/compliance decisions, A2A, You.com, and general web search.
