# RecallOps Evaluation Expansion — Design Specification

**Date:** 7 September 2026  
**Status:** Approved concept; detailed design awaiting review  
**Scope:** Retrieval quality, orchestration comparison, unified scorecard, UI/demo narrative, and architecture visuals

## 1. Executive intent

RecallOps already has a strong deterministic safety evaluator: R01–R21 exercise real offline runtimes, direct/stdio MCP parity, failure injection, human authority, idempotency, durable recovery, and closure safety. The next evaluation layer must answer a different question: **does each agentic-AI technique materially improve evidence quality and task execution, or is it merely present in the architecture?**

The expansion will make five claims measurable:

1. hybrid retrieval outperforms or matches either sparse or dense retrieval alone;
2. reciprocal-rank fusion and reranking improve the ordering of relevant evidence;
3. the agentic-RAG critic/rewrite loop improves designated difficult queries while respecting hard budgets;
4. specialist orchestration produces more complete, less duplicated investigations than a bounded single-agent baseline; and
5. every score shown in the UI, demo, or diagrams is backed by a digest-verified report rather than narration.

Safety remains deterministic and fail-closed. Optional model judging and live Deep Agents comparisons are reported separately and can never authorize a write, close a case, or turn an offline safety failure into a pass.

## 2. Existing baseline retained

The current artifacts remain backward compatible:

- `data/evals/scenarios.json` and `data/evals/report.json` remain the authoritative R01–R21 safety corpus/report;
- current safety metrics and zero-tolerance mutation counters keep their existing meaning;
- the strict UI loader continues rejecting missing, malformed, stale, or digest-mismatched safety reports;
- `uv run recallops eval --report data/evals/report.json` remains a compact safety summary;
- the blocked flagship and R17 positive close remain unchanged.

The expansion adds independent reports and a combined scorecard. It does not overload the safety report with ranking or subjective quality data.

## 3. Evaluation pyramid

| Layer | Purpose | Default execution |
|---|---|---|
| Data contracts | Schema, provenance, referential integrity, corpus/report digests | Every commit |
| Retrieval benchmark | Sparse, dense, fusion, reranking, critic, rewrite, citations | Offline deterministic |
| Trajectory benchmark | Planner, specialist delegation, tool route/order, budget, stopping | Offline deterministic |
| Safety red team | Existing R01–R21 mutation/recovery/closure hard gate | Offline deterministic |
| Response-quality rubric | Groundedness, uncertainty, completeness, actionability | Deterministic facts plus optional human/model rubric |
| Live-model comparison | Deep Agents quality, latency, token and estimated cost comparison | Explicit opt-in only |

The combined offline scorecard passes only when all deterministic suite gates pass. Optional live-model results have their own status and never affect the offline safety verdict.

## 4. Retrieval benchmark corpus

Add `data/evals/retrieval_cases.json` with exactly 96 hand-auditable cases:

| Family | Count | What it pressures |
|---|---:|---|
| Exact identifiers | 24 | Recall number, UPC, plant code, lot, shipment, and facility identifiers |
| Semantic product/hazard | 16 | Paraphrases, symptoms, product descriptions, and policy terminology |
| Forward/backward lineage | 16 | Causal event chains, destinations, and parent shipments |
| Quantity/reconciliation | 16 | Seven-component equation evidence and unexplained-unit gaps |
| Cross-source provenance | 8 | Official policy/notice versus synthetic operational evidence |
| Difficult rewrite | 8 | Noisy or underspecified queries where one bounded rewrite should help |
| Abstention/adversarial | 8 | Unanswerable questions, poisoned text, and prompt-injection-like records |

Every case contains:

- stable ID, task family, question, and expected source route;
- graded relevance judgments from 0–3 keyed by document ID;
- required and prohibited document/source IDs;
- expected cited facts and explicit unanswerable status where applicable;
- whether rewrite is allowed and the expected intent after rewrite;
- per-case `top_k`, query, hop, and read budgets;
- a rationale written independently of the retrieval implementation.

The corpus and the 1,175-document knowledge index each receive canonical SHA-256 digests. A report with a mismatched case or knowledge-corpus digest is stale and cannot display as verified.

## 5. Retrieval ablation matrix and metrics

The same 96 cases run through six configurations using the existing production retrieval components:

1. `sparse_bm25`;
2. `dense_lsa`;
3. `naive_hybrid` using deterministic de-duplicated concatenation;
4. `rrf_fusion`;
5. `rrf_plus_rerank`; and
6. `agentic_rag` with critic and at most one bounded rewrite.

Per configuration and task family, report:

- Recall@1, Recall@3, and Recall@5;
- Precision@5, mean reciprocal rank, and nDCG@5;
- citation precision and required-fact coverage;
- prohibited-hit and unsupported-answer counts;
- route and abstention accuracy;
- query/hop/read budget compliance;
- p50 and p95 measured latency; and
- rewrite win/loss/no-change counts for designated rewrite cases.

