# Operations integrity remediation report

Date: 2026-09-07

## Outcome

The Operations boundary now treats workflow review data as a request, not as authority. Every simulated write requires a persisted one-time execution grant from the active durable checkpoint resume, and the same SQLite transaction consumes that grant while validating authoritative recall data and lifecycle prerequisites.

## Red evidence reproduced

- A self-consistent caller-authored approval envelope could previously reach a write without a durable workflow capability.
- Case creation trusted caller-supplied recall/lot classification and evidence relationships too deeply.
- Holds could target lots or evidence outside the persisted case scope.
- Closure did not require a persisted lot hold and could over-trust caller-shaped completion state.
- A verifier failure could continue into action review.
- Quantity resolution reused evidence identifiers rather than recording a separate disposition fact.

The regression suite first exercised these conditions as failing direct-service and workflow tests before the implementation was changed.

## Implemented invariants

1. Case creation reloads the configured frozen recall, derives its predicate, recomputes lot classifications, and accepts only exact/probable case lots with exact trace, facility, reconciliation, and gap evidence.
2. Holds, facility tasks, acknowledgements, dispositions, and closure validate target/evidence relationships against persisted case scope and authoritative traceability data.
3. Execution grants are absent from the public MCP issuer surface, persisted in Operations SQLite, single-use, and bound to case, thread, checkpoint head, case version, workflow request, execution request, execution/action digests, actor, idempotency key, and operation request hash.
4. Grant consumption and the business mutation are atomic. Exact completed idempotent replay returns the original receipt; a consumed or mismatched grant cannot authorize another write.
5. The lifecycle is ordered: create case, hold every case lot, append any required disposition, create exact facility coverage, persist acknowledgements, then close.
6. Disposition is an append-only event with lot, quantity, type, timestamp, provenance, and source evidence. Reconciliation is recomputed from base inventory facts plus those events; raw trace IDs remain unchanged.
7. Independent-verifier failure routes to an escalated terminal state, and both action preparation and execution defensively require an explicit passing result.

## Evaluation preservation

The R01-R21 corpus remains exactly 21 safety-critical scenarios. R15, R16, and R20 were updated in place to measure the strengthened hold-first lifecycle, append-only resolution, and execution-fenced closure ordering. The more granular authority-boundary matrix remains in direct-service and real stdio MCP regression tests so the established scenario numbering and evidence history stay stable.

## Validation evidence

- Operations SQLite unit suite: 41 passed.
- Added real stdio authority/lifecycle matrix: 7 passed.
- Full unit suite: 812 passed.
- Full durable workflow integration suite: 92 passed.
- Full direct/real-stdio MCP suite: 23 passed.
- Documentation and evaluation-report projection suites: 351 passed.
- Retrieval MCP integration suite: 9 passed.
- Complete transport-inclusive safety gate: 21/21 safety-critical scenarios passed; every required rate is 1.0 and all unsafe counters are zero.
- Combined scorecard: safety 21/21, retrieval 96 cases/576 results, orchestration 24 cases/48 results; offline gate passed.
- Focused receipt-integrity recovery matrix: 8 passed.
- Ruff on all changed Python files: passed.

Generated safety artifacts were produced by `make eval-safety`; they were not hand-edited. The scorecard was rebuilt with `make eval-summary`.
