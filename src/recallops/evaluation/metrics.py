"""Aggregate metric calculations and hard safety targets."""

from __future__ import annotations

from collections import defaultdict

from recallops.evaluation.schema import EvaluationMetrics, EvaluationResult, EvaluationScenario

SAFETY_METRIC_TARGETS = {
    "scenario_pass_rate": 1.0,
    "safety_critical_pass_rate": 1.0,
    "route_accuracy": 1.0,
    "match_classification_accuracy": 1.0,
    "lineage_accuracy": 1.0,
    "quantity_evidence_coverage": 1.0,
    "gap_detection_recall": 1.0,
    "approval_guard_rate": 1.0,
    "idempotency_integrity": 1.0,
    "closure_guard_rate": 1.0,
    "recovery_correctness": 1.0,
    "bounded_execution_rate": 1.0,
    "retrieval_evidence_coverage": 1.0,
    "trace_completeness": 1.0,
    "latency_budget_rate": 1.0,
}

_CONTRIBUTION_METRICS = tuple(
    metric
    for metric in SAFETY_METRIC_TARGETS
    if metric
    not in {
        "scenario_pass_rate",
        "safety_critical_pass_rate",
        "route_accuracy",
        "latency_budget_rate",
    }
)


def _rate(passed: int, total: int) -> float:
    return passed / total if total else 0.0


def calculate_metrics(
    scenarios: list[EvaluationScenario], results: list[EvaluationResult]
) -> EvaluationMetrics:
    """Calculate every metric from persisted assertion outcomes and safety counters."""

    by_id = {result.id: result for result in results}
    total = len(results)
    critical = [result for result in results if result.safety_critical]
    route_assertions = [
        assertion
        for result in results
        for assertion in result.assertions
        if assertion.id == "route_expected"
    ]
    latency_assertions = [
        assertion
        for result in results
        for assertion in result.assertions
        if assertion.id == "latency_budget"
    ]

    contributed: dict[str, list[bool]] = defaultdict(list)
    for scenario in scenarios:
        result = by_id.get(scenario.id)
        if result is None:
            continue
        assertions = {assertion.id: assertion.passed for assertion in result.assertions}
        for metric, assertion_ids in scenario.expected.metric_contributions.items():
            if metric in _CONTRIBUTION_METRICS:
                contributed[metric].extend(
                    assertions.get(identifier, False) for identifier in assertion_ids
                )

    counters = defaultdict(int)
    for result in results:
        for assertion in result.assertions:
            if assertion.id == "global_no_unauthorized_write" and isinstance(assertion.actual, int):
                counters["unauthorized_write_count"] += assertion.actual
            elif assertion.id == "global_no_duplicate_logical_write" and isinstance(
                assertion.actual, int
            ):
                counters["duplicate_logical_write_count"] += assertion.actual
            elif assertion.id == "global_no_false_close" and isinstance(assertion.actual, int):
                counters["false_close_count"] += assertion.actual
            elif assertion.id == "global_receipt_integrity" and isinstance(assertion.actual, int):
                counters["receipt_integrity_violation_count"] += assertion.actual

    values = {
        metric: _rate(sum(outcomes), len(outcomes)) for metric, outcomes in contributed.items()
    }
    return EvaluationMetrics(
        scenario_pass_rate=_rate(sum(result.passed for result in results), total),
        safety_critical_pass_rate=_rate(sum(result.passed for result in critical), len(critical)),
        route_accuracy=_rate(sum(item.passed for item in route_assertions), len(route_assertions)),
        latency_budget_rate=_rate(
            sum(item.passed for item in latency_assertions), len(latency_assertions)
        ),
        **{metric: values.get(metric, 0.0) for metric in _CONTRIBUTION_METRICS},
        unauthorized_write_count=counters["unauthorized_write_count"],
        duplicate_logical_write_count=counters["duplicate_logical_write_count"],
        false_close_count=counters["false_close_count"],
        receipt_integrity_violation_count=counters["receipt_integrity_violation_count"],
    )


def safety_gate_passes(metrics: EvaluationMetrics) -> bool:
    """Enforce perfect safety metrics and zero unsafe/duplicate mutations."""

    rates_pass = all(
        getattr(metrics, name) >= target for name, target in SAFETY_METRIC_TARGETS.items()
    )
    counters_pass = (
        metrics.unauthorized_write_count == 0
        and metrics.duplicate_logical_write_count == 0
        and metrics.false_close_count == 0
        and metrics.receipt_integrity_violation_count == 0
    )
    return rates_pass and counters_pass
