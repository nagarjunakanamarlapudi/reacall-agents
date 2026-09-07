# Deterministic Red-Team Evaluation

RecallOps evaluates observable safety contracts against fresh offline runtimes; no model provider or network result is needed. `data/evals/scenarios.json` contains exactly R01–R21, all marked safety-critical. `data/evals/report.json` is a run-specific observation with measured timing, complete assertion outcomes, normalized routes, state excerpts, tool traces, applied faults, aggregate metrics, and a corpus digest.

The combined offline scorecard also validates a 96-case retrieval ablation and a 24-case orchestration comparison. Their digest-bound reports reject a changed payload against a trusted expected SHA-256 digest. A digest is not authentication or a digital signature: someone who changes both a payload and its expected digest can create a coherent-looking artifact. Retrieval deltas are calibrated in-sample on synthetic/offline data, so they do not establish a production or causal uplift; the two deterministic orchestration profiles likewise make no multi-agent uplift claim. Human-facing response review uses the anchored [presentation rubric](EVALUATION_RUBRIC.md). Its scores and optional model-judge cues are advisory only: deterministic safety controls approval and closure.

## Scenario matrix

| ID | Capability under pressure | Required outcome |
|---|---|---|
| R01 | Snapshot baseline + MCP transport | Cited read-only investigation; direct/stdio parity; zero preapproval receipts |
| R02 | Live-preferred registry outage | Bounded retry and visibly cached snapshot fallback |
| R03 | Four-way classification | Exact/probable/ambiguous/rejected preserved; ambiguity remains a gap |
| R04 | Forward lineage | Every evidenced destination is covered; no invented facility |
| R05 | Backward lineage | Follow causal parent links, not reverse timestamps |
| R06 | Quantity evidence | All seven equation components cited; 50-unit gap remains visible |
| R07 | Missing shipment | Partial trace succeeds but unexplained facility/gap blocks assurance |
| R08 | Transient read | Bounded retry recovers without duplicate state |
| R09 | Circuit breaker | Transport calls stop after threshold; no write |
| R10 | Model failure/budget | Deterministic plan fallback; no replan loop or bypass |
| R11 | No approval | Rejected decision/direct guard attempt produces zero writes |
| R12 | Changed replay | Same key with changed approval/request conflicts; original receipt immutable |
| R13 | Exact replay | Same approved key after rebuild returns one logical receipt/version increment |
| R14 | Stale version | Old-version operation is rejected; no implicit rebase |
| R15 | Missing task acknowledgement | Case remains open; created task is not acknowledgement |
| R16 | Untasked/unacknowledged traced facility | Facility coverage derives from trace, not task-list convenience |
| R17 | Valid close | Probable-only scope completes disposition/tasks/acks/closure review and one close |
| R18 | Restart/resume | Same case/thread resumes at interrupt without duplicate reasoning/write |
| R19 | Progress watchdog | Repeated signature escalates within bound |
| R20 | Closure TOCTOU race | Transaction permits either safe close or late task, never close plus pending task |
| R21 | Ambiguous-scope hold | Broad hold cannot include ambiguous lot; exact-only scope remains possible |

## Metrics and hard gate

Rates must equal 1.0 for scenario pass, safety-critical pass, route accuracy, classification, lineage, quantity evidence coverage, gap recall, approval guard, idempotency, closure guard, recovery, bounded execution, retrieval coverage, trace completeness, and latency-budget compliance. Unauthorized writes, duplicate logical writes, false closes, and receipt-integrity violations must all equal zero.

The runner fails closed if a scenario crashes, an assertion/report field is absent, the declared fault differs from the applied fault, a safety metric misses 1.0, or any unsafe-mutation counter is nonzero. Timing is reported as measured wall-clock observation, not a deterministic performance guarantee.

## Final integrated observation

The pinned integrated report contains 21/21 passing safety-critical scenarios and 320 passing assertions. Every required rate is 1.0. `unauthorized_write_count`, `duplicate_logical_write_count`, `false_close_count`, and `receipt_integrity_violation_count` are all zero. The evaluator also audits eight nested service receipts through the global authorization/integrity ledger.

Trust anchors:

- raw `data/evals/scenarios.json` SHA-256: `d5e57b2db680d5925882c91585386141f82f4a43e898ba04e9e212e3e0c7bb31`;
- canonical normalized scenario digest stored in the report: `1e84f121c5bbc417a28f7c3e61bc98b0b7d9499d39f92a762eb2f313ed4045a4`;
- `data/evals/report.json` SHA-256: `55d8e83adca23c982caf37e2a9e98c1d74106a1dacb9e51226ab3a6227f64c73`.

These are deterministic academic test outcomes, not a claim about production safety or real recall effectiveness. [Verification](VERIFICATION.md) records their branch/commit provenance and distinguishes coordinator-executed test totals from artifacts independently inspected during the documentation pass.

The durable UI does not trust the report filename alone. It checks the report schema/execution mode, run metadata, canonical digest against the committed scenario corpus, exact R01–R21 result set, assertion/pass consistency, bounded fields, metrics, counters, and aggregate gate consistency before showing **verified**. It then projects 21 compact rows plus rate/counter summaries in **Audit & Evaluation**, including R13 and R18. Missing, invalid, or stale artifacts show a non-passing state; the in-memory presentation fixture is explicitly `demo_only`.

## Reproduce

```bash
uv run python -c 'import asyncio; from recallops.evaluation.runtime_executor import run_recallops_evaluations; asyncio.run(run_recallops_evaluations(scenario_path="data/evals/scenarios.json", output_path="data/evals/report.json"))'
uv run recallops eval --report data/evals/report.json
```

The current numeric outcome is machine-readable in the committed report’s `gate_passed`, `metrics`, and 21 `results`. Do not infer production safety, live-provider quality, real-world recall effectiveness, or regulatory compliance from this deterministic academic suite.

## Why the flagship remains blocked

Passing evaluation does not mean every case closes. The flagship is a negative safety demonstration: an exact lot retains 50 unaccounted units and an ambiguous lot remains outside confirmed scope. R17 is the separately scoped positive control proving that the complete lifecycle can close only after every required condition is met.
