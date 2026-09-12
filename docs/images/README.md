# RecallOps Visual and Diagram Set

Three presentation PNGs are the primary presentation visuals:

| File | Presentation purpose |
|---|---|
| `recallops-data-boundary.png` | Opens with the non-negotiable official/synthetic provenance boundary. |
| `recallops-system-architecture.png` | Summarizes evidence, LangGraph control, agent/tool boundaries, human review, simulated actions, and outcome. |
| `recallops-five-minute-demo.png` | Gives the audience the flagship story at a glance; the exact 4:55 timings remain in `docs/demo_contract.json` and `07_demo_story`. |

The unchanged data-boundary image retains its [original visual provenance](submission-visual-prompts.md). The architecture and demo PNGs now use deterministic fixed-geometry drawing in `scripts/render_presentation.py`, at 1920×1080 RGB. Arial on macOS or DejaVu Sans on Linux is required; byte parity is claimed within the same pinned rendering environment, not across fonts/platforms.

The supporting technical diagrams are Mermaid sources rendered to SVG and high-resolution PNG through `scripts/render_diagrams.sh`. The locked renderer requires Node `24.15.0`, npm `11.12.1`, local `@mermaid-js/mermaid-cli@11.12.0`, and `scripts/mermaid-config.json` for theme/fonts. Run `npm ci` before rendering, then run `./scripts/render_diagrams.sh --verify`. Its verify mode proves byte-identical double renders and committed SVG/PNG parity, including the two generated presentation PNGs in the recorded environment; that is the reproducibility level claimed here.

| File | Purpose |
|---|---|
| `01_data_provenance` | Separates official openFDA H-1230-2026 from fictional Northstar records. |
| `02_system_architecture` | Shows data, agentic RAG, control/reasoning, MCP, dual consent, durable stores, and outcome. |
| `03_orchestration` | Shows the exact investigation nodes, OpenAI planning, sequential LLM specialists, safe claims, verifier, and versioned action loop. |
| `04_mcp_tool_safety` | Shows read boundaries, two-stage consent, idempotency, and approval-gated simulated writes. |
| `05_middleware_lifecycle` | Shows policy hooks plus cross-store checkpoint/Operations fencing. |
| `06_hitl_closure` | Shows action review, execution confirmation, one write/version, recovery, and closure review. |
| `07_demo_story` | Gives the exact 4:55 presenter sequence and first two version transitions. |
| `08_business_recall_lifecycle` | Shows the regulator-to-retailer business lifecycle, human decisions, facility evidence, consumers, and the distinct closure boundaries. |
| `09_domain_evidence_model` | Connects public recall scope to fictional product, lot, lineage, inventory, facility, action, approval, receipt, and closure evidence. |
| `11_live_resilience` | Separates semantic stop from provider fallback and cancellation recovery. |
| `10_evaluation_architecture` | Traces the three labelled corpora through deterministic suites, metrics, digest-bound authority, verified consumers, and a separate optional advisory lane. |

## Visual truth rules

- Blue means official public data or control flow; orange denotes synthetic academic data/actions; purple denotes OpenAI LLM reasoning in the primary architecture; green denotes verified outcomes; red denotes human review/risk/blocking; gray denotes durable state or authored audit/evaluation data.
- `official openFDA H-1230-2026` and `SYNTHETIC — ACADEMIC DEMO` must remain exact labels in provenance diagrams.
- No diagram depicts A2A or a direct agent-to-database/action write.
- The evaluation plane is read-only: it has no Operations MCP credentials, SQLite write edge, or action authority. Optional live runs and human/model presentation judging remain visibly excluded from deterministic scorecard authority.
- Diagram labels and paths are reconciled to the implemented runtime; command outcomes belong in `docs/VERIFICATION.md`.
