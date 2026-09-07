"""Measured offline orchestration comparison over one read-only evidence boundary.

The generalist and fixed-role adapters share domain validation functions. The
comparison measures deterministic coordination, not independent model quality.
No messages, raw service payloads, or private reasoning enter the report.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

from recallops.agents.deep_supervisor import build_deep_supervisor
from recallops.agents.planner import plan_investigation
from recallops.agents.prompts import (
    CONTAINMENT_COMMUNICATIONS_PROMPT,
    PRODUCT_LOT_MATCHING_PROMPT,
    RECALL_INTELLIGENCE_PROMPT,
    SUPERVISOR_PROMPT,
    TRACEABILITY_RECONCILIATION_PROMPT,
)
from recallops.agents.specialists import (
    ContainmentProposal,
    ProductLotAssessment,
    TraceabilityAssessment,
    assess_product_lots,
    assess_traceability,
    draft_containment,
    investigate_recall,
)
from recallops.data.loaders import load_recall_snapshot
from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256, verify_sha256
from recallops.evaluation.orchestration_schema import (
    OPERATIONS_TOOLS,
    READ_TOOLS,
    ComparisonDeltas,
    InvestigationInput,
    LiveProfileStatus,
    OrchestrationCase,
    OrchestrationCaseResult,
    OrchestrationEvalCorpus,
    OrchestrationEvalReport,
    OrchestrationGates,
    ProfileMetrics,
    ProfileResult,
    ToolCallObservation,
    TrajectoryMetrics,
    TrajectoryObservation,
    evidence_boundary_sha256,
    load_orchestration_cases,
)
from recallops.models import RecallPredicate
from recallops.paths import DATA_DIR
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService


def _family(name: str) -> str:
    if name == "get_recall":
        return "registry"
    if name in READ_TOOLS:
        return "traceability"
    return "operations" if name in OPERATIONS_TOOLS else "unknown"


@dataclass(frozen=True, slots=True)
class ReadOnlyEvidenceGateway:
    """Own only snapshot read services, with a hard bound at each invocation."""

    max_tool_calls: int
    _registry: RecallRegistryService = field(init=False, repr=False)
    _traceability: TraceabilityService = field(init=False, repr=False)
    _calls: list[ToolCallObservation] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self):
        if type(self.max_tool_calls) is not int or not 1 <= self.max_tool_calls <= 16:
            raise ValueError("invalid tool budget")
        object.__setattr__(
            self, "_registry", RecallRegistryService(data_dir=DATA_DIR, source_mode="snapshot")
        )
        object.__setattr__(
            self, "_traceability", TraceabilityService(data_dir=DATA_DIR, source_mode="snapshot")
        )

    def _read(self, name: str, value: Any):
        if name not in READ_TOOLS:
            raise ValueError("capability outside read boundary")
        if len(self._calls) >= self.max_tool_calls:
            raise RuntimeError("tool budget exhausted")
        payload = value.model_dump(mode="json") if isinstance(value, RecallPredicate) else value
        fingerprint = canonical_sha256(payload)
        succeeded = False
        try:
            service = self._registry if name == "get_recall" else self._traceability
            result = getattr(service, name)(value)
            succeeded = True
            return result
        finally:
            self._calls.append(
                ToolCallObservation(
                    name=name,
                    family=_family(name),
                    input_sha256=fingerprint,
                    succeeded=succeeded,
                )
            )

    async def get_recall(self, recall_number):
        return self._read("get_recall", recall_number)

    async def find_candidate_products(self, predicate):
        return self._read("find_candidate_products", predicate)

    async def match_lots(self, predicate):
        return self._read("match_lots", predicate)

    async def trace_forward(self, lot_id):
        return self._read("trace_forward", lot_id)

    async def trace_backward(self, lot_id):
        return self._read("trace_backward", lot_id)

    async def get_inventory(self, lot_id):
        return self._read("get_inventory", lot_id)

    async def reconcile_units(self, lot_id):
        return self._read("reconcile_units", lot_id)


def _exposed_tools() -> tuple[str, ...]:
    # Inspect the actual adapter surface rather than trusting a declared manifest.
    return tuple(
        sorted(
            name
            for name in dir(ReadOnlyEvidenceGateway)
            if not name.startswith("_") and callable(getattr(ReadOnlyEvidenceGateway, name))
        )
    )


def _independent_verify(matching, traceability, proposal) -> bool:
    """Revalidate typed outputs and check policy invariants independently of drafts."""
    matching = ProductLotAssessment.model_validate(matching.model_dump(mode="python"))
    traceability = TraceabilityAssessment.model_validate(traceability.model_dump(mode="python"))
    proposal = ContainmentProposal.model_validate(proposal.model_dump(mode="python"))
    confirmed = set(matching.confirmed_lot_ids)
    ambiguous = set(matching.ambiguous_lot_ids)
    facilities = {
        facility for coverage in traceability.coverage for facility in coverage.facility_evidence
    }
    if confirmed & ambiguous or facilities != set(traceability.affected_facilities):
        return False
    if proposal.executed or not proposal.communication_drafts:
        return False
    held = {
        target
        for action in proposal.proposed_actions
        if action.action_type == "apply_inventory_hold"
        for target in action.target_ids
    }
    return held == confirmed and not held & ambiguous


async def _investigate(
    inputs: InvestigationInput, budget: int, *, fixed: bool
) -> TrajectoryObservation:
    started = perf_counter()
    tasks, specialists, facts, criteria = [], [], [], []
    gateway = None
    stop = "error"
    try:
        gateway = ReadOnlyEvidenceGateway(max_tool_calls=budget)
        if fixed:
            plan = plan_investigation(case_id=inputs.id, question=inputs.question)
            delegation = tuple(todo.specialist.value for todo in plan.todos)
        else:
            delegation = ()

        def delegate(index):
            if fixed:
                specialists.append(delegation[index])

        delegate(0)
        tasks.append("intake")
        recall = await gateway.get_recall(inputs.recall_number)
        if recall is None:
            facts.append(f"recall_missing:{inputs.recall_number}")
            criteria.append("missing_evidence_reported")
            tasks.append("escalate")
            stop = "evidence_gap"
        else:
            intelligence = investigate_recall(recall)
            predicate = intelligence.predicate
            facts.extend(
                [
                    f"recall:{intelligence.recall_number}",
                    f"julian_window:{predicate.julian_start}-{predicate.julian_end}",
                ]
            )
            if (
                intelligence.source_provenance == "OFFICIAL_OPENFDA_SNAPSHOT"
                and intelligence.citations
            ):
                facts.append("source:official_snapshot")
                criteria.append("predicate_cited")
            delegate(1)
            tasks.append("matching")
            products = await gateway.find_candidate_products(predicate)
            all_lots = await gateway.match_lots(predicate)
            by_id = {lot["lot_id"]: lot for lot in all_lots}
            missing = [lot_id for lot_id in inputs.lot_ids if lot_id not in by_id]
            if missing:
                facts.extend(f"lot_missing:{lot_id}" for lot_id in missing)
                criteria.append("missing_evidence_reported")
                tasks.append("escalate")
                stop = "evidence_gap"
            else:
                matching = assess_product_lots(
                    predicate=predicate,
                    candidate_products=products,
                    candidate_lots=[by_id[lot_id] for lot_id in inputs.lot_ids],
                )
                facts.extend(
                    f"classification:{item.lot_id}:{item.classification}"
                    for item in matching.decisions
                )
                criteria.append("scope_classified")
                active = [
                    item.lot_id for item in matching.decisions if item.classification != "rejected"
                ]
                if not active:
                    tasks.append("verify")
                    criteria.append("out_of_scope_reported")
                    stop = "out_of_scope"
                else:
                    delegate(2)
                    tasks.append("lineage")
                    events, positions, reconciliations = [], [], []
                    for lot_id in active:
                        forward = await gateway.trace_forward(lot_id)
                        backward = await gateway.trace_backward(lot_id)
                        if {row["event_id"] for row in forward} != {
                            row["event_id"] for row in backward
                        }:
                            raise ValueError("forward/backward lineage mismatch")
                        events.extend(forward)
                        positions.extend(await gateway.get_inventory(lot_id))
                        reconciliations.append(await gateway.reconcile_units(lot_id))
                    tasks.append("reconciliation")
                    traceability = assess_traceability(
                        lot_ids=active,
                        events=events,
                        inventory_positions=positions,
                        reconciliations=reconciliations,
                    )
                    criteria.extend(["lineage_supported", "quantities_verified"])
                    facts.extend(
                        f"unaccounted:{row.lot_id}:{row.unaccounted}"
                        for row in traceability.reconciliations
                    )
                    delegate(3)
                    tasks.append("containment")
                    proposal = draft_containment(
                        case_id=inputs.id,
                        expected_case_version=0,
                        matching=matching,
                        traceability=traceability,
                    )
                    if proposal.executed is False:
                        criteria.append("draft_only")
                        facts.append("writes_executed:0")
                    tasks.append("verify")
                    if not _independent_verify(matching, traceability, proposal):
                        raise ValueError("independent policy verification failed")
                    criteria.append("policy_verified")
                    facts.append("ambiguous_holds:0")
                    stop = (
                        "evidence_gap"
                        if traceability.evidence_gaps or matching.ambiguous_lot_ids
                        else "human_review"
                    )
    except Exception:
        # An explicit failed observation remains in the denominator. Exceptions can
        # contain raw payloads/provider secrets, so never serialize their message.
        stop = "error"
    calls = tuple(gateway._calls) if gateway is not None else ()
    routes = tuple(
        dict.fromkeys(
            "official" if call.family == "registry" else "synthetic"
            for call in calls
            if call.family in {"registry", "traceability"}
        )
    )
    return TrajectoryObservation(
        tasks=tuple(tasks),
        routes=routes,
        specialists=tuple(specialists),
        tool_calls=calls,
        evidence_facts=tuple(facts),
        completion_criteria=tuple(criteria),
        safe_stop=stop,
        duration_ms=(perf_counter() - started) * 1000.0,
    )


class BoundedSingleAgentProfile:
    name = "bounded_single_agent"

    async def run(self, inputs: InvestigationInput, budget: int) -> TrajectoryObservation:
        return await _investigate(inputs, budget, fixed=False)


class FixedSpecialistsProfile:
    name = "fixed_specialists"

    async def run(self, inputs: InvestigationInput, budget: int) -> TrajectoryObservation:
        return await _investigate(inputs, budget, fixed=True)


def _coverage(expected, actual) -> float:
    return len(set(expected) & set(actual)) / len(set(expected)) if expected else 1.0


def _expected_signatures(case):
    predicate = investigate_recall(load_recall_snapshot("H-1230-2026", data_dir=DATA_DIR)).predicate
    predicate_digest = canonical_sha256(predicate.model_dump(mode="json"))
    active = [
        lot_id
        for lot_id in case.lot_ids
        if any(
            f"classification:{lot_id}:{label}" in case.evidence_facts
            for label in ("exact", "probable", "ambiguous")
        )
    ]
    index = 0
    expected = []
    for name in case.required_tool_order:
        if name == "get_recall":
            fingerprint = canonical_sha256(case.recall_number)
        elif name in {"find_candidate_products", "match_lots"}:
            fingerprint = predicate_digest
        else:
            if index >= len(active):
                raise ValueError("case trace contract exceeds labelled active scope")
            fingerprint = canonical_sha256(active[index])
            if name == "reconcile_units":
                index += 1
        expected.append((name, fingerprint))
    return expected


def score_trajectory(
    case: OrchestrationCase, observation: TrajectoryObservation, profile: str
) -> TrajectoryMetrics:
    """Compute contributions from events, never a model's success assertion."""
    calls = observation.tool_calls
    required_specialists = case.required_specialists if profile != "bounded_single_agent" else ()
    signatures = [(call.name, call.input_sha256) for call in calls]
    duplicate_calls = len(signatures) - len(set(signatures))
    duplicate_work = (
        len(observation.tasks)
        - len(set(observation.tasks))
        + len(observation.specialists)
        - len(set(observation.specialists))
    )
    task_accuracy = _coverage(case.expected_tasks, observation.tasks)
    fact_coverage = _coverage(case.evidence_facts, observation.evidence_facts)
    completion = _coverage(case.completion_criteria, observation.completion_criteria)
    tool_names = tuple(call.name for call in calls)
    families = tuple(dict.fromkeys(call.family for call in calls))
    required_fields = (
        "predicate_cited",
        "scope_classified",
        "lineage_supported",
        "quantities_verified",
    )
    fields = tuple(item for item in case.completion_criteria if item in required_fields)
    delegation_accuracy = float(tuple(observation.specialists) == tuple(required_specialists))
    missing = len(set(required_specialists) - set(observation.specialists))
    prohibited = sum(
        call.name not in READ_TOOLS
        or call.name in case.prohibited_tool_names
        or call.family != _family(call.name)
        for call in calls
    )
    order = (
        tool_names == case.required_tool_order
        and families == case.required_tool_families
        and signatures == _expected_signatures(case)
    )
    route_accuracy = float(observation.routes == case.expected_routes)
    safe = observation.safe_stop == case.expected_safe_stop
    budget = len(calls) <= case.max_tool_calls
    success = (
        task_accuracy == fact_coverage == completion == route_accuracy == delegation_accuracy == 1.0
        and observation.tasks == case.expected_tasks
        and order
        and safe
        and budget
        and not prohibited
        and not duplicate_calls
        and not duplicate_work
        and set(observation.evidence_facts) == set(case.evidence_facts)
        and all(call.succeeded for call in calls)
    )
    return TrajectoryMetrics(
        task_success=success,
        task_accuracy=task_accuracy,
        route_accuracy=route_accuracy,
        required_field_coverage=_coverage(fields, observation.completion_criteria),
        evidence_fact_coverage=fact_coverage,
        completion_criteria_coverage=completion,
        delegation_accuracy=delegation_accuracy,
        missing_specialist_count=missing,
        duplicate_tool_calls=duplicate_calls,
        duplicate_work_count=duplicate_work,
        tool_order_correct=order,
        prohibited_tool_call_count=prohibited,
        safe_stop_correct=safe,
        budget_compliant=budget,
        tool_call_count=len(calls),
        required_task_count=len(case.expected_tasks),
        required_field_count=len(fields),
        required_fact_count=len(case.evidence_facts),
        required_completion_count=len(case.completion_criteria),
        required_specialist_count=len(required_specialists),
    )


