# RecallOps Command Center — Design Specification

**Date:** 30 August 2026
**Status:** Approved for implementation
**Submission:** GenAI Academy, Mastering Agentic AI — Week 3

## 1. Executive intent

RecallOps is an evidence-first food-recall response command center. It converts a public recall notice into an auditable operational case: identify affected products and lots, trace their movement, reconcile quantities, propose containment, pause for human authorization, simulate approved actions, monitor acknowledgements, and block closure while any unit or facility remains unresolved.

This is a genuine operational problem, not a generic chatbot. The public recall notice is authoritative. The retailer's operational records are a clearly labelled synthetic digital twin because real ERP, WMS, POS, and supplier records are private. The system never implies that the fictional retailer was involved in the real recall.

The submission is designed to make every Week 3 concept visible: explicit LangGraph state and routing, planning, multi-agent specialization, an optional Deep Agent supervisor, MCP tools/resources, middleware at agent/model/tool boundaries, durable human-in-the-loop interrupts, recovery, observability, and evaluation.

## 2. Product promise

Given a recall such as openFDA recall `H-1230-2026`, RecallOps produces a reviewable answer to five questions:

1. What exact recall predicate applies: product, UPC, plant code, Julian-date range, geography, and hazard?
2. Which internal products and lots are confirmed, probable, ambiguous, or rejected matches?
3. Where did affected units go, and what is each unit's current disposition?
4. Which containment actions are justified by evidence?
5. Is the case safe to close, or what remains unresolved?

The flagship assertion is deterministic and inspectable:

`received = on_hand + quarantined + sold + returned + disposed + unaccounted`

Closure is forbidden when `unaccounted > 0`, a facility has not acknowledged, a lot match remains ambiguous, or a proposed write lacks explicit approval.

## 3. Users and decisions

| User | Need | Decision retained by the human |
|---|---|---|
| Recall coordinator | Convert notice to scoped case | Accept or edit recall predicate |
| Food-safety manager | Assess hazard and coverage | Approve containment and closure |
| Distribution operations | Trace facilities and inventory | Confirm local counts and disposition |
| Store operations | Execute holds and acknowledgements | Confirm task completion |
| Auditor/evaluator | Reconstruct why each action occurred | Judge evidence, policy, and receipts |

RecallOps is decision support. It does not contact consumers, notify regulators, place real inventory holds, or make autonomous public-health or compliance decisions.

## 4. Data boundary and provenance

### 4.1 Authoritative public inputs

- **openFDA Food Enforcement API:** live lookup and a frozen, checksummed snapshot of `H-1230-2026` for a reproducible demo. The case is Class I, ongoing, for possible *Salmonella Enteritidis*, with plant codes `P-1950` or `0840962` and Julian dates `157–184`.
- **FDA Food Traceability Rule and traceability-lot guidance:** policy resources explaining critical tracking events, key data elements, and traceability lot codes.
- **GS1 EPCIS 2.0:** semantic reference for what, when, where, why, and business-step event records.
- **USDA FoodData Central:** optional product metadata enrichment; it is not needed on the critical path.
- **USDA FSIS recall API:** future adapter only because the endpoint may reject unauthenticated runtime calls. It is excluded from the flagship path.

### 4.2 Synthetic operational digital twin

Every internal record carries `origin = "SYNTHETIC_RETAILER_DIGITAL_TWIN"` and the UI displays `SYNTHETIC — ACADEMIC DEMO`.

The deterministic dataset contains:

- one fictional regional retailer, **Northstar Grocers**;
- two distribution centers and eight stores;
- four product-master rows, including exact, near, and non-match controls;
- six lots, including one ambiguous lot that exercises HITL;
- EPCIS-style receiving, shipping, transfer, sale, return, quarantine, and disposal events;
- inventory positions, POS aggregates, supplier shipments, facility acknowledgements, cases, tasks, and audit receipts;
- at least three intentionally difficult outcomes: an ambiguous plant code, an unacknowledged store, and a quantity discrepancy.

The generator is seeded, idempotent, checksummed, and validated for referential integrity.

## 5. System architecture

### 5.1 Control plane

An explicit outer `StateGraph` owns the operational lifecycle:

`intake → plan → plan-driven sequential specialist dispatcher → verify → human review → execute approved writes → monitor → close or escalate`

