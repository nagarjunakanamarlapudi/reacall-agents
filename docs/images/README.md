# RecallOps Diagram Set

All diagrams are Mermaid sources rendered to SVG through `scripts/render_diagrams.sh`. The locked renderer requires Node `24.15.0`, npm `11.12.1`, local `@mermaid-js/mermaid-cli@11.12.0`, and `scripts/mermaid-config.json` for theme/fonts. Run `npm ci` before rendering, then run `./scripts/render_diagrams.sh --verify`. Its verify mode proves byte-identical double renders and committed-SVG parity in the recorded environment; that is the reproducibility level claimed here.

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