def _aggregate(results, exposed) -> ProfileMetrics:
    metrics = [row.metrics for row in results]
    n = len(metrics)
    if not n:
        raise ValueError("empty profile")

    def mean(name):
        return sum(getattr(row, name) for row in metrics) / n

    calls = sum(row.tool_call_count for row in metrics)
    work = sum(len(row.observation.tasks) + len(row.observation.specialists) for row in results)
    durations = sorted(row.observation.duration_ms for row in results)
    exposure = len(set(exposed) - set(READ_TOOLS))
    return ProfileMetrics(
        case_count=n,
        task_success_rate=mean("task_success"),
        task_accuracy=mean("task_accuracy"),
        route_accuracy=mean("route_accuracy"),
        required_field_coverage=mean("required_field_coverage"),
        evidence_fact_coverage=mean("evidence_fact_coverage"),
        completion_criteria_coverage=mean("completion_criteria_coverage"),
        delegation_accuracy=mean("delegation_accuracy"),
        missing_specialist_count=sum(row.missing_specialist_count for row in metrics),
        duplicate_tool_call_ratio=sum(row.duplicate_tool_calls for row in metrics) / calls
        if calls
        else 0.0,
        duplicate_work_ratio=sum(row.duplicate_work_count for row in metrics) / work
        if work
        else 0.0,
        tool_order_accuracy=mean("tool_order_correct"),
        prohibited_tool_exposure_count=exposure,
        prohibited_tool_call_count=sum(row.prohibited_tool_call_count for row in metrics),
        safe_stop_accuracy=mean("safe_stop_correct"),
        budget_compliance=mean("budget_compliant"),
        total_tool_calls=calls,
        total_work_items=work,
        total_duration_ms=sum(durations),
        p50_duration_ms=float(median(durations)),
        p95_duration_ms=durations[math.ceil(n * 0.95) - 1],
    )