The graph is the authority for state, branch decisions, retry bounds, interrupt/resume, and side effects. Each node returns typed state updates. A SQLite checkpointer preserves case state by `thread_id` so review can resume after process restart.

### 5.2 Reasoning plane

An optional Deep Agent supervisor is used only in live-model mode. It supplies planning (`write_todos`), bounded delegation, context quarantine, and case-workspace summaries. Fixed subagents are:

1. **Regulatory Intake Agent** — extracts the recall predicate and citations.
2. **Product & Lot Matching Agent** — classifies exact, probable, ambiguous, and rejected matches.
3. **Traceability Agent** — traces forward/backward events, facilities, sales, and inventory.
4. **Containment Agent** — drafts holds, tasks, and follow-ups without executing them.

A separate **Verification/Critic node** validates evidence coverage, contradictions, facility coverage, action proportionality, and quantity reconciliation. It is outside the supervisor so the system cannot grade its own work in the same context.

The deterministic offline planner implements the same task contract without a model. This makes the complete demo and evaluations credential-free while preserving a genuine Deep Agents integration for live use.

### 5.3 Action plane

Three Python FastMCP stdio servers expose one tool per action:

- **Recall Registry MCP (read-only):** `search_recalls`, `get_recall`, `get_product_metadata`; policy resources.
- **Traceability MCP (read-only):** `find_candidate_products`, `match_lots`, `trace_forward`, `trace_backward`, `get_inventory`, `get_sales`, `reconcile_units`.
- **Recall Operations MCP (write-sensitive simulation):** `create_case`, `apply_inventory_hold`, `create_facility_tasks`, `record_acknowledgment`, `record_disposition`, `close_case`.

The application supports two gateways with the same interface: `direct` for fast offline tests and `stdio` through `MultiServerMCPClient` for the actual protocol demonstration. Operational tools require an approval record, actor, justification, expected case version, and idempotency key; they return a durable audit receipt. Agents never receive raw database access.

No A2A protocol is used. All agent coordination occurs inside LangGraph; MCP is the vertical tool/data boundary.

## 6. Typed case state

`RecallCaseState` includes:

- identity: `case_id`, `thread_id`, `created_at`, `status`, `case_version`;
- input: `recall_number`, `question`, `source_mode`;
- reasoning: `plan`, `current_step`, `agent_outputs`, `tool_trace`;
- evidence: `recall`, `recall_predicate`, `candidate_products`, `candidate_lots`, `trace_events`, `affected_facilities`, `citations`, `evidence_gaps`;
- deterministic controls: `reconciliation`, `unaccounted_units`, `verification`, `retry_count`, `progress_signature`;
- action safety: `proposed_actions`, `review_packet`, `human_decision`, `write_receipts`, `acknowledgements`;
- observability: `node_trace`, `warnings`, `latency_ms`, `estimated_tokens`, `model_mode`.

State is JSON-serializable. Public and synthetic objects retain provenance at record level.

## 7. Middleware and policy

Middleware is concrete behavior, not a slide-only list:

| Boundary | Middleware | Enforced behavior |
|---|---|---|
| Agent start | Case context | Inject case ID, user role, source labels, and limits |
| Planning | Todo policy | Require a bounded plan with completion criteria |
| Model call | Model router | Choose deterministic/live model from configuration |
| Model call | Retry/fallback | Exponential retry, then deterministic extractor |
| Model call | Call budget | Stop loops after configured calls |
| Model call | Structured output | Validate agent outputs against Pydantic contracts |
| Context | Summarization | Compact large tool payloads while retaining citations |
| Tool selection | Permission policy | Read agents cannot see write tools |
| Tool call | Schema/provenance | Validate inputs and reject unlabelled observations |
| Tool call | Retry/circuit breaker | Retry transient reads; fail closed on writes |
| Tool call | Tool budget | Bound repeated searches and per-node calls |
| Tool call | PII masking | Mask customer-like fields before model/traces |
| Side effect | Approval guard | Block every write without matching approval |
| Side effect | Idempotency/version | Prevent duplicates and stale-case writes |
| Graph progress | Loop watchdog | Escalate repeated state signatures |
| Verification | Reconciliation guard | Block closure on gaps or contradictions |
| Cross-cutting | Trace recorder | Record route, node, tool, duration, status, and receipt |

The repository includes both production middleware utilities and a self-contained teaching notebook that makes hook order visible.

