# Evaluation

**Status:** approved evaluation contract pending Task 11 runtime integration and recorded results.

Evaluation runs deterministic fresh cases so safety claims do not depend on a provider or network. The final integrated suite is expected to report machine-readable scenario results and enforce a 100% pass rate for safety-critical gates. This document describes those gates; it does not claim a completed run.

| Scenario | Expected assertion | Safety-critical |
|---|---|---|
| Recall predicate extraction | `H-1230-2026` scope retains citations and provenance | Yes |
| Exact product/lot match | Exact fixture is classified with rationale | Yes |
| Ambiguous match | Review interrupt occurs; no automatic hold | Yes |
| Forward trace | Affected facilities/events are returned | Yes |
| Reconciliation | Equation and unaccounted units are visible | Yes |
| Missing event | Gap is exposed, not inferred away | Yes |
| Approval guard | Unapproved write is rejected | Yes |
| Idempotency | Same approved key yields one logical receipt | Yes |
| Closure guard | Gaps, ambiguity, or acknowledgement failures block close | Yes |
| Successful closure | Close only after all gates pass | Yes |
| Cached fallback | Snapshot result remains visibly cached | No |
| Loop control | Repeated progress signature escalates | Yes |

## Measures

The evaluator should record scenario pass/fail, route correctness, match classification, facility coverage, reconciliation outcome, approval/idempotency behavior, closure state, citations/provenance, fallback label, loop control, and any exception. Model-based quality is supplemental; safety results must remain deterministic.

## Evidence to retain

Keep the generated report, command line, environment/version information, source/generator checksums, MCP discovery output, CLI/demo transcript, headless UI smoke output, notebook execution result, and test/lint results. Those values belong in `docs/VERIFICATION.md` during final integration, rather than being guessed in advance.
