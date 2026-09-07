"""Immutable observable-only contracts for the orchestration benchmark."""

from __future__ import annotations

import json
import re
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
Intent = Literal["intake", "matching", "lineage", "reconciliation", "containment"]
Criterion = Literal[
    "predicate_cited",
    "scope_classified",
    "lineage_supported",
    "quantities_verified",
    "draft_only",
    "policy_verified",
    "missing_evidence_reported",
    "out_of_scope_reported",
    "facility_tasks_supported",
    "facility_communications_supported",
    "confirmed_holds_supported",
]
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
    intent: Intent
    recall_number: Identifier
    lot_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=3)


class GoldPredicate(FrozenContract):
    product_terms: tuple[StrictStr, ...]
    upcs: tuple[Identifier, ...]
    plant_codes: tuple[Identifier, ...]
    julian_start: StrictInt
    julian_end: StrictInt
    geography: tuple[StrictStr, ...]
    hazard: StrictStr


class ExpectedRead(FrozenContract):
    name: Identifier
    input_sha256: Digest


class OrchestrationCase(InvestigationInput):
    family: Family
    rationale: StrictStr = Field(min_length=1)
    expected_tasks: tuple[Task, ...] = Field(min_length=1)
    expected_routes: tuple[Route, ...] = Field(min_length=1)
    required_specialists: tuple[Specialist, ...] = Field(min_length=1)
    required_tool_families: tuple[Literal["registry", "traceability"], ...] = Field(min_length=1)
    required_tool_order: tuple[Identifier, ...] = Field(min_length=1, max_length=16)
    prohibited_tool_names: tuple[Identifier, ...]
    completion_criteria: tuple[Criterion, ...] = Field(min_length=1)
    evidence_facts: tuple[Identifier, ...] = Field(min_length=1)
    expected_predicate: GoldPredicate
    expected_citations: tuple[Identifier, ...]
    expected_product_catalog_sha256: Digest
    expected_calls: tuple[ExpectedRead, ...]
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
        if tuple(call.name for call in self.expected_calls) != self.required_tool_order:
            raise ValueError("expected call arguments do not cover required tools")
        predicate = self.expected_predicate.model_dump(mode="json")
        field_facts = {
            f"field:{name}:{canonical_sha256(value)}" for name, value in predicate.items()
        }
        field_facts.add(f"field:official_products:{self.expected_product_catalog_sha256}")
        field_facts.update(f"citation:{citation}" for citation in self.expected_citations)
        field_facts.add("source:official_snapshot")
        declared_fields = {
            fact
            for fact in self.evidence_facts
            if fact.startswith(("field:", "citation:", "source:"))
        }
        if declared_fields != (field_facts if self.recall_number == "H-1230-2026" else set()):
            raise ValueError("gold field/citation declarations disagree with predicate")
        for call in self.expected_calls:
            if call.name in {
                "find_candidate_products",
                "match_lots",
            } and call.input_sha256 != canonical_sha256(predicate):
                raise ValueError("gold read arguments disagree with predicate")
            if call.name == "get_recall" and call.input_sha256 != canonical_sha256(
                self.recall_number
            ):
                raise ValueError("gold recall argument mismatch")
        if self.rationale in self.question:
            raise ValueError("operator question leaks evaluator rationale")
        if self.expected_safe_stop in {"error", "unsafe"}:
            raise ValueError("an error or unsafe stop cannot be a gold outcome")
        return self