Offline hard gates:

- route accuracy, abstention accuracy, provenance-label accuracy, and budget compliance equal 1.0;
- prohibited-hit and unsupported-answer counts equal zero;
- `agentic_rag` Recall@5 is at least 0.95 and nDCG@5 is at least 0.90;
- aggregate RRF Recall@5 does not regress below the stronger of sparse and dense baselines;
- reranking does not regress aggregate RRF nDCG@5;
- every metric has a denominator and every case has a persisted result—no silent skips.

Comparative deltas are shown even when zero or negative. The UI and demo must never claim an uplift that the measured report does not contain.

## 6. Orchestration and trajectory benchmark

Add `data/evals/orchestration_cases.json` with 24 cases covering recall intake, product/lot matching, lineage, reconciliation, containment drafting, evidence gaps, and safe escalation. Each case defines expected tasks, required specialist/tool families, prohibited Operations capabilities, completion criteria, evidence facts, and call budgets.

Every case runs through two credential-free profiles over the same read-only evidence boundary:

- `bounded_single_agent`: one deterministic generalist baseline that performs the complete investigation contract;
- `fixed_specialists`: the implemented planner, four specialists, and independent verifier.

An optional third profile, `deep_agents_live`, runs only through an explicit model-enabled command. It uses the existing sealed Deep Agents factory, records provider/model/prompt digests, and cannot access Operations tools.

Metrics:

- task and route accuracy;
- required-field and evidence-fact coverage;
- delegation accuracy and missing-specialist count;
- duplicate-work/tool-call ratio;
- tool-order correctness and prohibited-tool exposure;
- completion-criteria coverage and safe-stop accuracy;
- calls, measured latency, and—only when available—tokens and estimated cost.

Offline hard gates require perfect prohibited-tool isolation and budget compliance. `fixed_specialists` must not regress task success or evidence coverage below `bounded_single_agent`; all differences in latency and tool count remain visible. The optional Deep Agents profile reports `not_run_missing_credentials` rather than being silently omitted and is excluded from the offline pass calculation.

## 7. Response-quality rubric

Deterministic checks remain authoritative for recall predicate, classification, lineage, reconciliation, citations, ambiguity, blockers, and write/closure safety. A separate presentation rubric scores:

- correctness and citation alignment;
- completeness of affected scope and gaps;
- explicit uncertainty and abstention;
- operator actionability; and
- plain-language clarity.

The repository includes a human-review worksheet with anchored 1–5 examples. An optional model judge may score only presentation qualities, uses a fixed rubric/prompt digest, runs repeated blinded comparisons, and reports disagreement. It cannot grade safety invariants or modify the combined offline verdict.

## 8. Artifacts and implementation boundaries

New committed artifacts:

```text
data/evals/retrieval_cases.json
data/evals/retrieval_report.json
data/evals/orchestration_cases.json
data/evals/orchestration_report.json
data/evals/scorecard.json
docs/EVALUATION_RUBRIC.md
```

New implementation modules are isolated under `src/recallops/evaluation/`:

- retrieval-case schema and loader;
- ranking metric functions with hand-derived tests;
- ablation executor using production retriever APIs;
- orchestration-case schema and profile adapters;
- trajectory metric functions;
- digest-bound scorecard builder; and
- UI-safe report projectors that remove raw traces and hidden reasoning.

Current safety schemas/runners stay intact. Shared digest and report-validation utilities may be extracted only when tests prove identical behavior.

## 9. CLI and Make interface

The Make interface becomes:

```text
make eval-safety          regenerate R01–R21 safety report
make eval-retrieval       run the 96-case retrieval ablation
make eval-orchestration   run the 24-case offline profile comparison
make eval                 run all deterministic suites and build scorecard
make eval-summary         validate and summarize the combined scorecard
make eval-model           opt-in live Deep Agents comparison
```

`make eval-fast` runs schema/digest checks plus a stable smoke subset for ordinary iteration. `make verify` runs the complete deterministic evaluation. Existing direct CLI safety-summary behavior remains available for backward compatibility.

## 10. Product UI

The existing **Audit & Evaluation** view gains three verified sections:

1. **Safety** — current R01–R21 rates and zero-tolerance counters;
2. **Retrieval quality** — ablation table, task-family filters, Recall@5/nDCG@5, fusion delta, rerank delta, critic/rewrite outcomes, citations, and budget compliance;
3. **Orchestration quality** — single-agent versus fixed-specialist comparison, with optional Deep Agents status/results clearly separated.

Every section shows artifact timestamp, execution mode, case count, corpus digest, report digest, and verification status. Missing/stale/malformed reports show unavailable—not zero and not passed. The product shows aggregate and per-family metrics, not raw hidden reasoning or unmasked tool payloads.

