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
