"""Read-only, fail-closed projections of independently verified evaluation artifacts.

Only aggregate numbers, contract identifiers, and fixed status text cross this
boundary. Integrity verification checks recorded observations, not run authenticity.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from recallops.evaluation.orchestration_benchmark import load_orchestration_report_bytes
from recallops.evaluation.orchestration_schema import LiveProfileStatus
from recallops.evaluation.retrieval_benchmark import load_retrieval_report_bytes
from recallops.evaluation.schema import EvaluationReport
from recallops.evaluation.scorecard import (
    load_safety_report,
    load_safety_report_bytes,
    validate_scorecard,
)
from recallops.paths import EvaluationArtifactPaths, RepositoryPaths
from recallops.ui.presenters import mask_display_value


@dataclass(frozen=True)
class SafetyProjection:
    scenario_count: int | None = None
    result_count: int | None = None
    gate_passed: bool | None = None
    metrics: dict[str, int | float] = field(default_factory=dict)
    scenarios: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RetrievalProjection:
    case_count: int | None = None
    result_count: int | None = None
    gate_passed: bool | None = None
    configurations: tuple[dict[str, Any], ...] = ()
    families: tuple[str, ...] = ()
    gates: dict[str, int | float] = field(default_factory=dict)


@dataclass(frozen=True)
class OrchestrationProjection:
    case_count: int | None = None
    result_count: int | None = None
    gate_passed: bool | None = None
    profiles: tuple[dict[str, Any], ...] = ()
    deltas: dict[str, int | float] = field(default_factory=dict)
    gates: dict[str, int | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionalLiveSummary:
    """Sanitized, immutable scalar summary; no per-case facts or provider messages."""

    status: str = "unavailable"
    excluded_from_offline_gates: bool = True
    provider: str | None = None
    model_sha256: str | None = None
    prompt_sha256: str | None = None
    repetitions: int | None = None
    executed_case_count: int | None = None
    task_success_rate: float | None = None
    evidence_fact_coverage: float | None = None
    budget_compliance: float | None = None
    total_tool_calls: int | None = None
    duplicate_tool_call_ratio: float | None = None
    prohibited_tool_call_count: int | None = None
    duration_ms: float | None = None
    tokens_available: bool = False
    tokens: int | None = None
    cost_available: bool = False
    estimated_cost: float | None = None


def _live_summary(live: LiveProfileStatus) -> OptionalLiveSummary:
    if live.status != "completed":
        return OptionalLiveSummary(status=live.status)
    count = len(live.results)
    calls = sum(row.metrics.tool_call_count for row in live.results)
    # The report validator has already checked every trajectory and the complete
    # repetition matrix. These means follow the offline profile aggregation units.
    return OptionalLiveSummary(
        status=live.status,
        provider=mask_display_value(live.provider),
        model_sha256=live.model_sha256,
        prompt_sha256=live.prompt_sha256,
        repetitions=live.repetitions,
        executed_case_count=live.executed_case_count,
        task_success_rate=sum(row.metrics.task_success for row in live.results) / count,
        evidence_fact_coverage=sum(row.metrics.evidence_fact_coverage for row in live.results)
        / count,
        budget_compliance=sum(row.metrics.budget_compliant for row in live.results) / count,
        total_tool_calls=calls,
        duplicate_tool_call_ratio=sum(row.metrics.duplicate_tool_calls for row in live.results)
        / calls
        if calls
        else 0.0,
        prohibited_tool_call_count=sum(
            row.metrics.prohibited_tool_call_count for row in live.results
        ),
        duration_ms=live.duration_ms,
        tokens_available=live.tokens_available,
        tokens=live.tokens,
        cost_available=live.cost_available,
        estimated_cost=live.estimated_cost,
    )


@dataclass(frozen=True)
class EvaluationProjection:
    safety: SafetyProjection = field(default_factory=SafetyProjection)
    retrieval: RetrievalProjection = field(default_factory=RetrievalProjection)
    orchestration: OrchestrationProjection = field(default_factory=OrchestrationProjection)
    verification_status: str = "unavailable"
    artifact_digests: dict[str, str] = field(default_factory=dict)
    generated_at: str | None = None
    execution_mode: str | None = None
    optional_live_status: str = "unavailable"
    optional_live_summary: OptionalLiveSummary = field(default_factory=OptionalLiveSummary)
    offline_gate_passed: bool = False


def _safety_projection(report: EvaluationReport) -> SafetyProjection:
    return SafetyProjection(
        scenario_count=len(report.results),
        result_count=len(report.results),
        gate_passed=report.gate_passed,
        metrics=report.metrics.model_dump(mode="json"),
        scenarios=tuple(
            {
                "scenario": row.id,
                "safety_critical": row.safety_critical,
                "passed": row.passed,
                "expected": f"{len(row.assertions)} safety assertions",
                "observed": f"{sum(item.passed for item in row.assertions)}/{len(row.assertions)} assertions passed · {row.duration_ms} ms",
                "exception": "Execution error" if row.error else None,
            }
            for row in report.results
        ),
    )


def load_safety_projection(paths: RepositoryPaths) -> dict[str, Any]:
    """Compatibility boundary for case snapshots, using the full safety contract."""
    unavailable = {
        "source": "committed_evaluation_report",
        "gate_passed": None,
        "scenario_count": None,
        "scenarios": [],
        "metrics": {},
        "unsafe_counters": {},
    }
    try:
        if not paths.evaluation_report.is_file() or not paths.evaluation_corpus.is_file():
            return {
                **unavailable,
                "status": "missing",
                "message": "Committed evaluation report is missing. Unavailable. No passing score is claimed.",
            }
        report = load_safety_report(paths.evaluation_report, paths.evaluation_corpus)
    except (OSError, RuntimeError, ValueError) as error:
        stale = "digest mismatch" in str(error)
        return {
            **unavailable,
            "status": "stale" if stale else "invalid",
            "message": "Committed evaluation report is unavailable: "
            + ("corpus digest mismatch." if stale else "invalid artifact.")
            + " No passing score is claimed.",
        }
    section = _safety_projection(report)
    return {
        "status": "verified",
        "source": "committed_evaluation_report",
        "message": f"Committed evaluation report verified: {sum(row.passed for row in report.results)}/{section.scenario_count} scenarios passed; safety gate {'PASS' if report.gate_passed else 'FAIL'}.",
        "gate_passed": section.gate_passed,
        "scenario_count": section.scenario_count,
        "scenarios": list(section.scenarios),
        "metrics": {
            name: value for name, value in section.metrics.items() if not name.endswith("_count")
        },
        "unsafe_counters": {
            name: value for name, value in section.metrics.items() if name.endswith("_count")
        },
    }


def _critic_counts(results) -> dict[str, int]:
    # Stop reasons are a closed enum. Free-text evidence gaps never cross the UI boundary.
    return dict(Counter(row.stop_reason for row in results))


def load_evaluation_scorecard(paths: RepositoryPaths) -> EvaluationProjection:
    """Load without rebuilding. Bind projected immutable bytes to validated digests.

    Capturing before validation and matching its returned digest matrix prevents a
    file replacement between validation and projection from supplying new content.
    The canonical evidence boundary remains the evaluator's repository DATA_DIR.
    """
    try:
        inputs = EvaluationArtifactPaths(
            paths.evaluation_report,
            paths.retrieval_evaluation_report,
            paths.orchestration_evaluation_report,
            paths.evaluation_corpus,
            paths.retrieval_evaluation_corpus,
            paths.orchestration_evaluation_corpus,
        )
        snapshots = {
            name: getattr(inputs, name).read_bytes() for name in inputs.__dataclass_fields__
        }
        scorecard = validate_scorecard(paths.evaluation_scorecard, inputs)
        if any(
            hashlib.sha256(raw).hexdigest() != scorecard.artifact_digests[name]
            for name, raw in snapshots.items()
        ):
            return EvaluationProjection()
        safety = load_safety_report_bytes(snapshots["safety_report"], snapshots["safety_corpus"])
        retrieval = load_retrieval_report_bytes(
            snapshots["retrieval_report"], snapshots["retrieval_corpus"]
        )
        orchestration = load_orchestration_report_bytes(
            snapshots["orchestration_report"], snapshots["orchestration_corpus"]
        )
    except (OSError, RuntimeError, ValueError):
        # Validation exceptions can contain attacker-controlled data. Never render them.
        return EvaluationProjection()
    summaries = {item.name: item for item in scorecard.suite_summaries}
    return EvaluationProjection(
        safety=_safety_projection(safety),
        retrieval=RetrievalProjection(
            case_count=summaries["retrieval"].case_count,
            result_count=summaries["retrieval"].result_count,
            gate_passed=retrieval.gate_passed,
            configurations=tuple(
                {
                    "name": item.name,
                    "metrics": item.metrics.model_dump(mode="json"),
                    "family_metrics": {
                        name: metrics.model_dump(mode="json")
                        for name, metrics in item.family_metrics.items()
                    },
                    "critic_stops": _critic_counts(item.results),
                    "family_critic_stops": {
                        name: _critic_counts(row for row in item.results if row.family == name)
                        for name in item.family_metrics
                    },
                }
                for item in retrieval.configurations
            ),
            families=tuple(retrieval.configurations[0].family_metrics),
            gates=retrieval.gates.model_dump(mode="json"),
        ),
        orchestration=OrchestrationProjection(
            case_count=summaries["orchestration"].case_count,
            result_count=summaries["orchestration"].result_count,
            gate_passed=orchestration.gate_passed,
            profiles=tuple(
                {"profile": item.name, **item.metrics.model_dump(mode="json")}
                for item in orchestration.profiles
            ),
            deltas=orchestration.deltas.model_dump(mode="json"),
            gates=orchestration.metrics.model_dump(mode="json"),
        ),
        verification_status="verified",
        artifact_digests={**scorecard.artifact_digests, "scorecard": scorecard.scorecard_sha256},
        generated_at=scorecard.generated_at.isoformat(),
        execution_mode=scorecard.execution_mode,
        optional_live_status=scorecard.optional_live_status.status,
        optional_live_summary=_live_summary(orchestration.live_status),
        offline_gate_passed=scorecard.offline_gate_passed,
    )