def _comparison(profiles):
    baseline, specialists = (profile.metrics for profile in profiles)
    deltas = ComparisonDeltas(
        **{
            name: getattr(specialists, name) - getattr(baseline, name)
            for name in ComparisonDeltas.model_fields
        }
    )
    exposure = sum(profile.metrics.prohibited_tool_exposure_count for profile in profiles)
    prohibited = sum(profile.metrics.prohibited_tool_call_count for profile in profiles)
    gates = OrchestrationGates(
        prohibited_tool_exposure_count=exposure,
        prohibited_tool_call_count=prohibited,
        isolation_passed=exposure == prohibited == 0,
        budget_passed=all(profile.metrics.budget_compliance == 1.0 for profile in profiles),
        task_nonregression_passed=deltas.task_success_rate >= 0,
        evidence_nonregression_passed=deltas.evidence_fact_coverage >= 0,
        trajectory_contracts_passed=all(
            profile.metrics.task_success_rate == 1.0 for profile in profiles
        ),
    )
    passed = all(value for key, value in gates.model_dump().items() if key.endswith("passed"))
    return deltas, gates, passed


async def _run_profile(corpus, adapter):
    results = []
    for case in corpus.cases:
        inputs = InvestigationInput.model_validate(
            case.model_dump(include=set(InvestigationInput.model_fields))
        )
        observation = await adapter.run(inputs, case.max_tool_calls)
        results.append(
            OrchestrationCaseResult(
                case_id=case.id,
                observation=observation,
                metrics=score_trajectory(case, observation, adapter.name),
            )
        )
    exposed = _exposed_tools()
    return ProfileResult(
        name=adapter.name,
        exposed_tool_names=exposed,
        results=tuple(results),
        metrics=_aggregate(results, exposed),
    )


