"""Deterministic, offline evaluation contracts for RecallOps."""

from recallops.evaluation.orchestration_benchmark import (
    LiveProgram,
    LiveRunnerFactory,
    LiveUsage,
    load_orchestration_report,
    run_orchestration_benchmark,
    validate_orchestration_report,
)
from recallops.evaluation.orchestration_schema import (
    OrchestrationEvalReport,
    load_orchestration_cases,
)
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
    "LiveProgram",
    "LiveRunnerFactory",
    "LiveUsage",
    "OrchestrationEvalReport",
    "load_orchestration_cases",
    "load_orchestration_report",
    "run_orchestration_benchmark",
    "validate_orchestration_report",
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
