# RecallOps Architecture

The [business-domain guide](BUSINESS_DOMAIN.md), [business recall lifecycle](images/08_business_recall_lifecycle.svg), and [domain evidence model](images/09_domain_evidence_model.svg) define the operating problem. This document maps that problem onto the implemented control, retrieval/reasoning, tool, state, and action boundaries.

![RecallOps system architecture: evidence, approval, and safe closure](images/recallops-system-architecture.png)

The presentation visual summarizes the boundaries. The reproducible [technical architecture](images/02_system_architecture.svg), [orchestration](images/03_orchestration.svg), [MCP safety](images/04_mcp_tool_safety.svg), and [middleware lifecycle](images/05_middleware_lifecycle.svg) diagrams carry the implementation detail.

## Architectural thesis

RecallOps separates evidence gathering from operational authority:

- read paths may retrieve, rank, trace, and propose;
- the graph owns ordering, state, interrupts, and retries;
- the independent verifier checks deterministic invariants;
- a human authorizes one exact action at one exact case version;
- a second human confirmation permits the approved graph node to call Operations MCP once;
- Operations SQLite revalidates approval, version, idempotency, evidence, and closure inside the transaction.

No agent, retrieved document, UI callback, or MCP transport can skip those layers.

## Component map

| Plane | Implemented components | Authority |
|---|---|---|
| Data | Frozen openFDA snapshot, five policy references, seeded Northstar twin, manifests/checksums | Defines evidence and provenance; never authorizes a write |
| Retrieval | BM25, TF-IDF/SVD LSA, RRF, deterministic rerank, source routing, evidence critic, bounded rewrite | Advisory cited context only |
| Reasoning | Deterministic planner, four specialists, optional Deep Agents factory, independent verifier | Produces typed facts, assessments, and proposals |
| Control | LangGraph `StateGraph`, conditional edges, JSON-only state, `interrupt()`, `Command(resume=...)` | Owns lifecycle, side-effect order, and human pauses |
| Tool | Three FastMCP servers; direct and stdio gateways | Exposes narrow typed reads and simulated writes |
| Durable state | LangGraph checkpoint SQLite plus Operations SQLite | Persists graph state, case versions, approvals, tasks, acknowledgements, receipts, and fences |
| Product | Streamlit five-view command center and CLI | Presents state and invokes only the runtime adapter |
| Assurance | 21-scenario evaluator, verified report projection, unit/integration/UI/docs/notebook tests | Measures observable safety contracts; stale/missing/invalid reports never claim success |

## Agentic RAG

The local corpus has 1,175 documents and a pinned corpus digest. Each query runs two independent retrieval components:

1. BM25-style sparse search preserves exact identifiers and domain terms.
2. Local TF-IDF plus 64-dimensional truncated-SVD LSA provides dense semantic similarity without an embedding API.
3. Reciprocal-rank fusion combines sparse and dense ranks.
4. A deterministic reranker rewards identifier/term/source/intent matches and retains component explanations.
5. A source-aware critic checks whether the returned official/synthetic evidence covers identifiers and domain concepts and whether advisory claims contradict supplied authoritative facts.
6. If needed, the retriever performs one bounded rewrite and stops after at most two hops, four queries, or eight reads. Its JSON state can be serialized at a read boundary.

The agentic retrieval graph has two sealed capabilities: regulatory search routes to official evidence on Recall Registry MCP, and operational search routes to synthetic evidence on Traceability MCP. It has no Operations capability. The main workflow exposes RAG citations and gaps in the review packet but keeps structured predicate matching, trace/reconciliation, and Operations closure gates authoritative.

## Planning and agents

The credential-free runtime calls a deterministic bounded planner, then the four fixed specialists in sequence:

1. Regulatory Intake extracts the recall predicate and citations.
2. Product & Lot Matching classifies exact, probable, ambiguous, and rejected candidates.
3. Traceability/Reconciliation follows forward/backward lineage, inventory, and unit evidence.
4. Containment drafts a scoped proposal without executing it.

The independent verifier sits outside the specialist context and checks overlap, facility coverage, and authoritative-control ownership. The repository also builds a real Deep Agents graph with `write_todos`, fixed subagent registry, context quarantine, and read-only tools. It is an optional live reasoning component and is not invoked by the default durable workflow; it cannot see Operations tools.

![Orchestration and action loop](images/03_orchestration.svg)

## Durable LangGraph lifecycle

The investigation sequence is:

`START → intake → retrieve_context → plan → regulatory_intake → product_lot_match → trace_forward_backward → reconcile → containment_draft → verify → prepare_action_review → action_review`

After that, the graph repeats a versioned action loop:

`action review → approval recorded (zero writes) → execution confirmation → one Operations call → receipt/version increment → recompute next action → next review`

The first two flagship cycles are fixed by evidence and case state:

- `create_case` v0→v1;
- `apply_inventory_hold` v1→v2.

If ambiguity or an unresolved gap remains, planning stops `open_closure_blocked`. A closeable scope may continue through `record_disposition`, `create_facility_tasks`, one `record_acknowledgment` per facility, `closure_review`, execution confirmation, and `close_case`. `edit` may change rationale only and re-enters verification; `reject` leaves the case open; `escalate` ends safely.

## Two durable stores and mutation fencing

`RecallOpsRuntime.open()` keeps `AsyncSqliteSaver` open for the runtime lifetime. The checkpoint SQLite database stores a randomly generated persistent owner UUID, graph checkpoints, and an exact mutation marker. Operations SQLite binds the same case/thread to that checkpoint-store owner and tracks the current checkpoint head.

Every start/resume computes a SHA-256 request digest over case ID, thread ID, expected checkpoint head, interrupt kind, normalized human response, action/digest, execution ID, idempotency key, and initial payload where applicable. A mutation must claim the exact Operations head and matching checkpoint marker before the graph advances. Wrong case/thread/version/action/key, a copied checkpoint store, a stale head, a changed request, or a concurrent mutation fails without altering trusted state. Recovery reconciles a prepared marker only when both stores prove the same request and head transition.

The public runtime returns an immutable `RuntimeResult` containing detached JSON case state, one pending interrupt if present, next nodes, and the persistent LangGraph checkpoint ID. Restarting over the same two database paths resumes at the pending interrupt without rerunning completed reasoning nodes.

## MCP and transport

Recall Registry and Traceability are read-only. Recall Operations is simulated write-sensitive. `DirectGateway` and `StdioMCPGateway` implement the same async methods; stdio uses `MultiServerMCPClient` and three Python subprocess servers. Runtime configuration chooses the transport, not a weaker policy. See [MCP and Tools](MCP_AND_TOOLS.md).

## Network boundary

The default graph, retrieval corpus, evaluator, notebooks, and demo work offline. No You.com or general web search is used. The only optional live public-data call is the hard-coded openFDA Food Enforcement endpoint on `api.fda.gov`; it has a bounded timeout and labelled frozen fallback. Optional Deep Agents model invocation is separate from public-data search and is not required for the product walkthrough.

## Trust boundaries and non-goals

- Official openFDA/policy evidence and fictional Northstar evidence remain distinct on every record.
- Retrieved text is untrusted data, never executable instructions or transactional truth.
- There is no A2A; LangGraph is the only agent coordinator.
- Agents have no raw database or Operations tool access.
- The UI is not an authorization system; the runtime and Operations transaction enforce the contract.
- Internal synthetic case closure is not FDA recall termination.
- No real hold, task, acknowledgement, disposition, notification, or closure occurs.