async def _run_live_profile(model: Any) -> LiveProfileStatus:
    """Validate the optional sealed factory, reporting unavailable capture honestly.

    A model opt-in is not evidence of a measured trajectory. Until the sealed
    runtime supplies an observable event bridge, no provider is invoked and no
    live result is manufactured. This status never changes offline gates.
    """
    started = perf_counter()
    model_id = (
        model if type(model) is str else f"{type(model).__module__}.{type(model).__qualname__}"
    )
    provider = model_id.split(":", 1)[0] if ":" in model_id else "unspecified"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
        provider = "unspecified"
    error = "live_capture_unavailable"
    try:
        supervisor = build_deep_supervisor(model=model)
        exposed = set(supervisor.parent_tool_names) | set(supervisor.exposed_read_tool_names)
        exposed.update(name for names in supervisor.subagent_tool_names.values() for name in names)
        if supervisor.operational_write_tool_names or set(OPERATIONS_TOOLS) & exposed:
            error = "prohibited_tool_exposure"
    except Exception:
        error = "factory_error"
    return LiveProfileStatus(
        status="error",
        error_code=error,
        provider=provider,
        model_sha256=canonical_sha256(model_id),
        prompt_sha256=canonical_sha256(
            [
                SUPERVISOR_PROMPT,
                RECALL_INTELLIGENCE_PROMPT,
                PRODUCT_LOT_MATCHING_PROMPT,
                TRACEABILITY_RECONCILIATION_PROMPT,
                CONTAINMENT_COMMUNICATIONS_PROMPT,
            ]
        ),
        duration_ms=(perf_counter() - started) * 1000.0,
    )


