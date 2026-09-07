# Submission Checklist

Use this as the final reviewer/recording gate. A checked box means the referenced artifact or executed evidence is present in the integrated branch; command outcomes belong in [Verification](VERIFICATION.md).

## Business and provenance

- [x] The opening slide explains recall predicate, lot lineage, reconciliation, containment, acknowledgement, and the difference between internal closure and FDA termination.
- [x] `H-1230-2026` is labelled official openFDA live or snapshot; every Northstar row says **SYNTHETIC — ACADEMIC DEMO**.
- [x] The narration explicitly says the public notice does not prove Northstar involvement.
- [x] Data counts match the manifests: 5 public snapshot records; 48 products, 144 lots, 18 facilities, 577 events, 216 inventory positions, 144 shipments, 18 acknowledgement seeds; 1,175 retrieval documents.
- [x] No You.com/general-search claim appears; optional live data is limited to `api.fda.gov` with labelled fallback.

## Week 3 architecture

- [x] LangGraph node order, JSON state, SQLite checkpoint, persistent checkpoint ID, `thread_id`, interrupt, and resume are visible.
- [x] Agentic RAG visibly includes BM25 sparse, local LSA dense, RRF, reranking, critic/rewrite, citations/gaps, and 2-hop/4-query/8-read limits.
- [x] The deterministic planner, four specialists, optional Deep Agents factory, and independent verifier are accurately distinguished.
- [x] Recall Registry, Traceability, and Recall Operations FastMCP servers plus direct/stdio transports are demonstrated.
- [x] Middleware covers reads, model fallback, retrieval, masking, tracing, approvals, versions, idempotency, watchdog, and cross-store fencing.
- [x] “No A2A” and “agents never write directly” appear in architecture/narration.

## Human authority and lifecycle

- [x] **Approve** records zero writes and exposes a separate **Simulate approved actions** confirmation.
- [x] First receipt is `create_case` v0→v1; the next fresh review is `apply_inventory_hold` v1→v2.
- [x] Edit/reject/escalate behavior is explained; ambiguous scope is never held.
- [x] Full later lifecycle is documented: evidence-bound disposition, facility tasks, repeated one-facility acknowledgements, closure review, execution confirmation, close.
- [x] Lost-response recovery reuses the exact key; wrong case/thread/version/action/key leaves state unchanged.
- [x] Flagship finishes **Open — closure blocked**; R17 is shown only as an optional positive control.

## Product and evaluation

- [x] All five UI views and exact controls/fields/statuses in `demo_contract.json` are visible.
- [x] CLI data validation/demo/MCP config/eval summary and Streamlit startup exit successfully.
- [x] The generated evaluator report contains R01–R21, all safety-critical, `gate_passed=true`, perfect required rates, and zero unsafe counters.
- [x] The 96-case retrieval report includes all six configurations and shows the measured fusion/rerank/rewrite deltas without extrapolating production uplift.
- [x] The 24-case orchestration report compares bounded single agent and four specialists; zero observed quality/tool-call deltas and `not_run_missing_credentials` live status remain visible.
- [x] The digest-bound combined scorecard verifies all three offline suites; optional live/model judging is excluded from deterministic authority.
- [x] Seven self-contained notebooks rebuild and execute without product imports, network, install cells, or credentials.
- [x] The full test suite, Ruff, lock, dependency, Bandit, pip-audit, Streamlit smoke, MCP stdio smoke, and data checks are recorded.

## Documentation and recording

- [x] The presentation uses [data boundary](images/recallops-data-boundary.png), [system architecture](images/recallops-system-architecture.png), and [five-minute demo](images/recallops-five-minute-demo.png) visuals; [Diagram 10](images/10_evaluation_architecture.svg) preserves the read-only evaluation truth.
- [x] README, proposal, business guide, architecture, source register, MCP/HITL/operations/evaluation docs, submission document, backlog, demo, and verification are synchronized.
- [x] All ten Mermaid sources render through the pinned local CLI; two fresh renders match each other and committed SVGs.
- [x] The 4:55 walkthrough is rehearsed with the exact copy/paste card, exact 45-second evaluation narration, and a fresh explicit runtime directory.
- [x] No secret, real PII, unlabelled synthetic claim, production action claim, unsupported test count, or stale integration-status wording remains.
