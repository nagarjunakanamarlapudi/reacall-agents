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

### Task 10 evaluation-plane edit record

Tool: **Built-in `image_gen` edit** with `docs/images/recallops-system-architecture.png` as the local edit target after visual inspection. The selected built-in output was copied into the same project path, then normalized to an opaque 1600×900 PNG.

Required evaluation-plane card copy preserved through the edit chain: **Safety — R01–R21**; **Retrieval — 96 cases / six ablations**; **Orchestration — 24 cases / single agent vs specialists**. Safety constraint: **no edge from evaluation to Operations**.

Exact evaluation edit prompt used to produce the near-final input:

> Use case: precise-object-edit. Asset type: final submission architecture infographic PNG. Input image: Image 1 is the approved near-final edit target. Primary request: Keep the entire composition, hand-drawn whiteboard style, operational flow, separate execution-confirmation box, safety boundaries, icons, colors, title, and existing evaluation cards unchanged. Make only the following evaluation-plane accuracy refinements. 1. Add a small blue-gray header above the three suite cards with the exact text “AUTHORED • LABELLED • OFFLINE EVAL DATA” and a smaller exact subtitle “not official source evidence”. 2. Inside or immediately below the Retrieval card, add the exact compact text “Δ Recall@5 +0.00568 • Δ nDCG@5 +0.00527”. 3. Inside or immediately below the Orchestration card, add the exact compact text “quality + tool-call Δ = 0 • no assumed uplift”. 4. Preserve “Optional Deep Agents / model judge — advisory” and “non-authoritative”. 5. Where the main top-row action path currently visually jumps from human review to approved graph node, add a small clear label “separate confirmation required” on that arrow, without altering the authoritative lower-box path. Critical invariants: the only mutation path remains human review → execution confirmation → approved graph node → Recall Operations MCP → simulated write → durable receipt/version. No arrow or path from any evaluation element, optional Deep Agents, model judge, scorecard, UI/demo/CI, or reports to Operations, the action path, or execution. Keep “Approve = zero writes”, “one approved write per version”, “No A2A”, and “Agents never write directly”. Solid opaque white 16:9 canvas, all text inside safe margins, no watermark, no new decorative elements.

Exact final refinement:

> Use case: precise-object-edit. Asset type: final submission architecture infographic PNG. Input image: Image 1 is the near-final edit target. Primary request: Make exactly two surgical corrections and preserve every other element, position, arrow, safety boundary, color, icon, number, label, and the solid white 16:9 composition unchanged. Correction 1: Replace the small evaluation data header with exactly “AUTHORED • LABELLED • OFFLINE EVAL DATA” (spell AUTHORED A-U-T-H-O-R-E-D), retaining the exact subtitle “not official source evidence”. Ensure the word AUTHORED is rendered correctly and clearly. Correction 2: Remove the dashed arrow between the “Optional Deep Agents / model judge — advisory” card and the Safety card. The optional advisory card must be visually isolated from all deterministic suite cards and from the scorecard. Keep “non-authoritative” directly beneath it. Do not add a replacement edge. Critical invariants: no arrow or path from optional live/model judging or any evaluation element to Operations, action execution, the approved graph node, or simulated writes. The only mutation path remains the boxed human review → execution confirmation → approved graph node → Recall Operations MCP → simulated write → durable receipt/version path. Preserve both measured delta labels and “no assumed uplift”. No watermark, no transparency, no clipped text, no other edits.

## 3. Five-minute demo story

Target file: `recallops-five-minute-demo.png`

Prompt:

> Create a landscape 16:9 hand-drawn engineering-whiteboard infographic titled “RECALLOPS — FIVE-MINUTE DEMO”. Present a clear numbered story: 1 “Problem & data truth” with official versus synthetic labels; 2 “Investigate” with hybrid agentic RAG, planner, four specialists, citations, and evidence gaps; 3 “Human review” with actor, justification, exact action, case, thread, version, and digest; 4 “Approve records zero writes”; 5 “Separate execution confirmation” creates one simulated receipt and version increment; 6 “Repeat for the next version-bound action”; 7 “Request closure”; 8 “Open — closure blocked” because of unaccounted units, an ambiguous lot, facility acknowledgements, or a pending action. Add a small recovery inset: “Lost response? Recover recorded outcome with the exact same key — never retry with a new key.” Add an evaluation badge “21/21 scenarios • 320 assertions • required rates 1.0 • unsafe counters 0”. End with “Final closure is a separate human-reviewed action.” Keep this as a presentation overview; do not print exact timestamps.

Refinement applied:

> Preserve the full demo sequence. Make “Approve records zero writes” and the separate execution confirmation visually distinct. Correct the evaluation badge to exactly “21/21 scenarios” and “320 assertions”; keep the canvas solid opaque white and all labels inside the margins.

### Task 10 evaluation-reveal edit record

