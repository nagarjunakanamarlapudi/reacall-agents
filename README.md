# RecallOps Command Center

RecallOps is an evidence-first food-recall response command center built for the GenAI Academy Week 3 project. It converts an official recall notice into an auditable simulated retailer case: retrieve cited context, match products and lots, trace movement, reconcile every unit, draft containment, stop for human authorization, record one approved simulated action per case version, and refuse unsafe closure.

The point is not “a chatbot answered a recall question.” The point is that an explicit agentic workflow can show what it knows, what it does not know, who authorized each action, and why a case must stay open.

Start with the [business-domain guide](docs/BUSINESS_DOMAIN.md), [business recall lifecycle](docs/images/08_business_recall_lifecycle.svg), and [domain evidence model](docs/images/09_domain_evidence_model.svg). They explain the operating problem before the software.

![Official openFDA evidence and synthetic retailer data remain visibly separate](docs/images/recallops-data-boundary.png)

![RecallOps system architecture: evidence, approval, and safe closure](docs/images/recallops-system-architecture.png)

The presentation visuals above are backed by the reproducible [data-provenance Mermaid diagram](docs/images/01_data_provenance.svg) and [technical system diagram](docs/images/02_system_architecture.svg).

## What is implemented

- A durable LangGraph `StateGraph` with JSON-only state, SQLite checkpoints, exact interrupt binding, restart-safe resume, and request/head fencing across the checkpoint and Operations stores.
- A deterministic planner and four fixed specialists—Regulatory Intake, Product & Lot Matching, Traceability/Reconciliation, and Containment—followed by an independent verifier. A real Deep Agents factory demonstrates bounded live-model delegation without giving agents Operations tools; the credential-free product path remains deterministic.
- Agentic RAG over 1,175 citable records: BM25 sparse retrieval plus local TF-IDF/SVD LSA dense retrieval, reciprocal-rank fusion, deterministic reranking, an evidence critic, query rewriting, provenance checks, and hard limits of two hops, four queries, and eight reads.
- Three FastMCP servers with equivalent direct and stdio gateway surfaces: Recall Registry, Traceability, and approval-gated simulated Recall Operations.
- Middleware for context, structured validation, provenance, masking, retry, circuit breaking, budgets, approval, idempotency, versioning, progress detection, and structured traces.
- Dual consent for every write: human action review records approval but writes nothing; a separate execution confirmation powers **Simulate approved actions**. Exactly one operation can advance one case version.
- A five-view Streamlit command center, CLI, 21-scenario deterministic red-team evaluator, six self-contained teaching notebooks, and reproducibly rendered diagrams. The pinned integrated report records 21/21 scenarios and 320 assertions passing, all required rates at 1.0, and all four unsafe counters at zero; hashes and scope are in [Verification](docs/VERIFICATION.md).

## Honest data boundary

The public source is a frozen, checksummed openFDA response containing five food-enforcement records, including `H-1230-2026`. An allowlisted live lookup can call only `api.fda.gov` and falls back to the labelled snapshot. The fictional Northstar Grocers digital twin contains 48 products, 144 lots, 18 facilities, 577 EPCIS-like events, 216 inventory positions, 144 supplier shipments, and 18 facility acknowledgement seeds.

Every operational record is labelled **SYNTHETIC — ACADEMIC DEMO**. The public notice does not prove that Northstar or any synthetic facility was involved. RecallOps has no You.com dependency and performs no general web search; policy and operational retrieval use the committed, checksummed corpus.

## Quick start

Prerequisites: Python 3.12, `uv`, Node 24.15.0, and npm 11.12.1.

```bash
uv sync --locked --all-groups
npm ci
uv run recallops data-validate
uv run recallops demo --recall-number H-1230-2026
uv run streamlit run src/recallops/ui/app.py
```

The Streamlit app defaults to the durable SQLite-backed runtime and direct MCP gateway. To demonstrate actual stdio MCP subprocesses:

```bash
RECALLOPS_MCP_TRANSPORT=stdio uv run streamlit run src/recallops/ui/app.py
```

Optional live public lookup is deliberately narrow:

```bash
RECALLOPS_SOURCE_MODE=live uv run streamlit run src/recallops/ui/app.py
```

The durable graph itself uses the pinned snapshot for repeatable reasoning and evaluation. No API key is required. See [Operations](docs/OPERATIONS.md) for validation, evaluator, MCP, notebook, and security commands.

## Flagship proof

![RecallOps flagship walkthrough from investigation to blocked closure](docs/images/recallops-five-minute-demo.png)

The flagship mixed-lot case reaches human review with official citations, synthetic trace evidence, classifications, and the reconciliation equation:

`received = on_hand + quarantined + sold + returned + disposed + unaccounted`

The demo then shows two complete consent cycles:

1. approve `create_case`, separately confirm execution, and record the v0→v1 receipt;
2. review the newly planned `apply_inventory_hold`, approve it, separately confirm execution, and record the v1→v2 receipt.

The graph never batches those writes and never reuses approval after a version change. The flagship still ends **Open — closure blocked** because ambiguity and reconciliation gaps remain. A separate probable-only evaluator scenario proves that disposition, facility tasks, repeated acknowledgements, closure review, and `close_case` can reach a safe simulated close when all authoritative gates pass.

## Documentation map

- [Proposal](PROPOSAL.md) — product thesis, differentiation, and scope.
- [Architecture](docs/ARCHITECTURE.md) — control, reasoning, retrieval, MCP, and action planes.
- [Data sources](docs/DATA_SOURCES.md) — source register, counts, hashes, and network boundary.
- [MCP and tools](docs/MCP_AND_TOOLS.md) — all three servers, tools, transports, and write contract.
- [Middleware and HITL](docs/MIDDLEWARE_AND_HITL.md) — policies, dual consent, recovery, and fencing.
- [Operations](docs/OPERATIONS.md) — exact local commands and recovery runbook.
- [Evaluation](docs/EVALUATION.md) — 21-scenario matrix and hard gates.
- [Week 3 coverage](docs/CURRICULUM_COVERAGE.md) — topic-to-code/demo map.
- [Demo walkthrough](docs/DEMO_WALKTHROUGH.md) — a 4:35 presenter script with exact clicks and copy/paste inputs.
- [Submission document](docs/SUBMISSION_DOCUMENT.md) — reviewer-ready handout.
- [Verification](docs/VERIFICATION.md) — executed evidence only.

## Safety and limitations

RecallOps is academic decision support. It does not access a real ERP/WMS/POS, contact consumers or regulators, place real holds, process real customer PII, provide production authentication, or make autonomous public-health or compliance decisions. It is not legal or food-safety advice. There is no A2A protocol: LangGraph coordinates the workflow, and MCP is the vertical tool/data boundary.
