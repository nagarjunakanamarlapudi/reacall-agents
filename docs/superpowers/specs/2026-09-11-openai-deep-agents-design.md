# OpenAI Deep Agents Runtime Design

## Purpose

RecallOps must demonstrate genuine LLM-driven agent orchestration in its flagship Streamlit workflow. The configured OpenAI model will plan an investigation, delegate to the four fixed specialist agents, use sealed read-only MCP capabilities, and return structured evidence for independent verification. Deterministic LangGraph controls, Operations MCP authorization, and two-stage human approval remain the only path to simulated writes.

## Runtime modes

`RECALLOPS_MODEL_MODE=openai` is the live demo mode. It requires a nonblank `OPENAI_API_KEY` and `OPENAI_MODEL` in the repository-local, Git-ignored `.env` file. `make ui` loads that file without overriding variables already supplied by the process.

`RECALLOPS_MODEL_MODE=deterministic` remains available for credential-free CI, tests, notebooks, and a clearly labelled fallback. The product must never claim that a model ran when it did not.

Startup configuration is validated before the first model call. Secrets must not enter case state, checkpoints, traces, exceptions, reports, screenshots, or documentation.

## Architecture and authority boundary

The live reasoning lane uses `ChatOpenAI` and the existing `build_deep_supervisor` factory. The supervisor has four declarative roles: regulatory intake, product/lot matching, traceability and reconciliation, and containment drafting. Delegation middleware enforces role order and call budgets. Each role receives only its allowlisted, reconstructed, sealed read capabilities from Recall Registry MCP or Traceability MCP.

The live lane produces typed agent artifacts and an observable execution summary. It cannot see an Operations MCP tool. Its result is advisory until the deterministic verifier validates evidence coverage, contradictions, affected scope, completion criteria, and the absence of executed actions.

The outer durable LangGraph remains authoritative for checkpoint state, case versions, interrupts, approval binding, idempotency keys, execution confirmation, receipts, monitoring, closure, and escalation. Only the graph's approved execution node can invoke simulated Operations MCP writes.

## Data flow

1. The user opens an official recall notice.
2. The UI reports the configured provider, model, and readiness without exposing credentials.
3. Running an investigation invokes the OpenAI Deep Agents supervisor with the case question and read-only MCP gateway.
4. The supervisor creates its plan and delegates once to each required specialist.
5. Specialists retrieve official and synthetic evidence through their sealed MCP tools and return structured artifacts.
6. The independent verifier validates the structured result against returned evidence.
7. Verified evidence and a sanitized model execution summary are attached to the durable case projection.
8. The existing action-review interrupt, execution-confirmation interrupt, and Operations MCP receipt path proceed unchanged.

## Product presentation

The header uses `Reasoning mode`, not `Model mode`. In live mode it shows `OpenAI · <model>` and a status of ready, running, completed, failed, or explicit deterministic fallback. The investigation view shows the plan, specialist sequence, read-tool calls, sanitized model summary, token usage when returned by the provider, and verifier outcome. It does not expose hidden chain-of-thought.

If a model call fails, the UI records a sanitized error category and visibly labels any deterministic fallback. Fallback output cannot be represented as a successful live-model run.

## Evaluation

The same provider construction is shared by the Streamlit workflow and `make eval-model`; no separate demo-only adapter is allowed. Live evaluation records provider, a model identifier or digest, prompt digest, duration, task/delegation/tool metrics, and token usage where available. It never records credentials or raw private reasoning.

Credential-free deterministic safety, retrieval, and orchestration suites remain the reproducible submission gate. Live evaluation is an additional measured lane because hosted model behavior and cost are not deterministic.

## Make interface

- `make ui` loads `.env` and runs the configured mode.
- `make ui-openai` requires the OpenAI configuration and starts the live flagship UI.
- `make demo` remains a credential-free deterministic CLI demonstration.
- `make eval-model` uses the built-in OpenAI live runner by default and may retain an explicit adapter override for advanced use.
- `make verify` remains credential-free and must not spend API tokens.

## Failure and safety behavior

- Missing or invalid live configuration fails with a precise setup message.
- Provider authentication, timeout, rate-limit, malformed-output, and budget failures are sanitized and classified.
- A bounded retry policy applies only to safe read/model calls; Operations writes are never retried by the model lane.
- Model output is schema-validated and independently verified before it can influence proposed actions.
- Prompt injection in retrieved content is treated as untrusted data; retrieved text cannot alter tool allowlists, role definitions, middleware, or the operational graph.
- The API key is never read from command-line arguments and `.env` remains ignored by Git.

## Testing and acceptance

Unit tests cover environment loading, validation, model construction, redaction, mode labelling, failure classification, and schema enforcement using fake chat models. Integration tests prove fixed-role delegation, sealed read-only MCP exposure, verifier gating, visible fallback, and unchanged HITL/write authority. UI tests cover provider/model/status presentation without credential leakage.

The final acceptance run must include targeted tests, the complete test suite, lint, dependency/security gates, notebook execution, diagram verification, and a real OpenAI smoke investigation using the user's local `.env`. The real smoke result must confirm an actual model call, all required specialists, read-only tools, verifier completion, and a pending human-review interrupt before any write.

## Documentation and demo

README, operations guide, architecture documentation, middleware/HITL guide, demo walkthrough, teaching notebooks, and rendered architecture diagrams must all describe the same live reasoning lane and deterministic authority boundary. The demo script must explicitly show model readiness, agent planning and delegation, MCP reads, verifier output, first HITL approval, second execution confirmation, simulated receipts, evaluation results, and failure/fallback behavior.
