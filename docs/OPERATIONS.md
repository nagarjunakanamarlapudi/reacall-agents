# Local Operations and Reproducibility Runbook

All default product paths are credential-free and offline. Evaluation commands resolve their
project and artifact paths from the Makefile/package, so they also work when invoked through an
absolute Makefile path from another directory.

## Make command interface

Run `make help` for the complete command list. The primary workflows are:

```bash
make setup
make data-validate
make demo
make ui
make ui-stdio
make mcp-smoke
make eval-fast
make eval-safety
make eval-retrieval
make eval-orchestration
make eval
make eval-summary
make notebooks
make diagrams
make test
make lint
make security
make verify
```

`make eval-safety`, `make eval-retrieval`, and `make eval-orchestration` regenerate
their deterministic reports through temporary same-directory files. A report replaces the prior
artifact only after its strict loader verifies the complete result matrix, digests, recomputed
metrics, and passing gate. `make eval` runs all three suites, builds the digest-bound scorecard,
then validates and summarizes it. `make eval-summary` never repairs an artifact: missing,
malformed, stale, failed, or digest-mismatched inputs return nonzero and print `UNVERIFIED` or
`FAILED`. `make eval-fast` validates all committed artifacts and runs the stable ranking-metric
smoke tests without regenerating the full suites. `make verify` includes the complete deterministic
`make eval` workflow.

`make ui` and `make ui-stdio` accept `PORT` and `RUNTIME_DIR`; `make demo` accepts `RECALL_NUMBER`. For example:

```bash
make ui PORT=8765 RUNTIME_DIR=.recording-runtime
make demo RECALL_NUMBER=H-1230-2026
```

`make security` is the production gate: Python dependency audit, Bandit medium/high-severity scan, and production-only npm audit. `make security-full` additionally reports all low-severity Bandit findings and development-only npm advisories; it returns nonzero while recorded findings remain. `make verify` composes the complete submission gate. The underlying commands are retained below for auditability and direct troubleshooting.

## Install

```bash
uv sync --locked --all-groups
npm ci
```

The Python range is `>=3.12,<3.13`. Diagram rendering is pinned to Node `24.15.0`, npm `11.12.1`, and Mermaid CLI `11.12.0`.

## Validate data and run the flagship CLI

```bash
uv run recallops data-validate
uv run recallops demo --recall-number H-1230-2026
uv run recallops mcp-config
uv run recallops eval
```

`recallops demo` creates temporary checkpoint/Operations databases unless `RECALLOPS_RUNTIME_DIR` is set. It demonstrates the first consent/write cycle and reports authoritative closure blockers; the Streamlit walkthrough shows both flagship action cycles.

## Run the five-view command center

Durable runtime with direct MCP gateway:

```bash
uv run streamlit run src/recallops/ui/app.py
```

Durable runtime with three actual stdio MCP subprocesses:

```bash
RECALLOPS_MCP_TRANSPORT=stdio uv run streamlit run src/recallops/ui/app.py
```

To isolate a recording run, use an explicit recoverable directory:

```bash
RECALLOPS_RUNTIME_DIR=.recallops-runtime-demo uv run streamlit run src/recallops/ui/app.py
```

The default `RECALLOPS_UI_MODE=durable` uses LangGraph plus two SQLite files. `RECALLOPS_UI_MODE=demo` is a deterministic presentation fixture and must be labelled as such; use durable mode for the submission demo.

## Optional live public lookup

```bash
RECALLOPS_SOURCE_MODE=live uv run streamlit run src/recallops/ui/app.py
```

Only the hard-coded `https://api.fda.gov/food/enforcement.json` host is contacted. Failure or a mismatched/malformed record returns the labelled frozen snapshot. The durable investigation graph stays snapshot-bound for reproducibility. There is no You.com/general web search.

## Regenerate and validate deterministic artifacts

```bash
uv run python scripts/generate_demo_data.py
uv run python scripts/build_notebooks.py
uv run pytest -q tests/notebooks/test_notebooks.py
./scripts/render_diagrams.sh --verify
```

`build_notebooks.py` recreates six executed self-contained notebooks. Each notebook contains embedded teaching data, does not import the product package, and requires no network/key.

