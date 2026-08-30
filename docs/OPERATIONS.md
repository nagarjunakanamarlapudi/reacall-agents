# Operations Runbook

This runbook documents the intended final command surface. Run each command only after the integrated repository exposes it, and capture exact output in the final verification report.

## Local setup and checks

```bash
uv sync --all-groups
uv run recallops data-validate
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
```

## Demonstration and evaluation

```bash
uv run recallops demo --recall-number H-1230-2026
uv run recallops eval
uv run recallops mcp-config
```

The default offline path uses the frozen openFDA snapshot and seeded `SYNTHETIC — ACADEMIC DEMO` data. Live model use is optional and must visibly identify the model mode. The displayed Streamlit views are Command Center, Investigation, Reconciliation, Human Review, and Audit & Evaluation; final integration confirmation should verify labels and invocation details before a recorded demo.

## Failure response

| Condition | Operator-visible response |
|---|---|
| openFDA unavailable | Continue from labelled frozen snapshot. |
| Read timeout or rate limit | Bounded retry, then circuit-open failure / fallback. |
| Ambiguous lot | Pause for review; do not auto-hold. |
| Missing event or quantity discrepancy | Retain a gap and block closure. |
| Unacknowledged facility | Keep case open and create a simulated follow-up after approval. |
| Lost write response | Retry only with the same idempotency key. |
| Stale version | Refresh/review; do not overwrite. |
| Repeated graph progress | Escalate through watchdog instead of continuing the loop. |

## Observability

The audit view should expose source mode, state/node trace, tool sequence, duration, warnings, review decision, acknowledgement state, evaluation output, and operation receipts. Logs and artefacts must preserve labels and mask customer-like values.
