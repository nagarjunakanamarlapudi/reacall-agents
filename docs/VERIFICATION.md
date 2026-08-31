# Verification Evidence

This file records commands actually executed against a named repository state. It does not convert an implementation plan or another worktree’s report into a success claim. The coordinator should append the final integrated-branch audit after UI and evaluator commits are merged.

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

The repository runbook defines the exact install, data, evaluator, CLI, Streamlit, MCP, notebook, test, lint, lock, Python-security, and npm-security commands. Their outcomes must be added here from the final integrated commit rather than inferred from this documentation-only branch.