Tool: **Built-in `image_gen` edit** with `docs/images/recallops-five-minute-demo.png` as the local edit target after visual inspection. The selected built-in output was copied into the same project path, then normalized to an opaque 1600×900 PNG.

Presentation claim: **Measured deltas — no assumed uplift**.

Exact evaluation edit prompt used to produce the near-final input:

> Use case: precise-object-edit. Asset type: final five-minute demo infographic PNG. Input image: Image 1 is the edit target and must remain the visual/style anchor. Primary request: Preserve the existing RecallOps demo sequence, hand-drawn engineering-whiteboard style, source truth, reconciliation gap, dual consent, simulated actions, durable audit trail, and blocked-closure ending. Expand the presentation to include an explicit 04:00 Audit & Evaluation step before the 04:45 closure request. Style/medium: same clean hand-drawn black marker lettering, rounded cards, light hatching, simple line icons, generous whitespace. Composition/framing: solid opaque white 16:9 landscape canvas; safe margins; large readable labels; clear left-to-right timeline that finishes by 04:55. Top sequence labels: “00:00 Open H-1230-2026”; “00:35 Investigate”; “01:20 Reconcile”; “02:00 Human review”; “03:05 Version-bound next action”; “04:00 Audit & Evaluation”; “04:45 Request closure”; “Open — closure blocked”. Preserve operational proof: official vs “SYNTHETIC — ACADEMIC DEMO”; agentic RAG/planner/four specialists; the seven-part reconciliation equation and “GAP 50 units”; “Approve = zero writes”; a visually separate “Execution confirmation”; create_case v0→v1 and apply_inventory_hold v1→v2 simulated receipts; exact-key recovery; final closure remains a separate human-reviewed action. Within the Audit & Evaluation panel, create three visible proof cards: 1. “Safety — 21/21 scenarios” and “unsafe counters 0”. 2. “Retrieval — 96 cases / six ablations” and “BM25 → LSA → RRF → rerank → critic/rewrite”. Add “Δ Recall@5 +0.00568” and “Δ nDCG@5 +0.00527”; add “rewrite: 0 win • 0 loss • 8 unchanged”. 3. “Orchestration — 24 cases” and “bounded single agent vs four specialists”; add “quality + tool-call Δ = 0”. Add a small separate dotted-outline advisory badge with exact text “Optional Deep Agents: not_run_missing_credentials” and “excluded from offline gates”. It must not feed Operations or any action path. Add one prominent line: “MEASURED DELTAS — NO ASSUMED UPLIFT”. Evaluation data is authored, labelled, digest-bound offline audit data, not official source evidence. Evaluation is read-only and has no path to Operations or execution. Recovery inset text: “Lost response? Recover with the exact same key.” Critical constraints: Keep Approve separate from execution confirmation. Do not depict approval itself as a write. Draw no evaluation-to-Operations/action edge. Do not imply optional Deep Agents was run. Do not claim an orchestration uplift. No logos, watermark, transparency, dark background, tiny paragraphs, misspellings, or clipped text.

Exact final refinement:

> Use case: precise-object-edit. Asset type: final five-minute demo infographic PNG. Input image: Image 1 is the near-final edit target. Primary request: Preserve the entire image, timeline, evaluation cards, measured numbers, authored/offline boundary, colors, icons, blocked ending, and solid white 16:9 composition. Make only these three text/meaning corrections. 1. In the 02:00 Human review card, replace the two red labels with exactly “1 Approve (zero writes)” and “2 Separate execution confirmation”. Keep them visually distinct. 2. Replace the orange dashed heading “Execution confirmation (read-only)” beneath the version-bound action card with exactly “DUAL-CONSENT RECEIPTS”. Keep the v0→v1 create_case and v1→v2 apply_inventory_hold receipt lines unchanged. Never call execution confirmation read-only. 3. In the 04:45 Request closure card, replace “Approve = zero writes” with exactly “Closure is a separate decision”. Keep the lost-response recovery inset unchanged. Critical invariants: approval records zero writes; separate execution confirmation authorizes the exact simulated action; evaluation remains read-only, is not official source evidence, and has no path to Operations or execution; optional Deep Agents remains not_run_missing_credentials and excluded from offline gates; no orchestration uplift claim. No other edits, no watermark, no transparent background, no clipped text.

## Truth and review record

- The PNGs are narrative presentation assets, not executable architecture specifications.
- Exact runtime order and timings are defined in `docs/demo_contract.json` and `docs/DEMO_WALKTHROUGH.md`.
- Technical parity is enforced on the Mermaid/SVG set through `scripts/render_diagrams.sh --verify`.
- Final PNGs were visually inspected at 1600×900 and a practical 800×450 downscale for layout, opacity, readable primary labels, source-boundary accuracy, measured-delta accuracy, optional-live status, and safe-action sequencing.
