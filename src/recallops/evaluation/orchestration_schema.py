"""Immutable observable-only contracts for the orchestration benchmark."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from recallops.data.loaders import load_demo_dataset, load_recall_snapshot
from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256, verify_sha256
from recallops.evaluation.retrieval_schema import StrictFiniteFloat
from recallops.paths import DATA_DIR

Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.:/=?-]+$")]
Rate = Annotated[StrictFiniteFloat, Field(ge=0, le=1)]
Count = Annotated[StrictInt, Field(ge=0)]
Milliseconds = Annotated[StrictFiniteFloat, Field(ge=0)]
Task = Literal[
    "intake", "matching", "lineage", "reconciliation", "containment", "verify", "escalate"
]
Route = Literal["official", "synthetic"]
Specialist = Literal[
    "recall-intelligence",
    "product-lot-matching",
    "traceability-reconciliation",
    "containment-communications",
]
ProfileName = Literal["bounded_single_agent", "fixed_specialists", "deep_agents_live"]
SafeStop = Literal["human_review", "evidence_gap", "out_of_scope", "error", "unsafe"]
Family = Literal[
    "recall_intake",
    "product_lot_matching",
    "lineage",
    "reconciliation",
    "containment_drafting",
    "evidence_gaps",
    "safe_escalation",
]
FAMILY_COUNTS = {
    "recall_intake": 3,
    "product_lot_matching": 4,
    "lineage": 4,
    "reconciliation": 4,
    "containment_drafting": 3,
    "evidence_gaps": 3,
    "safe_escalation": 3,
}
READ_TOOLS = (
    "get_recall",
    "find_candidate_products",
    "match_lots",
    "trace_forward",
    "trace_backward",
    "get_inventory",
    "reconcile_units",
)
OPERATIONS_TOOLS = (
    "create_case",
    "apply_inventory_hold",
    "create_facility_tasks",
    "record_acknowledgment",
    "record_disposition",
    "close_case",
)
SPECIALISTS = (
    "recall-intelligence",
    "product-lot-matching",
    "traceability-reconciliation",
    "containment-communications",
)


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class InvestigationInput(FrozenContract):
    """Only these inputs cross into an adapter; labels remain with the scorer."""

    id: Identifier
    question: StrictStr = Field(min_length=1)
    recall_number: Identifier
    lot_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=3)


class OrchestrationCase(InvestigationInput):
    family: Family
    rationale: StrictStr = Field(min_length=1)
    expected_tasks: tuple[Task, ...] = Field(min_length=1)
    expected_routes: tuple[Route, ...] = Field(min_length=1)
    required_specialists: tuple[Specialist, ...] = Field(min_length=1)
    required_tool_families: tuple[Literal["registry", "traceability"], ...] = Field(min_length=1)
    required_tool_order: tuple[Identifier, ...] = Field(min_length=1, max_length=16)
    prohibited_tool_names: tuple[Identifier, ...]
    completion_criteria: tuple[Identifier, ...] = Field(min_length=1)
    evidence_facts: tuple[Identifier, ...] = Field(min_length=1)
    expected_safe_stop: SafeStop
    max_tool_calls: StrictInt = Field(ge=1, le=16)

    @model_validator(mode="after")
    def valid_case(self):
        for name in (
            "lot_ids",
            "expected_tasks",
            "expected_routes",
            "required_specialists",
            "required_tool_families",
            "prohibited_tool_names",
            "completion_criteria",
            "evidence_facts",
        ):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {name}")
        if not self.question.strip() or not self.rationale.strip():
            raise ValueError("blank case text")
        if set(self.required_tool_order) - set(READ_TOOLS):
            raise ValueError("required tool exceeds read boundary")
        if not set(OPERATIONS_TOOLS) <= set(self.prohibited_tool_names):
            raise ValueError("all Operations tools must be prohibited")
        if set(self.required_tool_order) & set(self.prohibited_tool_names):
            raise ValueError("required/prohibited tool contradiction")
        if len(self.required_tool_order) > self.max_tool_calls:
            raise ValueError("required trajectory exceeds budget")
        if self.expected_safe_stop in {"error", "unsafe"}:
            raise ValueError("an error or unsafe stop cannot be a gold outcome")
        return self


def evidence_boundary_sha256() -> str:
    """Bind all source records actually available to these snapshot services."""
    return canonical_sha256(
        {
            "dataset": load_demo_dataset(DATA_DIR),
            "recall": load_recall_snapshot("H-1230-2026", data_dir=DATA_DIR).model_dump(
                mode="json"
            ),
        }
    )


class OrchestrationEvalCorpus(FrozenContract):
    schema_version: Literal["1.0"] = "1.0"
    source_label: Literal["SYNTHETIC — ACADEMIC DEMO"] = "SYNTHETIC — ACADEMIC DEMO"
    evidence_boundary_sha256: Digest
    cases: tuple[OrchestrationCase, ...] = Field(min_length=24, max_length=24)
    corpus_sha256: Digest

    @model_validator(mode="after")
    def case_matrix(self):
        if len({case.id for case in self.cases}) != 24:
            raise ValueError("duplicate orchestration case IDs")
        if Counter(case.family for case in self.cases) != FAMILY_COUNTS:
            raise ValueError("orchestration family balance mismatch")
        verify_sha256(self.model_dump(mode="json", exclude={"corpus_sha256"}), self.corpus_sha256)
        return self


class ToolCallObservation(FrozenContract):
    name: Identifier
    family: Literal["registry", "traceability", "operations", "unknown"]
    input_sha256: Digest
    succeeded: StrictBool


class TrajectoryObservation(FrozenContract):
    """Only public events and normalized facts; never messages or tool payloads."""

    tasks: tuple[Task, ...]
    routes: tuple[Route, ...]
    specialists: tuple[Specialist, ...]
    tool_calls: tuple[ToolCallObservation, ...]
    evidence_facts: tuple[Identifier, ...]
    completion_criteria: tuple[Identifier, ...]
    safe_stop: SafeStop
    duration_ms: StrictFiniteFloat = Field(ge=0)
    tokens: StrictInt | None = Field(default=None, ge=0)
    estimated_cost: StrictFiniteFloat | None = Field(default=None, ge=0)


class TrajectoryMetrics(FrozenContract):
    task_success: StrictBool
    task_accuracy: Rate
    route_accuracy: Rate
    required_field_coverage: Rate
    evidence_fact_coverage: Rate
    completion_criteria_coverage: Rate
    delegation_accuracy: Rate
    missing_specialist_count: Count
    duplicate_tool_calls: Count
    duplicate_work_count: Count
    tool_order_correct: StrictBool
    prohibited_tool_call_count: Count
    safe_stop_correct: StrictBool
    budget_compliant: StrictBool
    tool_call_count: Count
    required_task_count: Count
    required_field_count: Count
    required_fact_count: Count
    required_completion_count: Count
    required_specialist_count: Count


class OrchestrationCaseResult(FrozenContract):
    case_id: Identifier
    observation: TrajectoryObservation
    metrics: TrajectoryMetrics


class ProfileMetrics(FrozenContract):
    case_count: Count
    task_success_rate: Rate
    task_accuracy: Rate
    route_accuracy: Rate
    required_field_coverage: Rate
    evidence_fact_coverage: Rate
    completion_criteria_coverage: Rate
    delegation_accuracy: Rate
    missing_specialist_count: Count
    duplicate_tool_call_ratio: Rate
    duplicate_work_ratio: Rate
    tool_order_accuracy: Rate
    prohibited_tool_exposure_count: Count
    prohibited_tool_call_count: Count
    safe_stop_accuracy: Rate
    budget_compliance: Rate
    total_tool_calls: Count
    total_work_items: Count
    total_duration_ms: Milliseconds
    p50_duration_ms: Milliseconds
    p95_duration_ms: Milliseconds


class ProfileResult(FrozenContract):
    name: Literal["bounded_single_agent", "fixed_specialists"]
    exposed_tool_names: tuple[Identifier, ...]
    results: tuple[OrchestrationCaseResult, ...]
    metrics: ProfileMetrics


class ComparisonDeltas(FrozenContract):
    """Fixed specialists minus bounded single agent, including negative values."""

    task_success_rate: StrictFiniteFloat
    evidence_fact_coverage: StrictFiniteFloat
    total_tool_calls: StrictInt
    duplicate_tool_call_ratio: StrictFiniteFloat
    total_duration_ms: StrictFiniteFloat
    p50_duration_ms: StrictFiniteFloat
    p95_duration_ms: StrictFiniteFloat


class LiveProfileStatus(FrozenContract):
    name: Literal["deep_agents_live"] = "deep_agents_live"
    status: Literal["not_run_missing_credentials", "error"]
    excluded_from_offline_gates: StrictBool = True
    error_code: (
        Literal["prohibited_tool_exposure", "factory_error", "live_capture_unavailable"] | None
    ) = None
    provider: Identifier | None = None
    model_sha256: Digest | None = None
    prompt_sha256: Digest | None = None
    executed_case_count: StrictInt = Field(default=0, ge=0, le=0)
    duration_ms: StrictFiniteFloat = Field(ge=0)

    @model_validator(mode="after")
    def status_consistent(self):
        if self.excluded_from_offline_gates is not True:
            raise ValueError("live profiles must be excluded from offline gates")
        if self.status == "error" and (self.error_code is None or self.model_sha256 is None):
            raise ValueError("live error requires code and model digest")
        if self.status == "not_run_missing_credentials" and any(
            value is not None
            for value in (self.error_code, self.provider, self.model_sha256, self.prompt_sha256)
        ):
            raise ValueError("not-run live profile cannot claim model execution metadata")
        return self


class OrchestrationGates(FrozenContract):
    prohibited_tool_exposure_count: Count
    prohibited_tool_call_count: Count
    isolation_passed: StrictBool
    budget_passed: StrictBool
    task_nonregression_passed: StrictBool
    evidence_nonregression_passed: StrictBool
    trajectory_contracts_passed: StrictBool


class OrchestrationEvalReport(FrozenContract):
    schema_version: Literal["1.0"] = "1.0"
    execution_mode: Literal["offline_deterministic"] = "offline_deterministic"
    calibration: Literal["in_sample_offline_synthetic"] = "in_sample_offline_synthetic"
    measurement: Literal["sequential_perf_counter_including_service_setup"] = (
        "sequential_perf_counter_including_service_setup"
    )
    orchestration_case_corpus_sha256: Digest
    evidence_boundary_sha256: Digest
    profiles: tuple[ProfileResult, ProfileResult]
    live_status: LiveProfileStatus
    deltas: ComparisonDeltas
    metrics: OrchestrationGates
    gate_passed: StrictBool
    report_sha256: Digest


def load_orchestration_cases(path: Path) -> OrchestrationEvalCorpus:
    raw = Path(path).read_bytes()
    payload = json.loads(raw)
    if raw != canonical_json_bytes(payload):
        raise ValueError("orchestration corpus must use canonical JSON")
    corpus = OrchestrationEvalCorpus.model_validate(payload)
    if corpus.evidence_boundary_sha256 != evidence_boundary_sha256():
        raise ValueError("evidence boundary digest mismatch")
    return corpus