## 11. Demo integration

The timed walkthrough expands from 4:35 to no more than 4:55. The existing operational story remains primary. At **Audit & Evaluation**, the presenter says:

> “Safety tests prove the system does not bypass approval or close falsely. Retrieval evals answer a different question: BM25, local LSA, RRF, reranking, and the agentic critic are run as separate ablations against the same labelled questions. The orchestration benchmark then runs the same investigations through a bounded single-agent baseline and the four-specialist workflow, so multi-agent value is measured in evidence coverage, duplicate work, tool calls, and latency—not assumed from the diagram.”

The presenter shows, in order:

1. the unchanged 21/21 safety gate;
2. the retrieval ablation row progression from sparse/dense to agentic RAG;
3. measured fusion/rerank/rewrite deltas;
4. single-agent versus specialist comparison; and
5. optional Deep Agents status, explicitly saying when live-model evaluation was not run.

The final closure request still ends **Open — closure blocked**. Evaluation never replaces the business outcome or implies production validation.

`docs/demo_contract.json`, `docs/DEMO_WALKTHROUGH.md`, `docs/SUBMISSION_DOCUMENT.md`, and presenter Q&A must use the same suite names, counts, metrics, and qualification language.

## 12. Architecture and visual integration

Add `docs/images/10_evaluation_architecture.mmd` and rendered SVG with five left-to-right layers:

`labelled corpora → suite runners → metrics/gates → digest-bound scorecard → UI/demo/CI`

It must visibly separate:

- deterministic offline safety authority;
- retrieval ablations;
- orchestration profiles;
- optional human/model presentation judging; and
- optional live Deep Agents comparison.

Update `02_system_architecture.mmd` to add an **Evaluation plane** fed by read-only traces/reports, never connected to the Operations write path. Update `03_orchestration.mmd` with trajectory capture feeding evaluation after the verifier. Update `07_demo_story.mmd` with the three-part evaluation reveal.

The polished `recallops-system-architecture.png` and `recallops-five-minute-demo.png` are edited through the image-generation tool so the presentation layer shows the same evaluation plane and ablation/comparison story. Mermaid/SVG remains the reproducible technical truth. Generation/edit prompts and refinements are appended to `docs/images/submission-visual-prompts.md`.

## 13. Testing and falsification

Implementation follows RED→GREEN tests for:

- corpus size/family balance, schema rejection, duplicate IDs, and canonical digests;
- literal hand-calculated Recall, Precision, MRR, and nDCG examples;
- retrieval ablation completeness and production-component use;
- designated cases that prove fusion, rerank, and rewrite deltas can be positive, zero, or negative;
- provenance, abstention, prohibited-hit, and budget hard gates;
- trajectory normalization, required/prohibited tools, duplicate-work calculation, and profile isolation;
- optional live profile unavailable/error states without silent skips;
- combined scorecard digest and aggregate-gate consistency;
- UI rejection of stale/missing/forged reports;
- CLI/Make targets, documentation contract, diagram render/parity, and exact demo terms.

Mutation checks must prove that changing a relevance judgment, removing a case result, forging a metric, exposing an Operations tool, or swapping a corpus digest causes the appropriate gate or UI verification to fail.

## 14. Safety and claim boundaries

- Retrieval quality does not prove regulatory correctness.
- Offline synthetic results do not prove production recall effectiveness.
- A model judge never decides safety or closure.
- Optional live-model results are nondeterministic observations, not replacements for deterministic gates.
- No evaluator receives mutation authority or Operations credentials.
- No raw chain-of-thought is stored or displayed; evaluation uses observable routes, typed outputs, citations, and tool traces.
- All Northstar data remains **SYNTHETIC — ACADEMIC DEMO**.

## 15. Acceptance criteria

The expansion is complete only when:

- all 96 retrieval and 24 orchestration cases execute with no missing results;
- all offline hard gates pass from fresh runs;
- the current R01–R21 safety gate still passes unchanged;
- scorecard/UI verification rejects stale, forged, or incomplete artifacts;
- retrieval and orchestration comparisons display measured deltas without unsupported improvement claims;
- the five-view product demonstrates the three evaluation sections using durable mode;
- the exact sub-five-minute demo script includes the evaluation narrative;
- all four affected diagrams and both polished PNGs agree with the implementation;
- notebooks/docs/Make targets explain and reproduce the evaluation system; and
- complete tests, lint, lock, security, artifact digests, and visual parity are recorded from the integrated commit.

## 16. Explicit exclusions

This increment does not add production telemetry, customer data, automatic online learning, real-world recall outcome claims, unrestricted web search, A2A, model fine-tuning, paid-provider requirements, or evaluator-controlled Operations writes.
