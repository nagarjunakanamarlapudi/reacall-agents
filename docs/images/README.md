# RecallOps Diagram Set

All diagrams are Mermaid sources rendered to deterministic SVG through `scripts/render_diagrams.sh`. The renderer pins `@mermaid-js/mermaid-cli@11.12.0`; sources and SVGs are committed together so a reviewer can inspect either.

| File | Purpose |
|---|---|
| `01_data_provenance` | Separates official openFDA H-1230-2026 from fictional Northstar records. |
| `02_system_architecture` | Separates control, reasoning, and action planes. |
| `03_orchestration` | Shows StateGraph, specialists, optional Deep Agent supervisor, and independent critic. |
| `04_mcp_tool_safety` | Shows read boundaries and approval-gated simulated writes. |
| `05_middleware_lifecycle` | Shows policy hooks around agent, model, tool, and graph boundaries. |
| `06_hitl_closure` | Shows durable interrupts, resume, approved writes, monitoring, and closure blocks. |
| `07_demo_story` | Gives the presenter’s concise end-to-end sequence. |

## Visual truth rules

- Blue means official public data or control flow; orange denotes synthetic academic data; purple denotes reasoning; green denotes verified/approved flow; red denotes block/fail-closed behavior.
- `official openFDA H-1230-2026` and `SYNTHETIC — ACADEMIC DEMO` must remain exact labels in provenance diagrams.
- No diagram depicts A2A or a direct agent-to-database/action write.
- Diagrams describe the approved design; operational verification belongs to the final integrated run.
