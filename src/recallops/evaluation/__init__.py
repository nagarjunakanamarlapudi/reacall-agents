"""Deterministic, offline evaluation contracts for RecallOps."""

from recallops.evaluation.retrieval_benchmark import (
    load_retrieval_report,
    run_retrieval_benchmark,
    validate_retrieval_report,
)
from recallops.evaluation.retrieval_schema import RetrievalEvalReport
from recallops.evaluation.runner import (
    EvaluationGateError,
    EvaluationObservation,
    RuntimeProtocol,
    RuntimeScenarioExecutor,
    load_scenarios,
    run_evaluations,
)
from recallops.evaluation.runtime_executor import (
    RecallOpsEvaluationExecutor,
    run_recallops_evaluations,
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
    "RecallOpsEvaluationExecutor",
    "RetrievalEvalReport",
    "ScenarioCorpus",
    "load_scenarios",
    "load_retrieval_report",
    "run_evaluations",
    "run_recallops_evaluations",
    "run_retrieval_benchmark",
    "validate_retrieval_report",
]
