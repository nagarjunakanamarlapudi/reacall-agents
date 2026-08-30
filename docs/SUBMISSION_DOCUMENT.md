# RecallOps Command Center — Submission Handout

## Project overview

RecallOps is an evidence-first academic food-recall response command center. It converts a public recall notice into an auditable case: extract the scope, match internal products/lots, trace units and facilities, reconcile quantity, propose containment, pause for human authorization, simulate approved actions, and block closure while risk remains.

The product is intentionally decision support. It does not notify regulators or consumers, place real holds, access production inventory systems, handle real customer PII, or make autonomous public-health decisions. LangGraph owns the case lifecycle. MCP supplies narrow information and action interfaces. There is no A2A and agents do not directly write operational data.

## Datasets used

| Dataset | How it is used | Boundary |
|---|---|---|
| official openFDA `H-1230-2026` | Authoritative recall predicate and public notice evidence | Live lookup with frozen snapshot for reproducible offline demonstration |
| FDA traceability guidance | Recall/traceability policy context | Official reference, not retailer data |
| GS1 EPCIS 2.0 | Event-semantics reference | Official semantic reference |
| USDA FoodData Central | Optional product enrichment | Not required on the critical path |
| Northstar Grocers records | Products, lots, events, inventory, acknowledgements, tasks, receipts | `SYNTHETIC — ACADEMIC DEMO`; fictional digital twin, not connected to the public recall |

The essential provenance statement is: openFDA is the public source that defines recall scope; Northstar is a controlled fictional dataset used to demonstrate operations. A public notice never proves the fictional retailer was involved.

## Vibe-coding prompts and briefs

The following are verbatim project instructions, quoted from the approved design/specification and Task 10 brief. They are user-authored project text, not synthesized prompts.

> An explicit outer `StateGraph` owns the operational lifecycle: `intake → plan → specialist fan-out → reconcile → verify → human review → execute approved writes → monitor → close or escalate`
>
> The graph is the authority for state, branch decisions, retry bounds, interrupt/resume, and side effects. Each node returns typed state updates. A SQLite checkpointer preserves case state by `thread_id` so review can resume after process restart.

> ### Task 10: Product documentation and reproducible diagrams
>
> **Files:** `README.md`, `PROPOSAL.md`, `docs/{ARCHITECTURE,DATA_SOURCES,MCP_AND_TOOLS,MIDDLEWARE_AND_HITL,OPERATIONS,EVALUATION,DEMO_WALKTHROUGH,SUBMISSION_CHECKLIST,BACKLOG}.md`, `docs/images/*.mmd`, `docs/images/*.svg`, `scripts/render_diagrams.sh`, `tests/docs/test_documentation.py`
>
> **Produces:** Source-boundary, system architecture, orchestration, MCP/tool safety, middleware lifecycle, HITL lifecycle, and demo-story diagrams plus exact narration and copy-paste prompts.
>
> - [ ] Test that every promised artifact exists, diagrams contain scope-critical labels, docs contain no placeholder language, and demo commands/inputs match the CLI/UI.
> - [ ] Write Mermaid sources, render SVG with pinned Mermaid CLI, and visually inspect every SVG.
> - [ ] Write docs from implemented behavior, including honest limitations and evaluator Q&A; run tests and commit.

**Tool/model and iteration attribution:** this documentation pass used Codex in the desktop task environment; Mermaid CLI `11.12.0` generated the SVGs. Claude Code and the Grok CLI were not invoked by this documentation agent. The initial source/label contract, direct render, visual-layout review, and double-render stability check are the recorded iterations; runtime integration remains an approved contract pending Task 11.

## Iterations tried

1. **Architecture-first pass:** organized the product into control, reasoning, and action planes so orchestration does not blur into tool access.
2. **Provenance pass:** added exact public/synthetic labels in the source register and every source-boundary diagram.
3. **Safety pass:** made “no A2A” and “no direct agent writes” visible in diagrams and documentation rather than leaving them implicit.
4. **Demo pass:** turned the flow into presenter narration with role, decisions, and safe closure-blocking outcome rather than a generic feature tour.
5. **Documentation-contract pass:** wrote a test before the artifacts and added contract coverage for artifact names, labels, placeholders, and demo inputs.

## Learnings and observations

- Provenance must be designed into every view; one disclaimer at the start does not stop a reader from mistaking synthetic activity for a real recall response.
- A graph and MCP solve different problems. The graph controls lifecycle and review pauses; MCP narrows capability and makes tool activity inspectable.
- A deterministic path is essential for a short, credential-free evaluation. Live-model orchestration is valuable as an optional contrast, not as the only way the demo works.
- Human review is most persuasive when the review packet contains the reconciliation equation, source citations, gaps, and the exact action proposed.
- Closure is a better safety demonstration than a successful write: refusing to close with ambiguity, missing acknowledgement, or missing units explains the system’s operational discipline.

## Video walkthrough: 4 minutes 20 seconds

| Time | Presenter narration | Screen/action |
|---|---|---|
| 00:00 | “I am opening official openFDA recall H-1230-2026. Northstar Grocers is fictional training data, not a participant in this public recall.” | Command Center: **Open case** with **Recall number** `H-1230-2026`. |
| 00:35 | “LangGraph runs bounded investigation work; specialists return evidence and the critic verifies it. There is no A2A and agents cannot write records.” | Investigation: **Run investigation**, plan, outputs, sources, tool trace. |
| 01:20 | “This equation makes every unit visible. An unaccounted unit is a closure blocker.” | Reconciliation: lot/facility quantity view and gaps. |
| 02:00 | “The ambiguous lot pauses for the Food-safety manager.” | Human Review: **Review required**; **Decision** `approve`, **Actor** `Food-safety manager`, and the contract **Justification**. |
| 03:10 | “Only the approved graph node makes a simulated write.” | **Approve**, **Simulate approved actions**, receipt, and **Simulated action recorded**. |
| 04:20 | “Closure is separate and stays blocked while risk remains.” | Audit & Evaluation: **Request closure** and **Open — closure blocked**. |

The total is four minutes twenty seconds. [`demo_contract.json`](demo_contract.json) is the approved contract pending Task 11 runtime integration; the final integration test must compare it with runtime CLI/UI behavior before recording.
