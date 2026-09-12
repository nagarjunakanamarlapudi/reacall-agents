# RecallOps Visual and Diagram Set

Three presentation PNGs are the primary presentation visuals:

Audience-facing README, proposal, business-domain, architecture and runbook links open PNGs first. Mermaid `.mmd` and generated `.svg` files in this directory remain secondary reproducible source/parity artifacts, not the default presentation link targets. The architecture shows OpenAI as the sole reasoning model; deterministic RAG policies, child gates, source verification and HITL are controls. The demo visual explicitly distinguishes the expected verified route from the latest failed live attempt and the separate deterministic consent demonstration.

| File | Presentation purpose |
|---|---|
| `recallops-data-boundary.png` | Opens with the non-negotiable official/synthetic provenance boundary. |
| `recallops-system-architecture.png` | Summarizes evidence, LangGraph control, agent/tool boundaries, human review, simulated actions, and outcome. |
| `recallops-five-minute-demo.png` | Gives the audience the flagship story at a glance; the exact 4:55 timings remain in `docs/demo_contract.json` and `07_demo_story`. |

The unchanged data-boundary image retains its [original visual provenance](submission-visual-prompts.md). The architecture and demo PNGs use deterministic fixed-geometry drawing in `scripts/render_presentation.py`, at 1920×1080 RGB. The repository bundles [DejaVu Sans 2.37 regular/bold, license and digest manifest](../../scripts/fonts/dejavu-2.37/README.md). macOS and Ubuntu load the same verified font bytes with Pillow's explicit BASIC layout engine; missing or modified fonts fail closed. No operating-system font fallback is permitted. Pillow is pinned by `uv.lock`; use the locked environment when regenerating these artifacts.

The supporting technical diagrams are Mermaid sources rendered to SVG and high-resolution PNG through `scripts/render_diagrams.sh`. The authored package toolchain remains Node `24.15.0`, npm `11.12.1`, and local `@mermaid-js/mermaid-cli@11.12.0`. Actual artifact rendering uses the official Mermaid CLI `11.12.0` Linux image, fixed to `linux/amd64` and immutable digest `ghcr.io/mermaid-js/mermaid-cli/mermaid-cli@sha256:bad64c9d9ad917c8dfbe9d9e9c162b96f6615ff019b37058638d16eb27ce7783`. This pins Chromium, system fonts, layout and rasterization together. The image's internal Node runtime is `18.20.5`, independent of the authored Node toolchain; it is development-only, receives only repository diagram sources/configuration, and runs with `--network none`, a read-only source mount and caller-owned output. Do not use it to process untrusted diagrams or serve application traffic.

Install/start Docker Desktop (macOS, including amd64 emulation on Apple Silicon) or Docker Engine (Linux). Run `make setup`: it installs locked Python/Node dependencies, checks daemon access and architecture support, and pulls the immutable renderer if missing. Ubuntu hosted CI performs the same runtime preflight before `make ci`. No image download is needed after this cache is populated; container rendering has no network access. If setup reports a missing/stopped daemon, start Docker and retry; on Linux ensure your account can access the daemon. If the image cannot be fetched, restore access to GHCR and retry. The application and live demo do not require Docker.

The image contains Mermaid library `11.12.0`; the local `package-lock.json` currently resolves library `11.17.2` under CLI `11.12.0`. They are separate locked development environments, not interchangeable artifact renderers. The image's older Node/runtime dependencies are not covered by the local npm audit; its use is limited to these trusted, committed diagram inputs. A future renderer upgrade must update the digest and regenerate/review all technical artifacts together.

Run `./scripts/render_diagrams.sh` to regenerate; run `make diagrams` or `./scripts/render_diagrams.sh --verify` to prove byte-identical double renders and committed SVG/PNG parity, including both generated presentation PNGs. Host-native Mermaid is not a canonical alternative: an actual macOS/Linux probe produced different layout dimensions with the same CLI version, so SVG identifier normalization alone cannot establish PNG parity. Updating the image digest, fonts, Pillow or sources requires regeneration, parity tests and visual inspection of every affected PNG.

The presentation PNG writer fixes RGB scanlines, chunk order and stored DEFLATE blocks, avoiding platform-dependent Pillow/zlib compression. The files are larger (about 6.2 MB each); exact PNG byte comparison remains the gate.

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
