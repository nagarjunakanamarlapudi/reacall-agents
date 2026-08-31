"""Deterministic, offline evaluation contracts for RecallOps."""

from recallops.evaluation.runner import (
    EvaluationGateError,
    EvaluationObservation,
    RuntimeProtocol,
    RuntimeScenarioExecutor,
    load_scenarios,
    run_evaluations,
)
from recallops.evaluation.schema import (
    EvaluationReport,
    EvaluationResult,
    EvaluationScenario,
    ScenarioCorpus,
)

__all__ = [
    "EvaluationGateError",
    "EvaluationObservation",
    "EvaluationReport",
    "EvaluationResult",
    "EvaluationScenario",
    "RuntimeProtocol",
    "RuntimeScenarioExecutor",
    "ScenarioCorpus",
    "load_scenarios",
    "run_evaluations",
]
