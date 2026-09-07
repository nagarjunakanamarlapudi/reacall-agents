# Verification Evidence

This file records commands actually executed against named repository states. It does not convert an implementation plan or another worktree’s report into a success claim. The final integrated audit is recorded below.

## Documentation worktree evidence — 30 August 2026

**Branch base inspected:** `agents/final-docs` from runtime commit `ee5242d` before this documentation commit.

### Dependency installation for diagram verification

Command:

```bash
npm ci
```

Observed: exit 0; 258 packages added; npm reported a Puppeteer deprecation warning and five high-severity findings in the development dependency tree. No forced remediation was applied because Mermaid CLI is a pinned development-only renderer and `npm audit fix --force` could change the locked toolchain. The final security audit must record `npm audit --omit=dev` and the full `npm audit` separately.

### Canonical Mermaid render

Command:

```bash
./scripts/render_diagrams.sh
```

Observed: exit 0; nine Mermaid sources rendered with Node `v24.15.0`, npm `11.12.1`, and Mermaid CLI `11.12.0`.

### Diagram stability/parity

Command executed twice after the final diagram edit:

```bash
./scripts/render_diagrams.sh --verify
```

Observed on both executions: exit 0; each execution rendered all nine sources twice, each pair was byte-identical, and every fresh SVG matched its committed counterpart. Both executions produced the same SHA-256 set:

```text
3ff4bde62dfe8ad1d8fb344fcc283c41e725d956bdedfbd8fc1749fb0cd10679  01_data_provenance.svg
10eb3b5769e36f749687d6108b4a9b3942b2df9ef93805997d379035265cc589  02_system_architecture.svg
da7e115632ffaca6364c5c616404c100fd07afcd3d045eee7add858909610b58  03_orchestration.svg
948c29396f5680442d1624964d799bba06d805d8f22a23b107be5caa0303d52c  04_mcp_tool_safety.svg
08254a3cb9c88f080b88a312437f56d96062a78be6bbbd23a3befcf5594d0645  05_middleware_lifecycle.svg
840ac98dd6b9c4420a3385b5bf2cb39f77eee1b517cb51cb0257fe9a6dc15c7c  06_hitl_closure.svg
e022578320578ddad18e9d08e9ea7ffdb9a83aaebc43e425d5ad65d27ea2c850  07_demo_story.svg
50dcb3be9670a4820726fa4bdb9ef7ed860b802aefa0cfca3c9253b2847523e0  08_business_recall_lifecycle.svg
3e9c78b6c3b5eb83b911e3c37bdcd131e517cf4015198a2556be9fac72af98f3  09_domain_evidence_model.svg
```

### Documentation contract

Command used during the red phase:

```bash
uv run pytest -q tests/docs/test_documentation.py
```

Observed before documentation implementation: six failures, including missing `docs/VERIFICATION.md`, missing durable demo/data contract fields, stale runtime-integration status, missing new diagram labels, and absent local Mermaid install. This proved the added checks could catch the stale submission state.

After the final content, recovery script, evaluator/UI evidence, and polished-visual integration, the command was executed twice. Observed: exit 0 with `21 passed` on each execution (`20.79s` and `20.31s`). The contract checks promised artifacts, visual links, data counts/digests, source boundary, demo duration/order/terms, recovery contract, diagram labels/layout/parity, business lifecycle ordering, and stale placeholder language.

### Focused formatting, lint, lock, and patch checks

Commands:

```bash
uv run ruff format tests/docs/test_documentation.py
uv run ruff format --check tests/docs/test_documentation.py
uv run ruff check tests/docs/test_documentation.py
uv lock --check
git diff --check
```

Observed: the formatter made one mechanical update to the documentation test; the subsequent format check and Ruff check exited 0; `uv lock --check` resolved 166 packages and exited 0; `git diff --check` exited 0.

## Final integrated evaluator evidence — 30 August 2026

**Integrated branch inspected:** `feat/recallops` at `2763984ea1ba8828e36e1193065676d9670c3dd3`.

