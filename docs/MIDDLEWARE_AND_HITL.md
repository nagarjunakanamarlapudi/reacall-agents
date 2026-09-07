# Middleware, Human Review, and Durable Recovery

![Middleware lifecycle](images/05_middleware_lifecycle.svg)

Middleware is executable policy around agent, model, tool, graph, and side-effect boundaries. It is not a list of intended controls: the utilities are called by the runtime/services and exercised independently by tests and the red-team evaluator.

## Policy map

| Boundary | Implemented policy | Observable safe behavior |
|---|---|---|
| Case/agent | Case context and bounded task plan | Case ID, role, provenance, task limits, and completion criteria remain explicit |
| Model/reasoning | Fixed planner, structured Pydantic outputs, optional Deep Agents, model-failure fallback | Deterministic path remains available; provider failure cannot bypass review |
| Retrieval | Source routing, sealed capabilities, query/read budgets, progress watchdog, evidence critic | No Operations tool, no infinite query loop, unsupported concepts remain gaps |
| Read tool | `CallBudget`, `CircuitBreaker`, `with_retry`, typed/provenance validation | Reads retry only within bounds; malformed/unlabelled evidence fails closed |
| Context/display | `mask_sensitive`, citation-preserving summaries | Customer-like values do not leak to model/trace/UI while evidence IDs remain |
| Review | Pending-interrupt binding and `ApprovalGuard` | Wrong case/thread/version/action/digest is rejected before mutation |
| Write | One-attempt budget, one-use execution grant, idempotency key, case version, receipt validation | One approved write advances one version; exact completed replay is one logical effect |
| Cross-store | Owner UUID, checkpoint-head/request-digest fencing | Copied/stale/concurrent checkpoint state cannot mutate Operations |
| Progress/observability | `ProgressWatchdog` and `TraceRecorder` | Repeated signatures escalate; tool/node status/duration/warnings remain inspectable |
| Closure | Independent verifier plus transactional Operations checks | Failed verification, missing hold/disposition/acknowledgement, stale authority, gap, ambiguity, or contradiction blocks close |

## Dual-consent action lifecycle

![HITL, execution confirmation, and closure](images/06_hitl_closure.svg)

Each action uses two separate durable interrupts:

1. **Action review.** `interrupt()` presents one exact `ProposedAction`, current case version, canonical SHA-256 action digest, official/synthetic evidence, RAG citations/gaps, verification, and remaining action types. `approve` records an `ApprovalDecision` but performs zero writes.
2. **Execution confirmation.** The graph derives a stable execution UUID and idempotency key from thread/version/action/digest, then pauses again. Only a matching `confirm` response powers **Simulate approved actions** and permits the active checkpoint resume to mint one single-use, request-bound execution grant.

`edit` may change rationale only and returns through verification/re-review. Scope, target IDs, evidence IDs, action type, case identity, and version are immutable. `reject` keeps the case open without execution. `escalate` ends safely. `cancel` at execution confirmation also writes nothing.

After one receipt, the approval is invalidated, the Operations version increases, and the graph recomputes the next required action. That is why the flagship visibly requires a second review and confirmation for the hold after the case-creation receipt.

## Full versioned lifecycle

| Phase | Required reviewed action | Why it is separate |
|---|---|---|
| Establish case | `create_case` v0→v1 | Persists exact confirmed lots, trace events, facilities, reconciliation, and gaps |
| Contain | `apply_inventory_hold` v1→v2 | Prevents a case-creation approval from becoming blanket hold authority |
| Resolve quantity gap | `record_disposition` once per evidenced, held lot | Appends a quantity/type/time/provenance event without rewriting raw trace evidence |
| Assign coverage | `create_facility_tasks` | Tasks must cover every traced required facility |
| Confirm response | repeated `record_acknowledgment`, one facility/version | A task is not proof that a facility acknowledged |
| Close | `closure_review` → execution confirmation → `close_case` | Operations rechecks every authoritative predicate transactionally |

The flagship mixed case stops after the hold because ambiguity/gaps remain. The probable-only positive control can continue through all later actions.

## Restart and checkpoint identity

The pending interrupt and all prior JSON state live in the LangGraph checkpoint SQLite file. The Operations database owns case versions and receipts. Both stores are bound by a persistent random checkpoint-store UUID and current checkpoint head. Every human command is digested with its complete binding before the runtime claims a mutation.

Reopening both paths returns the same `thread_id`, checkpoint ID, pending kind, execution ID, and idempotency key. Completed investigation nodes are not rerun. A different/copy checkpoint store, wrong thread/case, stale head, changed action, changed human command, reused/cross-action grant, or overlapping resume is rejected without changing the checkpoint or Operations case.

## Unknown-write recovery

Writes are never retried automatically. A transport/receipt failure creates a durable `write_outcome_unknown` state and a `write_outcome_recovery` interrupt. `retry` reuses the exact key; Operations either returns the already committed receipt or makes the one authorized effect. Any changed request conflicts. A non-retry decision escalates.

## What humans still own

RecallOps does not decide that a retailer is implicated, authorize a real hold, accept a quantity explanation, acknowledge a facility, notify anyone, or terminate an FDA recall. The review controls demonstrate accountable decision points; they are not production identity/authentication.
