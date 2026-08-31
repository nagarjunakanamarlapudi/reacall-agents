# RecallOps Submission Visual Prompts

These are the generation prompts for the three primary PNG presentation assets. The built-in image-generation tool produced the images; the committed Mermaid/SVG set remains the reproducible technical detail layer.

## Shared art direction

Create a polished hand-drawn engineering-whiteboard infographic on a solid opaque white 16:9 canvas. Use large, highly legible marker lettering, generous spacing, rounded cards, restrained arrows, and simple operational icons. Keep the visual language consistent: blue for official evidence/control flow, orange for synthetic academic data and simulated actions, purple for agent reasoning, red for human review/risk, green for verified outcomes, and gray for durable state/audit. No logos, watermarks, photorealism, dark background, tiny paragraphs, unexplained abbreviations, or text outside the safe margins.

## 1. Data boundary

Target file: `recallops-data-boundary.png`

Prompt:

> Create a landscape 16:9 hand-drawn engineering-whiteboard infographic titled “RECALLOPS — KNOW WHAT IS REAL”. Split the canvas into two unmistakable evidence zones. On the left, a blue zone labelled exactly “OFFICIAL — openFDA snapshot” with recall number “H-1230-2026”, a public recall notice icon, and the rules “public scope and dates”, “live-preferred lookup”, and “frozen fallback”. On the right, an orange zone labelled exactly “SYNTHETIC — ACADEMIC DEMO” with fictional Northstar product, lot, shipment, facility, inventory, acknowledgement, and event records. Add the sentence “The public notice does not prove Northstar involvement.” Show both zones feeding an evidence ledger while preserving their labels. Across the bottom show “48 products • 144 lots • 18 facilities • 577 events • 1,175 retrieval documents”. Add “No You.com or general web search” and “Optional live host: api.fda.gov only”. Make provenance the dominant message.

Refinement applied:

> Preserve the composition and wording. Ensure the bottom counts are fully visible, render “SYNTHETIC — ACADEMIC DEMO” exactly, remove any accidental transparency, and make the entire background solid white.

## 2. Complete system architecture

Target file: `recallops-system-architecture.png`

Prompt:

> Create a landscape 16:9 hand-drawn engineering-whiteboard infographic titled “RECALLOPS — EVIDENCE TO SAFE ACTION”. Show a left-to-right system with four clearly separated layers. Layer 1, evidence: official openFDA snapshot/live-preferred lookup and fictional Northstar digital twin, retaining their exact blue/orange source labels. Layer 2, agentic RAG: BM25 sparse search plus local LSA dense search, reciprocal-rank fusion, deterministic reranking, critic/rewrite, citations and gaps, with limits “2 hops • 4 queries • 8 reads”. Layer 3, LangGraph control: deterministic planner, four specialists named “Regulatory Intake”, “Product & Lot Matching”, “Traceability”, and “Containment”, optional “Deep Agents supervisor”, plus an independent verifier. Layer 4, tool and authority boundary: three FastMCP servers named “Recall Registry”, “Traceability”, and “Recall Operations”; direct and stdio transports; middleware around agent, model, retrieval, and tool calls; SQLite checkpoints and Operations ledger; human review followed by a separate execution confirmation. Show the outcome “Open — closure blocked” when evidence gaps remain. Include the rules “No A2A”, “Agents never write directly”, “Approve = zero writes”, and “one approved write per version”. Agents must reach operations only through approved MCP tools and both human gates.

Refinement applied:

> Preserve all architecture labels and the hand-drawn style. Correct any arrow that implies direct agent-to-database or agent-to-operation access. The only mutation path must be human review → execution confirmation → Recall Operations MCP → durable receipt/version. Keep all text large and the background opaque white.

## 3. Five-minute demo story

Target file: `recallops-five-minute-demo.png`

Prompt:

> Create a landscape 16:9 hand-drawn engineering-whiteboard infographic titled “RECALLOPS — FIVE-MINUTE DEMO”. Present a clear numbered story: 1 “Problem & data truth” with official versus synthetic labels; 2 “Investigate” with hybrid agentic RAG, planner, four specialists, citations, and evidence gaps; 3 “Human review” with actor, justification, exact action, case, thread, version, and digest; 4 “Approve records zero writes”; 5 “Separate execution confirmation” creates one simulated receipt and version increment; 6 “Repeat for the next version-bound action”; 7 “Request closure”; 8 “Open — closure blocked” because of unaccounted units, an ambiguous lot, facility acknowledgements, or a pending action. Add a small recovery inset: “Lost response? Recover recorded outcome with the exact same key — never retry with a new key.” Add an evaluation badge “21/21 scenarios • 320 assertions • required rates 1.0 • unsafe counters 0”. End with “Final closure is a separate human-reviewed action.” Keep this as a presentation overview; do not print exact timestamps.

Refinement applied:

> Preserve the full demo sequence. Make “Approve records zero writes” and the separate execution confirmation visually distinct. Correct the evaluation badge to exactly “21/21 scenarios” and “320 assertions”; keep the canvas solid opaque white and all labels inside the margins.

## Truth and review record

- The PNGs are narrative presentation assets, not executable architecture specifications.
- Exact runtime order and timings are defined in `docs/demo_contract.json` and `docs/DEMO_WALKTHROUGH.md`.
- Technical parity is enforced on the Mermaid/SVG set through `scripts/render_diagrams.sh --verify`.
- Final PNGs were visually inspected for layout, opacity, readable labels, source-boundary accuracy, and safe-action sequencing.
