# Week 3 Curriculum Coverage

**Status:** approved curriculum-evidence contract pending Task 11 runtime integration.

This map identifies the concrete RecallOps design/demo evidence for each Week 3 topic. Final integration should link each row to the implemented module/test output, but this document does not claim that pending parallel code has already passed.

| Week 3 topic | Concrete RecallOps evidence | Where to show it |
|---|---|---|
| Agent loop | Bounded intake → plan → specialists → reconcile → verify → review → monitor routes, each returning typed state updates. | `docs/images/03_orchestration.svg`; Investigation timeline. |
| Planning | Deterministic bounded task plan with completion criteria; optional Deep Agent supervisor uses bounded delegation. | Planner output in Investigation; Architecture/Proposal. |
| State and checkpoints | `RecallCaseState`, `StateGraph`, case version, `thread_id`, and SQLite checkpoint/resume design. | `docs/ARCHITECTURE.md`; resume review demonstration. |
| Memory | In-thread durable case state/checkpoints are working memory for the case; fresh source/tool evidence remains truth. The design does not present cross-case model memory as operational truth. | Architecture and audit trace; explain the boundary in demo Q&A. |
| HITL | `interrupt()` pauses for ambiguity, pre-action review, edits/disputes, and closure; review packet carries evidence, gaps, equation, and proposed actions. | Human Review view; `06_hitl_closure.svg`. |
| Supervisor | Optional Deep Agent supervisor plans and delegates to fixed specialists, while an independent critic remains outside its context. | `03_orchestration.svg`; live-mode explanation. |
| Planner-executor/reflection | Planner generates a bounded task contract; specialists/approved graph node execute scoped work; Verification/Critic checks contradictions, coverage, and proportionality. | Orchestration diagram and tool trace. |
| MCP | Recall Registry, Traceability, and Operations FastMCP servers; direct and stdio gateways share an interface. | `04_mcp_tool_safety.svg`; MCP discovery smoke. |
| Middleware | Case context, planning rule, routing, fallback, budgets, structured output, permission/provenance, masking, approval, watchdog, and trace recorder. | `05_middleware_lifecycle.svg`; middleware notebook/test output. |
| Failures and recovery | Frozen fallback, bounded read retry, circuit breaking, durable resume, stale-version rejection, idempotent replay, gap retention, and escalation on repeated progress. | Operations failure table; injected failure scenarios. |
| Cost, latency, and reliability | Call/tool budgets, bounded delegation/retries, duration tracing, deterministic credential-free fallback, and fail-closed writes make resource use and reliability observable. | Audit & Evaluation view; telemetry and evaluation report. |
| Observability and evaluation | Node/tool trace, durations, warnings, source mode, receipts, scenario results, and deterministic safety gates. | Audit & Evaluation view; `docs/EVALUATION.md`. |
| A2A | **A2A is intentionally excluded.** The case has one explicit LangGraph coordinator; MCP is the vertical data/action interface. Avoiding A2A keeps ownership, state, review, and side-effect order inspectable for this workflow. | System architecture diagram and presenter narration. |

## Evidence discipline

The required story is not “an agent did a recall.” It is “an explicit workflow used evidence to recommend an action, stopped for a human, made only an approved simulated change, and refused closure until the case was safe.” This makes the curriculum concepts inspectable rather than decorative.