The documentation pass independently inspected the committed JSON through `git show`, `jq`, and `shasum -a 256`. Observed: schema `1.1`, execution mode `offline_deterministic`, `gate_passed=true`, 21 result objects, and 320 assertion objects. All required rate metrics equal 1.0. The four mutation counters—unauthorized writes, duplicate logical writes, false closes, and receipt-integrity violations—equal zero.

Artifact trust anchors observed:

```text
d5e57b2db680d5925882c91585386141f82f4a43e898ba04e9e212e3e0c7bb31  data/evals/scenarios.json
1e84f121c5bbc417a28f7c3e61bc98b0b7d9499d39f92a762eb2f313ed4045a4  canonical normalized scenario digest in report
55d8e83adca23c982caf37e2a9e98c1d74106a1dacb9e51226ab3a6227f64c73  data/evals/report.json
```

The coordinator’s final integrated run additionally reported eight nested service receipts audited by the global ledger, `31 passed` for the evaluator tests, and `501 passed` for the unit tests. Those three execution totals are attributed to that integrated run; this documentation worktree did not re-execute those suites.

## Final integrated durable UI evidence — 30 August 2026

**Integrated branch inspected:** `feat/recallops` at `8814d41ad06230047824c936f193c38b46b0fd68`.

The integrated durable UI verifies and projects the committed evaluator report as 21 compact rows with R13/R18, aggregate rates, and unsafe counters using cwd-independent artifact paths. Missing, invalid, or stale reports remain non-passing; the in-memory fixture is labelled `demo_only`. The coordinator reported `69 passed` for the final UI suite. That execution total is attributed to the integrated run; this documentation worktree inspected the committed source but did not re-execute the UI suite.

## Final integrated verification commands

**Executable baseline:** `feat/recallops` at `f21ecc6` (`docs: record polished PNG generation prompts`). The only subsequent changes in the verification commit are this evidence record and completed submission checklist.

### Complete test tree

The six disjoint test directories were run in four concurrent partitions to reduce wall-clock time without dropping coverage:

```bash
uv run pytest -q tests/unit
uv run pytest -q tests/integration
uv run pytest -q tests/e2e
uv run pytest -q tests/ui tests/notebooks tests/docs
```

Observed: all commands exited 0—`501 passed`, `116 passed`, `39 passed`, and `96 passed`, respectively. Together they cover all 752 collected tests under `tests/`. The e2e partition includes the real R01–R21 hard-gate run and actual stdio MCP smoke; the integrated report remained 21/21 with 320 assertions, every required rate at 1.0, and all four unsafe counters at zero.

### Product, data, and CLI smoke

Commands:

```bash
uv run recallops data-validate
uv run recallops demo --recall-number H-1230-2026
uv run recallops mcp-config
uv run recallops eval --report data/evals/report.json
```

Observed: all four exited 0. Data validation reported the official `H-1230-2026` snapshot plus 48 synthetic products, 144 lots, and 577 events. The flagship demo reached one simulated `create_case` receipt/version increment and then correctly reported `Open — closure blocked`. MCP config emitted three machine-readable stdio server definitions. The evaluator summary reported 21 scenarios, 21/21 safety-critical passed, and `Evaluation gate: PASSED`.

The evaluator summary exposed a pre-release schema mismatch (`scenarios` versus the report’s `results` array). Two RED→GREEN CLI regression tests now verify the committed report schema and fail closed when `gate_passed` is false; the focused CLI suite finished `8 passed`.

### Streamlit smoke

Command:

```bash
RECALLOPS_RUNTIME_DIR=/tmp/recallops-streamlit-smoke uv run streamlit run src/recallops/ui/app.py --server.headless true --server.port 8765 --browser.gatherUsageStats false
curl --fail --silent --show-error --output /tmp/recallops-root.html --write-out '%{http_code} %{content_type} %{size_download}\n' http://127.0.0.1:8765/
```

Observed: Streamlit/Uvicorn started successfully; the application root returned `200 text/html; charset=utf-8` with a 2,515-byte page. The process then stopped cleanly with exit 0. The older `/_stcore/health` path returned 404 in this installed Streamlit release, so the smoke assertion uses the served application root rather than claiming that endpoint exists.