## Run the evaluators

Run the complete credential-free evaluation and verify the combined scorecard:

```bash
make eval
make eval-summary
```

The individual workflows are:

```bash
make eval-safety
make eval-retrieval
make eval-orchestration
make eval-fast
```

`eval-safety` runs R01–R21 against fresh runtime/Operations workspaces. The retrieval
workflow runs 96 labelled cases through six ablations (576 persisted results). The orchestration
workflow runs 24 cases through the bounded-single-agent and fixed-specialist profiles (48 offline
results). Every default workflow is offline and supplies no model or provider credentials.

When the project environment is not already activated, select the repository explicitly. Replace
`/absolute/repository/path` with this checkout's absolute path; these commands are then independent
of the current working directory:

```bash
uv run --project /absolute/repository/path recallops eval
uv run --project /absolute/repository/path recallops eval-retrieval
uv run --project /absolute/repository/path recallops eval-orchestration
uv run --project /absolute/repository/path recallops eval-scorecard
```

The first command intentionally retains the original safety-only summary semantics. The distinct
`eval-scorecard` command validates all six bound report/corpus digests and recomputes all three
suite summaries before reporting the combined offline verdict.

### Explicit live-model opt-in

Live orchestration is unavailable until an application-specific provider adapter and its provider
credentials are configured. Supply a Python `MODULE:ATTRIBUTE` that resolves to a
`LiveRunnerFactory` instance (or a zero-argument function returning one):

```bash
make eval-model LIVE_MODEL_ADAPTER=my_recallops_provider:live_factory
```

With no `LIVE_MODEL_ADAPTER`, `make eval-model` exits nonzero with an explicit configuration and
credentials message; it never selects a provider implicitly. The adapter retains credentials and
raw provider messages outside persisted artifacts, and its evaluated capability surface is the
sealed read-only orchestration capture—never Operations tools. A completed or errored optional-live
status is reported separately and is always excluded from `offline_gate_passed` and the command's
offline exit decision. Factory/provider errors remain visible rather than being presented as a
successful live run.

## Test and static checks

```bash
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv lock --check
uv pip check
```

## Security checks

```bash
uvx --from pip-audit pip-audit
uvx --from bandit bandit -q -r src
npm audit --omit=dev
npm audit
```

Mermaid CLI is a development-only diagram renderer. Record npm’s production-dependency result separately from transitive development-tool findings; do not imply that a clean Python audit clears Node tooling.

## Failure and recovery runbook

| Condition | Safe operator response |
|---|---|
| openFDA unavailable | Continue only with the visible frozen/cached label |
| Read timeout/429 | Observe bounded retry/circuit outcome; do not invent evidence |
| Malformed/missing evidence | Investigation escalates before any operation |
| Ambiguous lot | Retain ambiguity; confirmed-only hold may proceed after review |
| Quantity gap | Record a disposition only with exact evidence; otherwise stay open |
| Missing facility acknowledgement | Create/acknowledge tasks one version at a time; do not close |
| Lost write response | Use the pending recovery control and exact same key |
| Stale decision/version/digest | Reload the current packet and review again |
| Repeated progress | Preserve state and escalate after watchdog threshold |
| Copied/mismatched checkpoint store | Use the original paired checkpoint and Operations databases |

## Resetting a local demonstration

Runtime files under `.recallops-runtime` or a directory explicitly supplied through `RECALLOPS_RUNTIME_DIR` are generated academic state. Stop Streamlit before moving that exact directory aside. Do not delete a broad workspace path or an unknown Operations database. Starting with a fresh explicit directory produces a new checkpoint-store UUID and empty simulated Operations store.

## Observability

The UI’s Audit & Evaluation view shows the normalized node/tool timeline, human decision history, receipts, source/model/runtime/transport modes, failure fixture result, and closure gates. In durable mode it loads the committed evaluator artifacts through cwd-independent repository paths and shows 21 compact rows plus rate/counter summaries only after schema, digest, result-set, assertion, metric, and gate checks pass. Missing, invalid, or stale artifacts never display a success claim; the presentation fixture is labelled `demo_only`. Display output is masked and never exposes hidden reasoning.