def validate_orchestration_report(
    payload: Any, corpus: OrchestrationEvalCorpus
) -> OrchestrationEvalReport:
    """Recompute all scores and compare digests, ordering, denominators and gates."""
    if isinstance(payload, OrchestrationEvalReport):
        payload = payload.model_dump(mode="json")
    report = OrchestrationEvalReport.model_validate(payload)
    verify_sha256(report.model_dump(mode="json", exclude={"report_sha256"}), report.report_sha256)
    if report.orchestration_case_corpus_sha256 != corpus.corpus_sha256:
        raise ValueError("orchestration corpus digest mismatch")
    if report.evidence_boundary_sha256 != corpus.evidence_boundary_sha256:
        raise ValueError("report evidence boundary digest mismatch")
    if report.evidence_boundary_sha256 != evidence_boundary_sha256():
        raise ValueError("current evidence boundary digest mismatch")
    if tuple(profile.name for profile in report.profiles) != (
        "bounded_single_agent",
        "fixed_specialists",
    ):
        raise ValueError("offline profile matrix mismatch")
    for profile in report.profiles:
        if not set(READ_TOOLS) <= set(profile.exposed_tool_names) or len(
            profile.exposed_tool_names
        ) != len(set(profile.exposed_tool_names)):
            raise ValueError("incomplete or duplicate capability manifest")
        if tuple(row.case_id for row in profile.results) != tuple(case.id for case in corpus.cases):
            raise ValueError("profile case coverage mismatch")
        for case, row in zip(corpus.cases, profile.results, strict=True):
            if row.metrics != score_trajectory(case, row.observation, profile.name):
                raise ValueError("trajectory metrics mismatch")
        if profile.metrics != _aggregate(profile.results, profile.exposed_tool_names):
            raise ValueError("aggregate metrics mismatch")
    deltas, gates, passed = _comparison(report.profiles)
    if (report.deltas, report.metrics, report.gate_passed) != (deltas, gates, passed):
        raise ValueError("comparison gates or deltas mismatch")
    return report


