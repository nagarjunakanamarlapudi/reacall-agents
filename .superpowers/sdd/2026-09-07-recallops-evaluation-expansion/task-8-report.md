# Task 8 implementation report

## Delivered

- Added `docs/EVALUATION_RUBRIC.md` with anchored 1–5 human-review examples for correctness and citation alignment, completeness, uncertainty and abstention, actionability, and clarity.
- Marked safety decisions deterministic-only. The rubric makes human scores and optional model judges advisory: they cannot approve an action, close a case, or override a failed safety gate.
- Added deterministic notebook 07 through `scripts/build_notebooks.py`. It is self-contained, credential-free, offline, and has no product-package import or dependency on earlier notebooks.
- The lesson uses embedded data to calculate sparse and dense rankings, RRF, reranking, Recall@k, and nDCG. It also demonstrates the 21/96/24 evaluation contracts, signed payload digest verification and tamper rejection, zero rewrite uplift with in-sample limits, and a no-uplift two-profile orchestration comparison.
- Updated all current six-notebook references in the README, Make help, curriculum, submission material, verification note, and notebook contracts to seven.

## Verification

- RED: `uv run pytest -q tests/notebooks/test_notebooks.py` failed as expected before implementation because notebook 07 and the rubric were absent.
- GREEN: `make notebooks` passed twice with `8 passed` each time; the second rebuild produced no notebook diff.
- Generated notebook checks confirmed seven code artifacts have no outputs, no execution counts, and stable cell IDs.
- `uv run ruff check scripts/build_notebooks.py tests/notebooks/test_notebooks.py` passed.
- `uv run pytest -q tests/notebooks/test_notebooks.py tests/docs/test_documentation.py` passed with `30 passed`.
- `git diff --check` passed.

The notebook deliberately treats its literal results as instructional examples, not as new live or production-performance claims.
