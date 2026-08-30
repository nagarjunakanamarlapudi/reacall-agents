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

The work was guided by a precise product brief rather than an open-ended request. The central brief was:

> Build an evidence-first recall command center around official openFDA H-1230-2026 and a clearly labelled SYNTHETIC — ACADEMIC DEMO Northstar digital twin. Use an explicit LangGraph lifecycle, fixed specialists with an optional Deep Agent supervisor, three MCP servers, middleware, durable HITL, approval-gated simulated writes, reconciliation, monitoring, and closure blocking. Do not use A2A or direct agent writes.

Documentation-specific brief used for this handout set:

> Create product documentation, reproducible Mermaid diagrams and rendered SVGs that show data provenance, architecture, orchestration, MCP/tool safety, middleware, HITL/closure, and a presenter story. Keep the official/synthetic boundary exact, avoid claims that parallel implementation has already verified, and include copy/paste review inputs plus a final-integration confirmation note.

The prompts deliberately stated non-goals: no real retail operations, notifications, customer PII, production authorization, or claimed live verification without captured evidence. That made the output useful for a submission while preserving an honest safety boundary.

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

## Video walkthrough: 4 minutes 55 seconds

| Time | Presenter narration | Screen/action |
|---|---|---|
| 00:00–00:35 | “I am opening official openFDA recall H-1230-2026. Northstar Grocers is fictional training data, not a participant in this public recall.” | Start the pinned case and show source badges. |
| 00:35–01:00 | “The public notice defines scope. The synthetic twin lets us safely exercise the operational workflow.” | Point to official/synthetic provenance boundary. |
| 01:00–01:50 | “LangGraph plans and routes the investigation. Specialists return evidence; the independent critic verifies it. There is no A2A and agents cannot write records.” | Investigation: plan, outputs, sources, tool trace. |
| 01:50–02:35 | “This equation makes every unit visible. An unaccounted unit is a closure blocker, not a number we hide.” | Reconciliation: lot/facility quantity view. |
| 02:35–03:50 | “The ambiguous lot pauses for the Food-safety manager. The reviewer can approve, edit, reject, or escalate; approval is scoped to the action and case version.” | Human Review: use the supplied `approve` input. |
| 03:50–04:20 | “Only the approved graph node makes a simulated write, returning a receipt tied to an idempotency key.” | Show audit receipt and graph/tool timeline. |
| 04:20–04:55 | “The case remains open if acknowledgement, match certainty, or reconciliation is incomplete. Closure is a separate human gate.” | Show blocked closure and evaluation/audit panel. |

At **04:30**, pause on the closure blockers so the evaluator can see why a safe system refuses to complete a case. The total is under five minutes. Exact command flags and widget text require final integration confirmation before recording.