def load_orchestration_report(path: Path, case_path: Path) -> OrchestrationEvalReport:
    raw = Path(path).read_bytes()
    payload = json.loads(raw)
    if raw != canonical_json_bytes(payload):
        raise ValueError("orchestration report must use canonical JSON")
    return validate_orchestration_report(payload, load_orchestration_cases(case_path))


async def run_orchestration_benchmark(
    case_path: Path, output_path: Path, live_model: Any | None = None
) -> OrchestrationEvalReport:
    corpus = load_orchestration_cases(case_path)
    profiles = (
        await _run_profile(corpus, BoundedSingleAgentProfile()),
        await _run_profile(corpus, FixedSpecialistsProfile()),
    )
    live = (
        await _run_live_profile(live_model)
        if live_model is not None
        else LiveProfileStatus(status="not_run_missing_credentials", duration_ms=0.0)
    )
    deltas, gates, passed = _comparison(profiles)
    payload = {
        "schema_version": "1.0",
        "execution_mode": "offline_deterministic",
        "calibration": "in_sample_offline_synthetic",
        "measurement": "sequential_perf_counter_including_service_setup",
        "orchestration_case_corpus_sha256": corpus.corpus_sha256,
        "evidence_boundary_sha256": corpus.evidence_boundary_sha256,
        "profiles": [profile.model_dump(mode="json") for profile in profiles],
        "live_status": live.model_dump(mode="json"),
        "deltas": deltas.model_dump(mode="json"),
        "metrics": gates.model_dump(mode="json"),
        "gate_passed": passed,
    }
    payload["report_sha256"] = canonical_sha256(payload)
    report = validate_orchestration_report(payload, corpus)
    Path(output_path).write_bytes(canonical_json_bytes(report.model_dump(mode="json")))
    return report
