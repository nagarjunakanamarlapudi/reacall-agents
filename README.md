# RecallOps Command Center

RecallOps is an evidence-first academic command center for investigating food recalls. It turns one authoritative notice into a reviewable operational case: identify the recall predicate, match products and lots, trace units, reconcile quantities, draft containment, pause for human authorization, simulate approved actions, monitor acknowledgements, and prevent unsafe closure.

The flagship case uses **official openFDA H-1230-2026** alongside a clearly separated **SYNTHETIC — ACADEMIC DEMO** digital twin for fictional Northstar Grocers. A real public recall does not imply that Northstar, its facilities, or its records were involved.

![RecallOps system architecture](docs/images/02_system_architecture.svg)

## What the project demonstrates

- An explicit LangGraph lifecycle with typed state, routing, bounded retries, and durable interrupt/resume.
- Fixed recall, matching, traceability, and containment specialists; optional live-mode Deep Agent supervision; and an independent verification/critic step.
- Three MCP boundaries: read-only recall registry, read-only traceability, and approval-gated simulated operations.
- Concrete middleware for provenance, structured output, budgets, retries, masking, approval, idempotency, progress detection, and trace recording.
- Decision support with a human at every risk-bearing choice. Agents have no direct database access and never write operational records directly.

## Quick start

The implementation and command names are being completed in parallel with this documentation. The intended final commands are shown below; confirm them against the integrated CLI before recording a submission run.

```bash
uv sync --all-groups
uv run recallops data-validate
uv run recallops demo --recall-number H-1230-2026
uv run recallops eval
```

The default demonstration is designed to run offline from a frozen public snapshot and seeded synthetic data. `OPENAI_API_KEY` is only for the optional live-model / Deep Agent path.

## Read next

- [Proposal](PROPOSAL.md) — problem, scope, users, and decision boundaries.
- [Architecture](docs/ARCHITECTURE.md) — control, reasoning, and action planes.
- [Data sources](docs/DATA_SOURCES.md) — authoritative and synthetic provenance.
- [MCP and tools](docs/MCP_AND_TOOLS.md) — vertical boundaries and write safety.
- [Middleware and HITL](docs/MIDDLEWARE_AND_HITL.md) — policy lifecycle and approvals.
- [Operations](docs/OPERATIONS.md) — intended runbook and recovery behavior.
- [Evaluation](docs/EVALUATION.md) — deterministic scenarios and safety gates.
- [Week 3 curriculum coverage](docs/CURRICULUM_COVERAGE.md) — topic-to-evidence map.
- [Demo walkthrough](docs/DEMO_WALKTHROUGH.md) — exact narration and review inputs.

## Safety boundary

This is an academic decision-support demonstration. It does not contact consumers, notify regulators, place real inventory holds, access a production ERP/WMS/POS, handle real customer PII, or make autonomous public-health or compliance decisions. There is no A2A protocol. LangGraph coordinates agents internally; MCP is the data/tool boundary.