## 8. Human-in-the-loop lifecycle

LangGraph `interrupt()` pauses at four risk-based points:

1. when a lot match is ambiguous;
2. before inventory holds, facility tasks, or consumer-action drafts;
3. after the reviewer edits or disputes a proposed action;
4. before case closure.

The main demo uses one consolidated pre-action review and a final closure gate. The review packet shows the recall, match rationale, affected lots/facilities, quantity equation, unaccounted units, proposed actions, gaps, confidence, citations, and tool trace. The reviewer can `approve`, `edit`, `reject`, or `escalate`. Resume uses the same `thread_id` and `Command(resume=...)`.

All code before an interrupt is side-effect free or idempotent. Writes occur only after resume and use idempotency keys.

## 9. Failure behavior

| Failure | Safe response |
|---|---|
| openFDA unavailable | Use pinned snapshot and label it cached |
| MCP timeout/429 | Retry reads with bounded backoff; open circuit after threshold |
| Ambiguous lot | Pause for human; never auto-hold |
| Missing shipment event | Return partial trace and unresolved gap |
| Quantity discrepancy | Block closure |
| Store fails to acknowledge | Keep case open and generate follow-up task |
| Lost write response | Replay same idempotency key and return original receipt |
| Repeated plan/search | Progress watchdog escalates |
| Model error/budget | Preserve state and use deterministic fallback |
| Stale case version | Reject write and require refresh/review |

## 10. User experience

The Streamlit **RecallOps Command Center** has five task-oriented views:

1. **Command Center** — start the pinned recall, inspect source boundary, and see case status.
2. **Investigation** — plan, specialist outputs, match decisions, facility impact, and evidence.
3. **Reconciliation** — unit equation and gaps by product, lot, and facility.
4. **Human Review** — approve/edit/reject/escalate with explicit actor and justification.
5. **Audit & Evaluation** — tool/node timeline, write receipts, scenario scores, and failure injection.

The default mode is fully offline and deterministic. `OPENAI_API_KEY` enables live-model planning and Deep Agents. The UI must never hide whether output came from live public data, a frozen snapshot, or the synthetic twin.

## 11. Teaching notebooks

Six notebooks run first-to-last without network, keys, project imports, install cells, or reads from other repository files. Each embeds its own small data and is labelled `EDUCATIONAL — SELF-CONTAINED`:

1. LangGraph state, nodes, conditional edges, and planning.
2. MCP tools, resources, client discovery, and schema boundaries.
3. Agent/model/tool middleware and recovery.
4. Multi-agent supervisor, specialist delegation, and Deep Agents concepts.
5. Durable HITL interrupt/resume and idempotent writes.
6. End-to-end recall investigation with evaluation.

The production application is implemented separately. The notebooks explain the concepts rather than importing production code.

## 12. Evaluation and acceptance

The golden suite covers at least ten scenarios and reports machine-readable metrics:

- recall predicate extraction;
- exact product and lot matching;
- ambiguous match escalation;
- forward trace facility coverage;
- quantity reconciliation;
- missing-event gap detection;
- no write without approval;
- idempotent approved writes;
- closure blocked on gaps;
- successful close after acknowledgement;
- cached fallback; and
- loop-budget enforcement.

Acceptance gates:

- all default tests pass;
- all notebooks execute headlessly;
- Ruff formatting and lint pass;
- the deterministic evaluation passes every safety-critical scenario;
- three MCP servers initialize and expose the documented tools;
- the graph pauses and resumes with the same thread ID;
- duplicate approved writes return one logical receipt;
- documentation, diagrams, source register, demo script, and verification report match implemented behavior;
- the flagship walkthrough completes in under five minutes without credentials.

## 13. Scope lock

### Included

Live openFDA plus frozen fallback, deterministic synthetic digital twin, three logical MCP servers, explicit LangGraph workflow, optional Deep Agent supervisor, four specialists plus independent critic, middleware, durable HITL, simulated writes, Streamlit UI, CLI, failure injection, evaluation suite, self-contained notebooks, diagrams, product documentation, and exact demo script.

### Excluded

Real ERP/WMS/POS connectors, production authentication, real holds or notifications, PII, production regulatory decisions, A2A, nationwide scale, 24/7 monitoring, deployment, and FSIS as a critical dependency.

Any excluded item must remain labelled future work; it cannot be implied by diagrams, screenshots, or demo narration.
