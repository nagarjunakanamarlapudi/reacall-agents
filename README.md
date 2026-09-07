# RecallOps Command Center

RecallOps is an evidence-first food-recall response command center built for the GenAI Academy Week 3 project. It converts an official recall notice into an auditable simulated retailer case: retrieve cited context, match products and lots, trace movement, reconcile every unit, draft containment, stop for human authorization, record one approved simulated action per case version, and refuse unsafe closure.

The point is not “a chatbot answered a recall question.” The point is that an explicit agentic workflow can show what it knows, what it does not know, who authorized each action, and why a case must stay open.

Start with the [business-domain guide](docs/BUSINESS_DOMAIN.md), [business recall lifecycle](docs/images/08_business_recall_lifecycle.svg), and [domain evidence model](docs/images/09_domain_evidence_model.svg). They explain the operating problem before the software.

![Official openFDA evidence and synthetic retailer data remain visibly separate](docs/images/recallops-data-boundary.png)

![RecallOps system architecture: evidence, approval, and safe closure](docs/images/recallops-system-architecture.png)

The presentation visuals above are backed by the reproducible [data-provenance Mermaid diagram](docs/images/01_data_provenance.svg), [technical system diagram](docs/images/02_system_architecture.svg), and [evaluation architecture](docs/images/10_evaluation_architecture.svg).

## What is implemented

- A durable LangGraph `StateGraph` with JSON-only state, SQLite checkpoints, exact interrupt binding, restart-safe resume, and request/head fencing across the checkpoint and Operations stores.
- A deterministic planner whose validated task order drives a bounded LangGraph dispatcher: one of four specialists—Regulatory Intake, Product & Lot Matching, Traceability/Reconciliation, or Containment—runs at a time, records completion, and returns to the dispatcher. Missing, duplicate, unknown, cyclic, dependency-invalid, or over-budget plans stop closed; the independent verifier runs only after all four outputs exist. A separate optional Deep Agents factory demonstrates bounded live-model delegation without giving agents Operations tools.
- Agentic RAG over 1,175 citable records: BM25 sparse retrieval plus local TF-IDF/SVD LSA dense retrieval, reciprocal-rank fusion, deterministic reranking, an evidence critic, query rewriting, provenance checks, and hard limits of two hops, four queries, and eight reads.
- Three FastMCP servers with equivalent direct and stdio gateway surfaces: Recall Registry, Traceability, and approval-gated simulated Recall Operations.
- Middleware for context, structured validation, provenance, masking, retry, circuit breaking, budgets, approval, idempotency, versioning, progress detection, and structured traces.
- Dual consent for every write: human action review records approval but writes nothing; a separate execution confirmation powers **Simulate approved actions**. A runtime-only, checkpoint-bound broker issues the one-use execution grant; normal service and MCP consumers cannot mint authority. Exactly one operation can advance one case version.
- A five-view Streamlit command center, CLI, 21-scenario deterministic red-team evaluator, 96-case six-configuration retrieval ablation, 24-case two-profile orchestration comparison, seven self-contained teaching notebooks, and ten reproducibly rendered diagrams. The pinned integrated report records 21/21 safety scenarios passing, fusion Recall@5 delta `+0.005681818181818121`, rerank nDCG@5 delta `+0.005266955662502459`, and zero deterministic orchestration quality/tool-call uplift. Optional live Deep Agents is `not_run_missing_credentials` and excluded from offline gates; hashes and scope are in [Verification](docs/VERIFICATION.md).

## Honest data boundary

The public source is a frozen, checksummed five-row openFDA response captured from the report-date-sorted endpoint, including `H-1230-2026`. A separate exact-recall-number query was verified on 2026-09-07: it returned one flagship record matching the first frozen row field-for-field. The capture URL and verification URL remain distinct in the strict metadata receipt. An allowlisted live lookup can call only `api.fda.gov` and falls back to the labelled snapshot. The fictional Northstar Grocers digital twin contains 48 products, 144 lots, 18 facilities, 577 EPCIS-like events, 216 inventory positions, 144 supplier shipments, and 18 facility acknowledgement seeds.

Every operational record is labelled **SYNTHETIC — ACADEMIC DEMO**. The public notice does not prove that Northstar or any synthetic facility was involved. RecallOps has no You.com dependency and performs no general web search; policy and operational retrieval use the committed, checksummed corpus.

## Quick start

Prerequisites: Python 3.12, `uv`, Node 24.15.0, and npm 11.12.1.

```bash
make setup
make data-validate
make demo
make ui
```

Run `make help` to list the complete project interface. The Streamlit app defaults to the durable SQLite-backed runtime and direct MCP gateway. `RECALL_NUMBER`, `PORT`, and `RUNTIME_DIR` are configurable, for example `make ui PORT=8765 RUNTIME_DIR=.recording-runtime`.

The complete credential-free evaluation workflow is `make eval`: it regenerates the 21-case safety
suite, 96-case retrieval ablation, and 24-case orchestration comparison, then atomically builds and
validates their digest-bound combined scorecard. Use `make eval-fast` for artifact validation plus
stable metric smoke tests, or `make eval-summary` to validate the existing scorecard without
regeneration. The legacy `uv run recallops eval --report data/evals/report.json` command remains the
safety-only summary; `uv run recallops eval-scorecard` is the distinct combined summary. Optional
live orchestration requires the explicit `make eval-model LIVE_MODEL_ADAPTER=module:attribute`
opt-in and never contributes to the offline pass/fail verdict. `eval-model` imports a trusted local
Python adapter; it is not a sandbox for untrusted modules, and an API key by itself enables nothing.

To demonstrate actual stdio MCP subprocesses:

```bash
make ui-stdio
```

Optional live public lookup is deliberately narrow:

```bash
RECALLOPS_SOURCE_MODE=live uv run streamlit run src/recallops/ui/app.py
```

The durable graph itself uses the pinned snapshot for repeatable reasoning and evaluation. No API key is required. See [Operations](docs/OPERATIONS.md) for validation, evaluator, MCP, notebook, and security commands.

GitHub Actions runs the same credential-free core contract through `make ci`: locked installation,
data and artifact validation, tests, diagrams, MCP smoke, deterministic evaluation, and production
dependency/security gates. Development-only Mermaid/Puppeteer advisories are reported separately.

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
- [Evaluation](docs/EVALUATION.md) — safety, six-configuration retrieval ablation, two-profile orchestration comparison, and digest-bound scorecard.
- [Human presentation rubric](docs/EVALUATION_RUBRIC.md) — anchored review scores and authority limits.
- [Week 3 coverage](docs/CURRICULUM_COVERAGE.md) — topic-to-code/demo map.
- [Demo walkthrough](docs/DEMO_WALKTHROUGH.md) — a 4:55 presenter script with exact clicks, evaluation narration, and copy/paste inputs.
- [Submission document](docs/SUBMISSION_DOCUMENT.md) — reviewer-ready handout.
- [Verification](docs/VERIFICATION.md) — executed evidence only.

## Safety and limitations

RecallOps is academic decision support. It does not access a real ERP/WMS/POS, contact consumers or regulators, place real holds, process real customer PII, provide production authentication, or make autonomous public-health or compliance decisions. It is not legal or food-safety advice. There is no A2A protocol: LangGraph coordinates the workflow, and MCP is the vertical tool/data boundary.
