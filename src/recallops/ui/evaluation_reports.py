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
from recallops.evaluation.retrieval_benchmark import load_retrieval_report_bytes
from recallops.evaluation.schema import EvaluationReport
from recallops.evaluation.scorecard import (
    load_safety_report,
    load_safety_report_bytes,
    validate_scorecard,
)
from recallops.paths import EvaluationArtifactPaths, RepositoryPaths


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
class EvaluationProjection:
    safety: SafetyProjection = field(default_factory=SafetyProjection)
    retrieval: RetrievalProjection = field(default_factory=RetrievalProjection)
    orchestration: OrchestrationProjection = field(default_factory=OrchestrationProjection)
    verification_status: str = "unavailable"
    artifact_digests: dict[str, str] = field(default_factory=dict)
    generated_at: str | None = None
    execution_mode: str | None = None
    optional_live_status: str = "unavailable"
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
    if not paths.evaluation_report.is_file() or not paths.evaluation_corpus.is_file():
        return {
            **unavailable,
            "status": "missing",
            "message": "Committed evaluation report is missing. Unavailable. No passing score is claimed.",
        }
    try:
        report = load_safety_report(paths.evaluation_report, paths.evaluation_corpus)
    except ValueError as error:
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
    inputs = EvaluationArtifactPaths(
        paths.evaluation_report,
        paths.retrieval_evaluation_report,
        paths.orchestration_evaluation_report,
        paths.evaluation_corpus,
        paths.retrieval_evaluation_corpus,
        paths.orchestration_evaluation_corpus,
    )
    try:
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
    except (OSError, ValueError):
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
        offline_gate_passed=scorecard.offline_gate_passed,
    )