def audited_predicate():
    """Parse the audited notice structure without importing evaluated agent code."""
    payload = load_recall_snapshot("H-1230-2026", data_dir=DATA_DIR).payload
    description = payload["product_description"]
    markers = list(
        re.finditer(
            r"(?<!\S)([1-9]|1\d|2[0-8])\.\s+(?=Kroger|Brookshire|Super 1|Country Morning|Cal-Maine|Medium Grade|Large Grade|Grade A|Extra Large Grade|Jumbo Grade)",
            description,
        )
    )
    assert [int(m.group(1)) for m in markers] == list(range(1, 29))
    catalog = []
    for index, marker in enumerate(markers):
        text = description[
            marker.end() : markers[index + 1].start()
            if index + 1 < 28
            else description.index("Keep Refrigerated")
        ]
        if index < 21:
            product, remainder = text.split(", UPC", 1)
            upc = "".join(re.findall(r"\d", remainder.split(".", 1)[0]))
            assert len(upc) == 12
        else:
            product = re.split(
                r",\s*(?:MPS Egg Farms|Distributed by MPS Egg Farms)", text, maxsplit=1
            )[0]
            upc = None
        catalog.append(
            {
                "item_number": index + 1,
                "description": " ".join(product.strip().rstrip(".").split()).strip(" ,"),
                "upc": upc,
            }
        )
    # Literal scope was audited against code_info, distribution_pattern and reason_for_recall.
    assert "Codes P-1950 or 0840962" in payload["code_info"]
    assert "between 157 and 184" in payload["code_info"]
    assert (
        payload["distribution_pattern"]
        == "Arkansas, Louisiana, Mississippi, New Mexico, Oklahoma, Texas"
    )
    assert payload["reason_for_recall"] == "Possible Salmonella Enteritidis"
    predicate = {
        "product_terms": [row["description"] for row in catalog],
        "upcs": list(dict.fromkeys(row["upc"] for row in catalog if row["upc"])),
        "plant_codes": ["P-1950", "0840962"],
        "julian_start": 157,
        "julian_end": 184,
        "geography": ["Arkansas", "Louisiana", "Mississippi", "New Mexico", "Oklahoma", "Texas"],
        "hazard": "Possible Salmonella Enteritidis",
    }
    return predicate, catalog


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
        signatures = {
            (case.expected_tasks, case.completion_criteria, case.evidence_facts)
            for case in self.cases
        }
        if len(signatures) != 24:
            raise ValueError("duplicate investigation expectations")
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
    completion_criteria: tuple[Criterion, ...]
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

    @model_validator(mode="after")
    def offline_usage_is_unavailable(self):
        if any(
            row.observation.tokens is not None or row.observation.estimated_cost is not None
            for row in self.results
        ):
            raise ValueError("offline profiles cannot report model tokens or cost")
        return self


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
    status: Literal["not_run_missing_credentials", "completed", "error"]
    excluded_from_offline_gates: StrictBool = True
    error_code: Literal["prohibited_tool_exposure", "factory_error", "runner_error"] | None = None
    provider: Identifier | None = None
    model_sha256: Digest | None = None
    prompt_sha256: Digest | None = None
    repetitions: StrictInt = Field(default=0, ge=0, le=3)
    executed_case_count: Count = 0
    results: tuple[OrchestrationCaseResult, ...] = ()
    tokens: Count | None = None
    estimated_cost: Milliseconds | None = None
    tokens_available: StrictBool = False
    cost_available: StrictBool = False
    duration_ms: StrictFiniteFloat = Field(ge=0)

    @model_validator(mode="after")
    def status_consistent(self):
        if self.excluded_from_offline_gates is not True:
            raise ValueError("live profiles must be excluded from offline gates")
        if self.tokens_available != (self.tokens is not None) or self.cost_available != (
            self.estimated_cost is not None
        ):
            raise ValueError("live usage availability mismatch")
        if self.executed_case_count != len(self.results):
            raise ValueError("live executed count mismatch")
        if self.status == "completed" and (
            self.error_code is not None
            or self.executed_case_count != 24 * self.repetitions
            or not self.repetitions
        ):
            raise ValueError("incomplete live result matrix")
        if self.status != "not_run_missing_credentials" and any(
            value is None for value in (self.provider, self.model_sha256, self.prompt_sha256)
        ):
            raise ValueError("live execution requires provider/model/prompt metadata")
        if self.status == "error" and (self.error_code is None or self.model_sha256 is None):
            raise ValueError("live error requires code and model digest")
        if self.status == "not_run_missing_credentials" and any(
            value is not None
            for value in (self.error_code, self.provider, self.model_sha256, self.prompt_sha256)
        ):
            raise ValueError("not-run live profile cannot claim model execution metadata")
        if self.status == "not_run_missing_credentials" and (
            self.results or self.repetitions or self.tokens_available or self.cost_available
        ):
            raise ValueError("not-run live profile cannot claim execution")
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
    predicate, catalog = audited_predicate()
    for case in corpus.cases:
        if case.expected_predicate.model_dump(
            mode="json"
        ) != predicate or case.expected_product_catalog_sha256 != canonical_sha256(catalog):
            raise ValueError("gold predicate differs from audited snapshot")
        if case.expected_citations != ("openfda:H-1230-2026",):
            raise ValueError("gold citation differs from audited snapshot")
    return corpus