### Regenerated teaching and diagram artifacts

Commands:

```bash
uv run python scripts/build_notebooks.py
uv run pytest -q tests/notebooks/test_notebooks.py
./scripts/render_diagrams.sh --verify
```

Observed: all six notebooks rebuilt deterministically and their focused suite finished `6 passed`; no notebook diff remained. Diagram verification exited 0, double-rendered all nine Mermaid sources byte-identically, and matched every committed SVG. The three primary presentation visuals are opaque-white 1672×941 PNGs; their generation prompts and refinements are committed in `docs/images/submission-visual-prompts.md`.

### Task 8 evaluation expansion record (2026-09-07)

This later record is separate from the historical six-notebook observation above. It starts from Task 8 base commit `fd3cf1c` and is bound to the follow-up commit that contains this record. The result belongs to this Task 8 change only; it does not change the historical evidence statement.

Commands run for this follow-up: `make notebooks` twice (with a SHA-256 comparison of notebook 07 between rebuilds); an isolated `NotebookClient` execution of every notebook from a fresh temporary working directory; `uv run pytest -q tests/docs/test_documentation.py`; `uv run ruff check scripts/build_notebooks.py tests/notebooks/test_notebooks.py`; `uv run ruff format --check scripts/build_notebooks.py tests/notebooks/test_notebooks.py`; and `git diff --check`.

Observed: both notebook rebuilds finished `9 passed` with identical notebook 07 bytes. All seven notebooks executed from clean temporary working directories and emitted their assertions. The documentation suite finished `22 passed`; Ruff check/format and diff checks exited 0. Generated notebook artifacts retain no execution counts or saved outputs.

### Dependency and security audit

Commands and observed outcomes:

```text
uvx --from pip-audit pip-audit       exit 0; no known vulnerabilities found
uvx --from bandit bandit -q -r src  exit 1; 31 low, 0 medium, 0 high
npm audit --omit=dev                 exit 0; 0 vulnerabilities
npm audit                            exit 1; 5 high findings in development-only Mermaid/Puppeteer renderer dependencies
```

Bandit’s 31 low findings are 28 `B101` evaluator/invariant assertions, two `B105` false positives on the string value `1.0`, and one `B311` deterministic synthetic-data pseudo-random generator. No security-sensitive randomness is claimed. The npm production tree is clean; the five development-tree findings trace to `extract-zip` through Puppeteer in pinned `@mermaid-js/mermaid-cli@11.12.0`. The suggested forced fix would move Mermaid CLI outside the locked renderer version, so the finding is recorded rather than hidden or force-upgraded before submission.

### Final static/repository gate

