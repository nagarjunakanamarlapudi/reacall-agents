"""Typed, JSON-serializable schema for the RecallOps red-team suite."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AssertionOperator = Literal[
    "equals",
    "one_of",
    "contains",
    "contains_all",
    "not_contains",
    "ordered_subsequence",
    "set_equals",
    "empty",
    "nonempty",
    "truthy",
    "falsy",
    "gte",
    "lte",
]

CONTRIBUTION_METRICS = {
    "match_classification_accuracy",
    "lineage_accuracy",
    "quantity_evidence_coverage",
    "gap_detection_recall",
    "approval_guard_rate",
    "idempotency_integrity",
    "closure_guard_rate",
    "recovery_correctness",
    "bounded_execution_rate",
    "retrieval_evidence_coverage",
    "trace_completeness",
}


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "edit", "reject", "escalate"]
    actor: str = Field(min_length=1)
    justification: str = Field(min_length=1)


class PredicateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_terms: list[str] = Field(min_length=1)
    upcs: list[str]
    plant_codes: list[str]
    julian_start: int = Field(ge=1, le=366)
    julian_end: int = Field(ge=1, le=366)
    geography: list[str]
    hazard: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_julian_range(self) -> PredicateInput:
        if self.julian_start > self.julian_end:
            raise ValueError("julian_start must not exceed julian_end")
        return self


class ScenarioInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    recall_number: str = Field(min_length=1)
    question: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    approval: ApprovalInput
    predicate: PredicateInput


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str = Field(min_length=1)
    target: str = Field(min_length=1)
    times: int = Field(default=1, ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class AssertionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    path: str = Field(pattern=r"^/")
    operator: AssertionOperator
    expected: Any = None
    safety_invariant: str = Field(default="", max_length=500)


class ScenarioExpected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: list[str] = Field(min_length=1)
    statuses: list[str] = Field(default_factory=list)
    assertions: list[AssertionSpec] = Field(min_length=1)
    metric_contributions: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_assertion_references(self) -> ScenarioExpected:
        assertion_ids = [assertion.id for assertion in self.assertions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("assertion IDs must be unique within a scenario")
        unknown = {
            assertion_id
            for identifiers in self.metric_contributions.values()
            for assertion_id in identifiers
            if assertion_id not in assertion_ids
        }
        if unknown:
            raise ValueError(
                f"metric contributions reference unknown assertions: {sorted(unknown)}"
            )
        unknown_metrics = set(self.metric_contributions) - CONTRIBUTION_METRICS
        if unknown_metrics:
            raise ValueError(f"unknown contribution metrics: {sorted(unknown_metrics)}")
        return self


class EvaluationScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^R(?:0[1-9]|1[0-9]|2[01])$")
    title: str = Field(min_length=1)
    safety_critical: bool
    setup: dict[str, Any]
    input: ScenarioInput
    faults: list[FaultSpec]
    expected: ScenarioExpected


class ScenarioCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    common_fixture: dict[str, Any]
    common_input: ScenarioInput | None = None
    scenarios: list[EvaluationScenario]

    @model_validator(mode="before")
    @classmethod
    def expand_common_input(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not value.get("common_input"):
            return value
        expanded = dict(value)
        scenarios = []
        for raw_scenario in value.get("scenarios", []):
            scenario = dict(raw_scenario)
            scenario["input"] = _deep_merge(value["common_input"], scenario.get("input", {}))
            scenarios.append(scenario)
        expanded["scenarios"] = scenarios
        return expanded

    @model_validator(mode="after")
    def validate_golden_matrix(self) -> ScenarioCorpus:
        expected_ids = [f"R{number:02d}" for number in range(1, 22)]
        actual_ids = [scenario.id for scenario in self.scenarios]
        if actual_ids != expected_ids:
            raise ValueError("golden corpus must contain exactly R01 through R21 in order")
        if not all(scenario.safety_critical for scenario in self.scenarios):
            raise ValueError("all golden scenarios must be safety-critical")
        by_id = {scenario.id: scenario for scenario in self.scenarios}
        assertions = {
            scenario_id: {
                assertion.id: assertion.expected
                for assertion in by_id[scenario_id].expected.assertions
            }
            for scenario_id in ("R03", "R06", "R17")
        }
        lots = self.common_fixture.get("lots", {})
        exact = lots.get("LOT-EXACT-170", {})
        probable = lots.get("LOT-PROBABLE-160", {})
        quantity_assertions = {
            "received": "received_quantity_exact",
            "on_hand": "on_hand_quantity_exact",
            "quarantined": "quarantined_quantity_exact",
            "sold": "sold_quantity_exact",
            "returned": "returned_quantity_exact",
            "disposed": "disposed_quantity_exact",
            "unaccounted": "exact_unaccounted_visible",
        }
        fixture_bound = (
            assertions["R03"].get("exact_classification") == exact.get("classification")
            and assertions["R03"].get("probable_classification") == probable.get("classification")
            and assertions["R03"].get("ambiguous_classification")
            == lots.get("LOT-AMBIG-175", {}).get("classification")
            and set(assertions["R03"].get("rejected_controls", []))
            == {
                lot_id
                for lot_id, fixture in lots.items()
                if fixture.get("classification") == "rejected"
            }
            and all(
                assertions["R06"].get(assertion_id) == exact.get(field)
                for field, assertion_id in quantity_assertions.items()
            )
            and assertions["R17"].get("valid_close_zero_unaccounted") == probable.get("unaccounted")
        )
        if not fixture_bound:
            raise ValueError("common_fixture must match golden assertions")
        return self


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class AssertionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    passed: bool
    path: str
    operator: str
    expected: Any = None
    actual: Any = None
    detail: str = ""


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    passed: bool
    safety_critical: bool
    assertions: list[AssertionResult]
    route_actual: list[str]
    route_expected: list[str]
    state_excerpt: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    failure_injection: list[dict[str, Any]]
    duration_ms: int = Field(ge=0)
    error: str | None


class EvaluationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_pass_rate: float = Field(ge=0, le=1)
    safety_critical_pass_rate: float = Field(ge=0, le=1)
    route_accuracy: float = Field(ge=0, le=1)
    match_classification_accuracy: float = Field(ge=0, le=1)
    lineage_accuracy: float = Field(ge=0, le=1)
    quantity_evidence_coverage: float = Field(ge=0, le=1)
    gap_detection_recall: float = Field(ge=0, le=1)
    approval_guard_rate: float = Field(ge=0, le=1)
    idempotency_integrity: float = Field(ge=0, le=1)
    closure_guard_rate: float = Field(ge=0, le=1)
    recovery_correctness: float = Field(ge=0, le=1)
    bounded_execution_rate: float = Field(ge=0, le=1)
    retrieval_evidence_coverage: float = Field(ge=0, le=1)
    trace_completeness: float = Field(ge=0, le=1)
    latency_budget_rate: float = Field(ge=0, le=1)
    unauthorized_write_count: int = Field(ge=0)
    duplicate_logical_write_count: int = Field(ge=0)
    false_close_count: int = Field(ge=0)
    receipt_integrity_violation_count: int = Field(ge=0)


class EvaluationRunMetadata(BaseModel):
    """Provenance for values that legitimately differ between executions."""

    model_config = ConfigDict(extra="forbid")

    report_kind: Literal["run_specific_observation"] = "run_specific_observation"
    telemetry_policy: Literal["observed_only"] = "observed_only"
    timing_source: Literal["measured_wall_clock", "scripted_clock"]


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    scenario_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: Literal["offline_deterministic"] = "offline_deterministic"
    run_metadata: EvaluationRunMetadata
    results: list[EvaluationResult]
    metrics: EvaluationMetrics
    gate_passed: bool
