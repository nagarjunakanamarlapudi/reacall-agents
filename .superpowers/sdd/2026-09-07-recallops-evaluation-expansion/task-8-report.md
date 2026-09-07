# Task 8 implementation report

## Delivered

- Added `docs/EVALUATION_RUBRIC.md` with anchored 1–5 human-review examples for correctness and citation alignment, completeness, uncertainty and abstention, actionability, and clarity.
- Marked safety decisions deterministic-only. The rubric makes human scores and optional model judges advisory: they cannot approve an action, close a case, or override a failed safety gate.
- Added deterministic notebook 07 through `scripts/build_notebooks.py`. It is self-contained, credential-free, offline, and has no product-package import or dependency on earlier notebooks.
- The lesson uses embedded data to calculate sparse and dense rankings, RRF, reranking, Recall@k, and nDCG. It also demonstrates the 21/96/24 evaluation contracts, digest-bound payload verification and tamper rejection, zero rewrite uplift with in-sample limits, and a no-uplift two-profile orchestration comparison.
- Updated all current six-notebook references in the README, Make help, curriculum, submission material, verification note, and notebook contracts to seven.

## Verification

- RED: `uv run pytest -q tests/notebooks/test_notebooks.py` failed as expected before implementation because notebook 07 and the rubric were absent.
- GREEN: `make notebooks` passed twice with `8 passed` each time; the second rebuild produced no notebook diff.
- Generated notebook checks confirmed seven code artifacts have no outputs, no execution counts, and stable cell IDs.
- `uv run ruff check scripts/build_notebooks.py tests/notebooks/test_notebooks.py` passed.
- `uv run pytest -q tests/notebooks/test_notebooks.py tests/docs/test_documentation.py` passed with `30 passed`.
- `git diff --check` passed.

The notebook deliberately treats its literal results as instructional examples, not as new live or production-performance claims.

## Fix round 1

- Replaced the teaching sparse overlap score with a literal BM25-like calculation using IDF, saturated term frequency, and document-length normalization. The embedded corpus now gives distinct BM25, dense-cosine, RRF, and citation-bonus rerank orderings.
- Added an executable bounded agentic-RAG trace and event-derived bounded-generalist/fixed-specialist trajectory metrics, including calls, four-specialist/delegation/order coverage, duplicate rate, read-only tool boundary, and an excluded `not_run_missing_credentials` live status.
- Calculated positive fusion/rerank, zero rewrite, and negative agentic-versus-rerank comparisons from a separately labelled committed in-sample snapshot. Replaced the earlier checksum wording with digest-bound SHA-256 language and its non-authentication limitation.
- Added a fillable rubric worksheet and advisory judge protocol for fixed rubric/prompt digests, anonymous randomized A/B labels, repeated independent judgments, distributions, variance, and adjudication.
- Restored the historical six-notebook verification statement and appended a dated Task 8 verification record instead of rewriting it.

Verification for this fix: the strengthened contracts first failed against the previous notebook. After implementation, `make notebooks` passed twice with `9 passed` and identical notebook 07 bytes; all seven notebooks executed from fresh temporary working directories; documentation tests passed (`22 passed`); Ruff and diff checks passed.

## Fix round 2

- Strengthened fixed-specialist delegation scoring so it requires exactly one planner delegation for every required task, with the expected specialist role. Adversarial tests remove one delegation and all delegations; both now score `0.0`.
- Strengthened sequence scoring so every task follows plan → expected delegation (for specialists) → specialist tool call → completion → independent verifier. An adversarial early-verifier trajectory now scores `0.0` for order accuracy.
- Removed the obsolete unused `notebook_07_legacy()` builder. The teaching text now qualifies middleware/HITL as architecture while the local trace assertions observe only the literal middleware tuple and read-only/no-Operations events.

Verification for this fix: `make notebooks` ran twice with `10 passed` each time and identical notebook 07 bytes. The focused notebook plus documentation suite passed (`32 passed`); Ruff check/format and `git diff --check` passed. The focused clean-temporary-working-directory notebook execution remains covered by the notebook contract.
