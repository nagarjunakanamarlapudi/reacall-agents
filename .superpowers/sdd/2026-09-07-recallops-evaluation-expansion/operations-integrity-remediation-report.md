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

## Runtime-only authorization follow-up

The final authority-boundary review found that the normal `OperationsService` object still carried workflow reservation, mutation-claim, recovery, and grant-issuance methods. Those responsibilities now live behind a private runtime authorization broker. The public service and both direct/stdio gateways retain business reads and grant-consuming mutations only; the `RecallOpsRuntime` holds its broker outside the object's public attribute surface.

The checkpoint database creates and persists a random UUIDv4 owner capability. Each active execution attempt now also persists the exact execution ID and canonical execution-request digest. Grant issuance verifies the owner, active checkpoint head, workflow request, execution ID, and execution-request digest. Grant consumption joins the live workflow identity and rechecks every one of those attempt bindings in the same transaction as the business write. Restart and exact completed replay remain supported, while a different execution, action, version, request, owner, or consumed capability cannot gain authority.

The safety evaluator now labels R15/R16 direct lifecycle data as `privileged_lower_layer_lifecycle_fixture`. Those receipts remain in the unified ledger for sequence, integrity, closure, and lifecycle assertions, but are not counted as end-to-end dual-consent observations. Only runtime-context receipts enter `unauthorized_write_count`, and an approval-only service-probe mirror cannot authorize a runtime receipt. Missing or promoted fixture scope fails report validation. The existing R01-R21 numbering and 320-assertion history remain intact; R15/R16 disclose the lower-layer scope in their retained state and assertion text.

Fresh validation for this follow-up:

- Consumer-service and lifecycle unit suites: 63 passed.
- Durable workflow integration suite: 92 passed.
- Direct and real-stdio MCP integration suite: 23 passed.
- Evaluator end-to-end suite: 33 passed.
- Scorecard validation/tamper suite: 241 passed.
- Transport-inclusive safety gate: 21/21 scenarios and 320/320 assertions passed; all four unsafe counters are zero.
- Combined offline scorecard: safety 21/21, retrieval 96 cases/576 results, orchestration 24 cases/48 results; gate passed.
- Changed Python files: Ruff format and lint passed.

Canonical artifact bindings after regeneration:

- Safety corpus: `483a56638fcbaa01637c8864a521c2b66eb15694f61b11773411ef501f8d21c3`
- Safety report: `76dad415cc0c18e79942ce82ca3db6050a559e47e4b2276e2471539dac96a8ad`
- Scorecard: `b11edb68db8783d26812434b7e6f5b119ee551195d25aa15e522fd17137d8346`

The safety report was regenerated with `make eval-safety`, and the scorecard was rebuilt with `make eval-summary`; neither measured artifact was hand-edited.