Commands:

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv lock --check
uv pip check
uv run pytest --collect-only -q
uv run pytest -q tests/docs/test_documentation.py
git diff --check
```

Observed: dependency sync resolved 180 packages and checked 172; Ruff reported 88 files already formatted and no lint findings; the lock was current; all 172 installed packages were compatible; pytest collected exactly 752 tests; the post-evidence documentation contract finished `21 passed`; and patch whitespace was clean. A final repository search found no unresolved placeholder or stale integration-status markers in submission-facing documents. Git status was clean after committing this record.

## Make command interface verification — 7 September 2026

The project-level Make interface was added after the integrated submission baseline. Four RED→GREEN contract tests execute `make help` and dry-run the configurable UI, stdio UI, and composed verification workflows. The RED phase produced four missing-target failures; after implementation, `tests/e2e/test_makefile.py` finished `4 passed`.

Observed target results:

| Target | Observation |
|---|---|
| `make setup` | Exit 0; locked Python environment checked and 258 pinned Node packages installed. |
| `make data-validate` | Exit 0; official/synthetic labels and 48-product/144-lot/577-event counts printed. |
| `make demo` | Exit 0; flagship reached one receipt and correctly remained open with blockers. |
| `make ui PORT=8767 RUNTIME_DIR=/tmp/recallops-make-ui-smoke` | Streamlit/Uvicorn started and the application root returned HTTP 200 with an 11,141-byte HTML page; the interactive process was then stopped. |
| `make eval-summary` | Exit 0; 21 scenarios, 21/21 safety-critical passed, gate passed. |
| `make lint` | Exit 0; 89 files formatted, Ruff clean, lock current, 172 packages compatible. |
| `make security` | Exit 0; no known Python dependency vulnerabilities, no medium/high Bandit findings, and zero production npm vulnerabilities. |
| `make notebooks` | Exit 0; six deterministic notebooks rebuilt and `6 passed`; no generated diff remained. |
| `make diagrams` | Exit 0 when run after setup; nine stable double-renders matched committed SVGs. |
| `make mcp-smoke` | Exit 0; `16 passed` across direct and stdio MCP behavior. |
| `make security-full` | Intentionally nonzero; it reported the already-recorded 31 low Bandit findings and five development-only Mermaid/Puppeteer npm advisories after running every scanner. |

An initial verification attempt ran `make setup` and `make diagrams` concurrently; `npm ci` replaced `node_modules` while Mermaid was importing Puppeteer, so that render attempt failed. After setup completed, the documented serial `make diagrams` invocation passed with full parity. The composed `make verify` target does not include setup and therefore cannot create that race.

`make -s -n verify` expanded to all 14 expected underlying commands: data validation; Ruff format/lint; lock and package checks; complete pytest; notebook rebuild/execution; diagram parity; MCP smoke; evaluator summary; Python dependency audit; medium/high Bandit gate; and production npm audit.

## Evaluation expansion final integration — 7 September 2026

**Integrated code and artifact baseline inspected:** `main` at
`a9ef2c27ae1a36f21b9d8794994c55795782a639`. The following documentation-only
evidence is appended by the surrounding final Task 11 commit.

### Scope ruling and literal Make workflow

Task 11 exposed an integration defect in the written four-command sequence. At
the pre-fix baseline `869caa9`, fresh `make eval-safety`, `make eval-retrieval`,
and `make eval-orchestration` reports all passed independently, but the following
`make eval-summary` correctly exited nonzero because the existing scorecard was
still bound to the previous report bytes. The failure was preserved; no measured
output was edited.

The coordinator explicitly brought the minimal upstream correction into Task 11
scope. A RED integration test first reproduced the stale-scorecard failure. The
Makefile now declares `scorecard.json` as a file target depending on all three
suite reports. Make rebuilds the aggregate from validated report bytes only when
one of those inputs is newer, after which `eval-summary` remains a strict
validator. The focused Make suite finished `13 passed`. The exact literal
sequence was then rerun:

```bash
make eval-safety
make eval-retrieval
make eval-orchestration
make eval-summary
```

Observed: all four commands exited 0. Safety finished 21/21; retrieval finished
96 cases × 6 configurations = 576 persisted results; orchestration finished 24
cases × 2 offline profiles = 48 persisted results; and the digest-bound combined
offline gate passed.

### Fresh deterministic evaluation observations

The safety report contains 21 result rows and 320 assertion observations. Every
required rate is 1.0, while unauthorized writes, duplicate logical writes, false
closes, and receipt-integrity violations are all zero.

The fresh retrieval report recorded:

| Measurement | Observed value |
|---|---:|
| Agentic RAG Recall@5 | 0.9753787878787878 |
| Agentic RAG nDCG@5 | 0.9521676712287099 |
| Fusion Recall@5 delta | +0.005681818181818121 |
| Rerank nDCG@5 delta | +0.005266955662502459 |
| Agentic prohibited hits | 0 |
| Agentic unsupported answers | 0 |

The fresh orchestration report recorded task success and evidence coverage of
1.0 for both `bounded_single_agent` and `fixed_specialists`, 150 total tool calls
for each profile, zero duplicate tool-call ratio, zero prohibited-tool exposure,
and zero prohibited calls. The measured fixed-specialist minus single-agent
deltas were 0.0 for task success, evidence coverage, duplicate-call ratio, and
total calls; measured timing deltas remained visible rather than being converted
into an uplift claim. Optional live Deep Agents remained
`not_run_missing_credentials`, with zero executed cases and no token or cost
claim; it was excluded from the offline gate.

Content-integrity anchors at the inspected commit:

```text
d5e57b2db680d5925882c91585386141f82f4a43e898ba04e9e212e3e0c7bb31  scenarios.json
f2e347a72d4e8d0b9388cde3cf00f2fbf4e0fc834e1f34da875b5a68caed7976  report.json
f2759a1d993a5eec0be253aeecef25159aabe9e92b1eb5da422ed5649c4ea9bd  retrieval_cases.json
a23dadb43963b39819afdab5b2884ba6d583e8ad6139f298ae076d27360820dd  retrieval_report.json
ab10bc840cb47720392304406f363fcb8ced2c3562ff8613faf1046f12f7730c  orchestration_cases.json
466ebcbee7ad9167b2a9a4c634c4682c26ee6bdfc39f5ef7391bf56a12dd95bf  orchestration_report.json
94036038a5025fa342d874cd72ba7fe025799204b6d1e549e250329228a01f03  canonical scorecard self-digest
```

The scorecard additionally binds all six corpus/report file digests. Its file
SHA-256 is `9d0b74bb9bc30f00b22b3da901b9737f46166084cfc2ba3b7ff087e642591ab5`;
the different canonical self-digest above excludes its own digest field by
design.

### Complete product gates

Commands and exact observations:

| Command | Observed result |
|---|---|
| `make data-validate` | Exit 0; 48 products, 144 lots, and 577 events; official/synthetic boundary labels present. |
| `make test` | Exit 0; `1292 passed in 907.38s (0:15:07)` against the post-fix integrated baseline. |
| `make lint` | Exit 0; 110 files formatted, Ruff clean, 180 lock packages resolved, and 172 installed packages compatible. |
| `make notebooks` | Exit 0; seven notebooks rebuilt and `10 passed`. |
| `make diagrams` | Exit 0; ten diagrams passed stable double-render and committed-SVG parity. |
| `make mcp-smoke` | Exit 0; `16 passed in 228.35s (0:03:48)` across direct and stdio behavior. |
| `make security` | Exit 0; pip-audit found no known vulnerabilities, Bandit found no medium/high issues, and the production npm tree had zero vulnerabilities. |

The ten committed diagram SHA-256 values from the stable fresh render were:

```text
3ff4bde62dfe8ad1d8fb344fcc283c41e725d956bdedfbd8fc1749fb0cd10679  01_data_provenance.svg
289e3a8a14113a8af563b103b3a7e92652a9b5be4c86f45eaee7c23f65012af7  02_system_architecture.svg
487680c071ca59bb605111b48387c20a21c639ada11b958be80b63498e9d90a6  03_orchestration.svg
948c29396f5680442d1624964d799bba06d805d8f22a23b107be5caa0303d52c  04_mcp_tool_safety.svg
08254a3cb9c88f080b88a312437f56d96062a78be6bbbd23a3befcf5594d0645  05_middleware_lifecycle.svg
840ac98dd6b9c4420a3385b5bf2cb39f77eee1b517cb51cb0257fe9a6dc15c7c  06_hitl_closure.svg
0dc1679282f22b591e32fb115f1994af5dde3ac02319eed5cd50a9ef4d1eef4f  07_demo_story.svg
50dcb3be9670a4820726fa4bdb9ef7ed860b802aefa0cfca3c9253b2847523e0  08_business_recall_lifecycle.svg
3e9c78b6c3b5eb83b911e3c37bdcd131e517cf4015198a2556be9fac72af98f3  09_domain_evidence_model.svg
eb3d274e1e73d848b8e8e23a7a51546c6424237c6d422712e934705728b1cb15  10_evaluation_architecture.svg
```

`make security-full` was run separately and intentionally exited 2. It reported
38 low-severity Bandit findings, zero medium/high findings, no known Python
dependency vulnerabilities, zero production npm vulnerabilities, and five high
findings confined to the pinned Mermaid/Puppeteer development renderer chain.
No forced dependency change was applied.

### Durable direct and stdio UI evidence

Two independent Streamlit processes used distinct ports and fresh exact runtime
directories created under `/tmp`:

| Mode | Port | Runtime directory | Root response | Evaluation AppTest |
|---|---:|---|---|---|
| Durable direct | 8771 | `/tmp/recallops-task11-direct.PwnGWe` | `200 text/html; charset=utf-8`, 11,141 bytes | 1 passed |
| Durable stdio | 8772 | `/tmp/recallops-task11-stdio.BgHCuS` | `200 text/html; charset=utf-8`, 11,141 bytes | 1 passed |

Each AppTest opened the durable case, selected **Audit & Evaluation**, verified
the committed report metrics plus R13/R18, and produced no exception. Only the
two spawned Streamlit sessions were interrupted after the checks.

### Six adversarial temporary-copy probes

Each mutation was applied to a separate copy under one fresh temporary
repository. The relevant CLI validator exited 1 and the UI projector returned
`verification_status=unavailable` plus `offline_gate_passed=False` in every case:

1. changed one retrieval relevance grade;
2. deleted one persisted retrieval result and recomputed the report self-digest;
3. replaced one retrieval report self-digest;
4. set the orchestration prohibited-tool count to one and recomputed its self-digest;
5. forged `offline_gate_passed` and recomputed the scorecard self-digest; and
6. removed `optional_live_status` and recomputed the scorecard self-digest.

This demonstrates that corpus, completeness, report-integrity, safety-isolation,
aggregate-consistency, and optional-live schema mutations fail closed in both
the command and presentation boundaries.

### Final static, dependency, documentation, and repository gate

Commands:

```bash
uv run ruff format --check .
uv run ruff check .
uv lock --check
uv pip check
uv run pytest --collect-only -q
uv run pytest -q tests/docs/test_documentation.py
git diff --check
git status --short --branch
```

Observed: all commands exited 0. Ruff reported 110 files already formatted and
no lint findings; the lock resolved 180 packages; all 172 installed packages
were compatible; pytest collected exactly 1,292 tests; the documentation
contract finished `40 passed in 23.81s`; and patch whitespace was clean. The
pre-commit status listed only this verification record and its synchronized
submission checklist. After the final evidence commit, a separate status check
confirmed a clean `main` worktree.

## 2026-09-07 Operations authority and lifecycle hardening

The final integrity audit added trusted recall/predicate/lot recomputation,
target-bound evidence checks, hold-first lifecycle enforcement, append-only
disposition events, fail-closed independent verification, and persisted
single-use workflow execution grants. The R01-R21 corpus stayed at exactly 21
scenarios; R15, R16, and R20 were revised in place to exercise the strengthened
lifecycle and execution fence.

Commands and observed outcomes:

```bash
uv run pytest tests/unit -q --tb=short
uv run pytest tests/integration/test_workflow.py -q --tb=short
uv run pytest tests/integration/test_mcp_servers.py -q --tb=short
uv run pytest tests/integration/test_retrieval_mcp.py -q --tb=short
uv run pytest tests/docs/test_documentation.py tests/unit/test_evaluation_ranking.py tests/unit/test_evaluation_scorecard.py tests/ui/test_evaluation_reports.py -q --tb=short
make eval-safety
make eval-summary
uv run ruff format --check src/recallops tests/unit/test_gateway.py tests/unit/test_services.py tests/unit/test_operations_sqlite.py tests/integration/test_mcp_servers.py tests/integration/test_workflow.py
uv run ruff check src/recallops tests/unit/test_gateway.py tests/unit/test_services.py tests/unit/test_operations_sqlite.py tests/integration/test_mcp_servers.py tests/integration/test_workflow.py
git diff --check
```

Observed: `812 passed` for unit tests, `92 passed` for the durable workflow,
`23 passed` for direct and real-stdio MCP behavior, `9 passed` for retrieval MCP,
and `351 passed` for documentation/evaluation projection. The transport-inclusive
safety gate reported 21/21 safety-critical scenarios passed; the combined
scorecard reported safety 21/21, retrieval 96 cases/576 results, orchestration 24
cases/48 results, and `Offline evaluation gate: PASSED`. Ruff format/lint and
patch-whitespace checks exited zero.

## 2026-09-07 Runtime-only authorization and evaluator-scope follow-up

The consumer `OperationsService` and MCP gateway surfaces now expose no workflow
reservation, claim, recovery, or grant-issuance method. A runtime-only broker is
bound to the checkpoint store's persisted random UUIDv4 capability. The active
attempt stores the exact confirmed execution ID and execution-request digest;
issuance and atomic consumption both authenticate those bindings alongside the
checkpoint head, workflow request, case/version, action, actor, and operation
request.

R15/R16 lifecycle receipts are explicitly tagged
`privileged_lower_layer_lifecycle_fixture`. They remain evidence for sequence,
integrity, and closure assertions but are excluded from the end-to-end
dual-consent population. A mirrored approval-only fixture cannot authorize a
runtime receipt, and omitted or promoted scope is rejected during persisted
report validation.

Fresh observed results: consumer-service/lifecycle unit tests `63 passed`;
workflow `92 passed`; direct/real-stdio MCP `23 passed`; evaluator end-to-end
`33 passed`; scorecard/tamper validation `241 passed`; safety `21/21` with
`320/320` assertions and all four unsafe counters zero. `make eval-summary`
reported every offline suite passing. The regenerated bindings are safety corpus
`483a56638fcbaa01637c8864a521c2b66eb15694f61b11773411ef501f8d21c3`, safety
report `76dad415cc0c18e79942ce82ca3db6050a559e47e4b2276e2471539dac96a8ad`, and
scorecard `b11edb68db8783d26812434b7e6f5b119ee551195d25aa15e522fd17137d8346`.

## 2026-09-07 Plan-consumption and sequential-dispatch follow-up

This follow-up was executed in the working tree based on `125188a`. The default
runtime now validates the four-role plan, dispatches exactly the next ordered
specialist through a bounded conditional loop, persists cursor/completion/order,
and permits independent verification only after all four typed outputs exist.
The optional Deep Agents integration remains a separate live path; the default
runtime does not claim parallel fan-out.

Fresh observed results:

```text
planner contracts                                      10 passed
new reorder and malformed-plan workflow contracts       9 passed
complete durable workflow integration                   99 passed
durable projection plus Streamlit smoke                 17 passed
documentation contract                                  40 passed
Mermaid stable double-render/parity              10/10 diagrams
safety evaluation                               21/21, 320/320
orchestration evaluation                    24 cases, 48 results
combined offline evaluation gate                        PASSED
```

The malformed-plan matrix covers missing, duplicate, unknown, disallowed,
cyclic, dependency-invalid, and over-budget plans. Every case ended escalated
before specialist dispatch with zero Operations receipts. A valid reorder of
the two dependency-independent roles changed `specialist_execution_order`,
then completed traceability, containment, independent verification, and the
first human-review interrupt without a write. The full 99-test workflow suite
also exercises durable restarts across action, execution, recovery, and closure
interrupts.

Fresh raw artifact SHA-256 bindings are safety report
`1635714574f3471e97b193fe9b91de8fa6f250c6c08bcfb7f8ee511912ccd0e1`,
orchestration report
`95254d8b647a51a7e3c4893bbddbd78a0a90c34e8de9731f34ff0bb4241cf220`,
and scorecard
`506c57a919d7b8f1d420849fe0a80dee10c8fcdbe9933268610c8bccecf9d302`.
The scorecard self-digest is
`bda0d18521f1d5b875c41dc2273b9fbfbf5114292b62382a22f87a26a0b12035`.

One broader unit command was also run and not represented as a pass: it found
40 existing `tests/unit/test_specialists.py` failures rooted in
`deep_supervisor._exact_state()` calling `vars()` on the newly slotted
`OperationsService`, plus tests that directly mutate that former instance
dictionary. One broader UI command likewise retained the existing
`test_product_adapter_routes_exact_lot_through_required_disposition` expectation
that a final `close_case` receipt is absent, while the current hardened service
records that safe close. Neither unrelated result was hidden or changed in this
planner-focused follow-up.
