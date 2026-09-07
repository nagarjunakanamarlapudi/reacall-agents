"""Measured offline orchestration comparison over one read-only evidence boundary.

The generalist and fixed-role adapters share domain validation functions. The
comparison measures deterministic coordination, not independent model quality.
No messages, raw service payloads, or private reasoning enter the report.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

from recallops.agents.deep_supervisor import (
    _make_read_config,
    _make_sealed_capability,
    build_deep_supervisor,
)
from recallops.agents.planner import InvestigationPlan, plan_investigation
from recallops.agents.prompts import (
    SUPERVISOR_PROMPT,
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
from recallops.config import Settings
from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256, verify_sha256
from recallops.evaluation.orchestration_schema import (
    OPERATIONS_TOOLS,
    READ_TOOLS,
    ComparisonDeltas,
    Count,
    FrozenContract,
    InvestigationInput,
    LiveProfileStatus,
    Milliseconds,
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
from recallops.models import InventoryPosition, RecallPredicate, RecallRecord, TraceEvent
from recallops.paths import DATA_DIR
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService

_CASE_READS: ContextVar[tuple[list[ToolCallObservation], int] | None] = ContextVar(
    "orchestration_case_reads", default=None
)


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
    sealed: bool = False
    _registry: RecallRegistryService = field(init=False, repr=False)
    _traceability: TraceabilityService = field(init=False, repr=False)
    _calls: list[ToolCallObservation] = field(default_factory=list, init=False, repr=False)
    _capabilities: tuple = field(default=(), init=False, repr=False)

    def __post_init__(self):
        if type(self.max_tool_calls) is not int or not 1 <= self.max_tool_calls <= 16:
            raise ValueError("invalid tool budget")
        object.__setattr__(
            self, "_registry", RecallRegistryService(data_dir=DATA_DIR, source_mode="snapshot")
        )
        object.__setattr__(
            self, "_traceability", TraceabilityService(data_dir=DATA_DIR, source_mode="snapshot")
        )
        if self.sealed:
            config = _make_read_config(
                "direct", Settings(data_dir=DATA_DIR, source_mode="snapshot")
            )
            object.__setattr__(
                self,
                "_capabilities",
                tuple(_make_sealed_capability(config, name) for name in READ_TOOLS),
            )

    async def _read(self, name: str, value: Any):
        if name not in READ_TOOLS:
            raise ValueError("capability outside read boundary")
        if len(self._calls) >= self.max_tool_calls:
            raise RuntimeError("tool budget exhausted")
        capture = _CASE_READS.get()
        if capture is not None and len(capture[0]) >= capture[1]:
            raise RuntimeError("case tool budget exhausted")
        payload = value.model_dump(mode="json") if isinstance(value, RecallPredicate) else value
        fingerprint = canonical_sha256(payload)
        succeeded = False
        try:
            if self.sealed:
                argument = (
                    "recall_number"
                    if name == "get_recall"
                    else "predicate"
                    if name in {"find_candidate_products", "match_lots"}
                    else "lot_id"
                )
                result = await self._capabilities[READ_TOOLS.index(name)](**{argument: value})
            else:
                service = self._registry if name == "get_recall" else self._traceability
                result = getattr(service, name)(value)
            succeeded = True
            return result
        finally:
            observation = ToolCallObservation(
                name=name,
                family=_family(name),
                input_sha256=fingerprint,
                succeeded=succeeded,
            )
            self._calls.append(observation)
            if capture is not None:
                capture[0].append(observation)

    async def get_recall(self, recall_number):
        value = await self._read("get_recall", recall_number)
        return RecallRecord.model_validate(value) if value is not None else None

    async def find_candidate_products(self, predicate):
        return await self._read("find_candidate_products", predicate)

    async def match_lots(self, predicate):
        return await self._read("match_lots", predicate)

    async def trace_forward(self, lot_id):
        return await self._read("trace_forward", lot_id)

    async def trace_backward(self, lot_id):
        return await self._read("trace_backward", lot_id)

    async def get_inventory(self, lot_id):
        return await self._read("get_inventory", lot_id)

    async def reconcile_units(self, lot_id):
        return await self._read("reconcile_units", lot_id)


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
    """Verify complete target coverage from typed source evidence, not draft claims."""
    matching = ProductLotAssessment.model_validate(matching.model_dump(mode="python"))
    traceability = TraceabilityAssessment.model_validate(traceability.model_dump(mode="python"))
    proposal = ContainmentProposal.model_validate(proposal.model_dump(mode="python"))
    confirmed, ambiguous = set(matching.confirmed_lot_ids), set(matching.ambiguous_lot_ids)
    facilities = {f for coverage in traceability.coverage for f in coverage.facility_evidence}
    if (
        confirmed & ambiguous
        or facilities != set(traceability.affected_facilities)
        or proposal.executed
    ):
        return False

    def targets(action_type):
        return {
            target
            for action in proposal.proposed_actions
            if action.action_type == action_type
            for target in action.target_ids
        }

    facility_drafts = [
        draft for draft in proposal.communication_drafts if draft.audience == "facility"
    ]
    manager_drafts = [
        draft for draft in proposal.communication_drafts if draft.audience == "food_safety_manager"
    ]
    lot_sources = {
        row.lot_id: set(row.event_ids)
        | set(row.inventory_evidence_ids)
        | set(row.reconciliation_evidence_ids)
        for row in traceability.coverage
    }
    facility_sources = {
        facility: {
            evidence
            for row in traceability.coverage
            if facility in row.facility_evidence
            for evidence in (*row.facility_evidence[facility], *row.reconciliation_evidence_ids)
        }
        for facility in facilities
    }

    def supported(item, sources):
        expected = {target: sources.get(target, set()) for target in item.target_ids}
        return (
            bool(expected)
            and all(expected.values())
            and {target: set(ids) for target, ids in item.evidence_by_target.items()} == expected
            and set(item.evidence_ids) == set().union(*expected.values())
        )

    return (
        targets("apply_inventory_hold") == confirmed
        and targets("create_facility_tasks") == facilities
        and bool(facility_drafts)
        and bool(manager_drafts)
        and {target for draft in facility_drafts for target in draft.target_ids} == facilities
        and {target for draft in manager_drafts for target in draft.target_ids}
        == confirmed | ambiguous
        and all(
            action.action_type in {"apply_inventory_hold", "create_facility_tasks"}
            and supported(
                action,
                lot_sources if action.action_type == "apply_inventory_hold" else facility_sources,
            )
            for action in proposal.proposed_actions
        )
        and all(supported(draft, facility_sources) for draft in facility_drafts)
        and all(supported(draft, lot_sources) for draft in manager_drafts)
        and len(facility_drafts) + len(manager_drafts) == len(proposal.communication_drafts)
        and set(proposal.all_cited_evidence_ids)
        == {
            e
            for item in (*proposal.proposed_actions, *proposal.communication_drafts)
            for e in item.evidence_ids
        }
    )


def _validated_lineage(lot_id, forward, backward):
    """Require typed content agreement and real ancestry in both observed directions."""
    left = [TraceEvent.model_validate(row) for row in forward]
    right = [TraceEvent.model_validate(row) for row in backward]
    by_id = {row.event_id: row for row in left}
    if not left or len(by_id) != len(left) or len(right) != len(left):
        raise ValueError("incomplete or duplicate lineage")
    if len({row.event_id for row in right}) != len(right):
        raise ValueError("duplicate backward lineage")
    if any(row.lot_id != lot_id for row in (*left, *right)):
        raise ValueError("lineage lot scope mismatch")
    if {row.event_id: row for row in right} != by_id:
        raise ValueError("forward/backward source record mismatch")
    depths, visiting = {}, set()

    def depth(identifier):
        if identifier in visiting:
            raise ValueError("lineage cycle")
        if identifier in depths:
            return depths[identifier]
        visiting.add(identifier)
        event = by_id[identifier]
        if event.parent_event_id:
            if event.parent_event_id not in by_id:
                raise ValueError("missing lineage parent")
            parent = by_id[event.parent_event_id]
            if (parent.to_facility or parent.from_facility) != (
                event.from_facility or event.to_facility
            ):
                raise ValueError("lineage facility continuity mismatch")
            value = depth(event.parent_event_id) + 1
        else:
            if event.event_type != "receiving" or not event.to_facility:
                raise ValueError("invalid receiving root")
            value = 0
        visiting.remove(identifier)
        depths[identifier] = value
        return value

    for row in left:
        depth(row.event_id)
    fpos = {row.event_id: index for index, row in enumerate(left)}
    bpos = {row.event_id: index for index, row in enumerate(right)}
    if any(
        row.parent_event_id
        and (
            fpos[row.parent_event_id] >= fpos[row.event_id]
            or bpos[row.parent_event_id] <= bpos[row.event_id]
        )
        for row in left
    ):
        raise ValueError("incorrect forward/backward ancestry order")
    if [depths[row.event_id] for row in right] != sorted(
        (depths[row.event_id] for row in right), reverse=True
    ):
        raise ValueError("backward lineage is not descending ancestry depth")
    return left


class InvestigationSession:
    """Bounded task workers shared below the two independent schedulers."""

    def __init__(self, inputs, budget, *, sealed=False):
        self.started = perf_counter()
        self.inputs = inputs
        self.gateway = ReadOnlyEvidenceGateway(max_tool_calls=budget, sealed=sealed)
        self.tasks, self.specialists, self.facts, self.criteria = [], [], [], []
        self.stop = "human_review"
        self.halted = False
        self.intelligence = self.matching = self.traceability = None
        self.active, self.events, self.inventory, self.reconciliations = [], [], [], []

    def escalate(self, fact):
        self.facts.append(fact)
        self.criteria.append("missing_evidence_reported")
        self.tasks.append("escalate")
        self.stop, self.halted = "evidence_gap", True

    async def intake(self):
        self.tasks.append("intake")
        recall = await self.gateway.get_recall(self.inputs.recall_number)
        if recall is None:
            self.escalate(f"recall_missing:{self.inputs.recall_number}")
            return
        self.intelligence = investigate_recall(recall)
        value = self.intelligence
        self.facts.extend(
            f"field:{name}:{canonical_sha256(item)}"
            for name, item in value.predicate.model_dump(mode="json").items()
        )
        self.facts.append(
            f"field:official_products:{canonical_sha256([item.model_dump(mode='json') for item in value.official_products])}"
        )
        self.facts.extend(f"citation:{citation}" for citation in value.citations)
        if value.source_provenance == "OFFICIAL_OPENFDA_SNAPSHOT":
            self.facts.append("source:official_snapshot")
        if value.citations:
            self.criteria.append("predicate_cited")

    async def matching_task(self):
        self.tasks.append("matching")
        predicate = self.intelligence.predicate
        products = await self.gateway.find_candidate_products(predicate)
        lots = await self.gateway.match_lots(predicate)
        by_id = {row["lot_id"]: row for row in lots}
        missing = [lot for lot in self.inputs.lot_ids if lot not in by_id]
        if missing:
            partial = assess_product_lots(
                predicate=predicate,
                candidate_products=products,
                candidate_lots=[by_id[lot] for lot in self.inputs.lot_ids if lot in by_id],
            )
            self.facts.extend(
                f"classification:{row.lot_id}:{row.classification}" for row in partial.decisions
            )
            self.escalate(f"lot_missing:{missing[0]}")
            self.facts.extend(f"lot_missing:{lot}" for lot in missing[1:])
            return
        self.matching = assess_product_lots(
            predicate=predicate,
            candidate_products=products,
            candidate_lots=[by_id[lot] for lot in self.inputs.lot_ids],
        )
        self.facts.extend(
            f"classification:{row.lot_id}:{row.classification}" for row in self.matching.decisions
        )
        self.criteria.append("scope_classified")
        self.active = [
            row.lot_id for row in self.matching.decisions if row.classification != "rejected"
        ]
        if not self.active:
            self.criteria.append("out_of_scope_reported")
            self.stop, self.halted = "out_of_scope", True
        elif self.matching.ambiguous_lot_ids:
            self.stop = "evidence_gap"

    async def lineage(self):
        self.tasks.append("lineage")
        for lot in self.active:
            forward = await self.gateway.trace_forward(lot)
            backward = await self.gateway.trace_backward(lot)
            records = _validated_lineage(lot, forward, backward)
            self.events.extend(records)
            self.facts.append(
                f"lineage:{lot}:{canonical_sha256({row.event_id: row.model_dump(mode='json') for row in records})}"
            )
        self.criteria.append("lineage_supported")

    async def reconciliation(self):
        self.tasks.append("reconciliation")
        for lot in self.active:
            positions = [
                InventoryPosition.model_validate(row)
                for row in await self.gateway.get_inventory(lot)
            ]
            self.inventory.extend(positions)
            self.facts.append(
                f"inventory:{lot}:{canonical_sha256([row.model_dump(mode='json') for row in positions])}"
            )
            self.reconciliations.append(await self.gateway.reconcile_units(lot))
        self.traceability = assess_traceability(
            lot_ids=self.active,
            events=self.events,
            inventory_positions=self.inventory,
            reconciliations=self.reconciliations,
        )
        for row in self.traceability.reconciliations:
            self.facts.extend(
                f"quantity:{row.lot_id}:{name}:{getattr(row, name)}"
                for name in (
                    "received",
                    "on_hand",
                    "quarantined",
                    "sold",
                    "returned",
                    "disposed",
                    "unaccounted",
                )
            )
        if self.traceability.evidence_gaps:
            self.stop = "evidence_gap"
        self.criteria.append("quantities_verified")

    async def containment(self):
        self.tasks.append("containment")
        proposal = draft_containment(
            case_id=self.inputs.id,
            expected_case_version=0,
            matching=self.matching,
            traceability=self.traceability,
        )
        for action in proposal.proposed_actions:
            prefix = (
                "hold_target"
                if action.action_type == "apply_inventory_hold"
                else "facility_task_target"
            )
            self.facts.extend(f"{prefix}:{target}" for target in sorted(action.target_ids))
        self.facts.extend(
            f"facility_message_target:{target}"
            for draft in proposal.communication_drafts
            if draft.audience == "facility"
            for target in sorted(draft.target_ids)
        )
        if not proposal.executed:
            self.facts.append("writes_executed:0")
            self.criteria.append("draft_only")
        if not _independent_verify(self.matching, self.traceability, proposal):
            raise ValueError("independent containment verification failed")
        self.facts.append("ambiguous_holds:0")
        self.criteria.extend(
            (
                "policy_verified",
                "facility_tasks_supported",
                "facility_communications_supported",
                "confirmed_holds_supported",
            )
        )

    def finish(self):
        if self.stop != "error" and (not self.tasks or self.tasks[-1] != "escalate"):
            self.tasks.append("verify")
        calls = tuple(self.gateway._calls)
        return TrajectoryObservation(
            tasks=tuple(self.tasks),
            routes=tuple(
                dict.fromkeys(
                    "official" if call.family == "registry" else "synthetic" for call in calls
                )
            ),
            specialists=tuple(self.specialists),
            tool_calls=calls,
            evidence_facts=tuple(self.facts),
            completion_criteria=tuple(self.criteria),
            safe_stop=self.stop,
            duration_ms=(perf_counter() - self.started) * 1000.0,
        )


class BoundedSingleAgentProfile:
    name = "bounded_single_agent"

    async def run(self, inputs: InvestigationInput, budget: int) -> TrajectoryObservation:
        session = InvestigationSession(inputs, budget)
        # A generalist's own sequential plan, independent of the specialist planner.
        sequence = ("intake", "matching_task", "lineage", "reconciliation", "containment")
        limit = {"intake": 1, "matching": 2, "lineage": 3, "reconciliation": 4, "containment": 5}[
            inputs.intent
        ]
        try:
            for task in sequence[:limit]:
                await getattr(session, task)()
                if session.halted:
                    break
        except Exception:
            session.stop = "error"
        return session.finish()


# Audited dispatch contracts: changing a planner task is not silently ignored.
_DISPATCH_CONTRACT = (
    (
        "recall-intelligence",
        "Extract the authoritative recall predicate and source citations.",
        "Product, UPC, plant, date window, geography, hazard, and provenance are cited.",
    ),
    (
        "product-lot-matching",
        "Classify internal products and lots against the recall predicate.",
        "Every candidate is exact, probable, ambiguous, or rejected with field rationale.",
    ),
    (
        "traceability-reconciliation",
        "Trace affected lots and reconcile units by facility.",
        "Lineage, facility coverage, component evidence, and quantity gaps are explicit.",
    ),
    (
        "containment-communications",
        "Draft evidence-cited containment actions and communications for review.",
        "Drafts cite known evidence, exclude ambiguous holds, and execute no writes.",
    ),
)


def _validated_plan(inputs):
    plan = plan_investigation(case_id=inputs.id, question=inputs.question)
    plan = InvestigationPlan.model_validate(plan.model_dump(mode="python"))
    if plan.case_id != inputs.id or plan.objective != inputs.question.strip():
        raise ValueError("planner context mismatch")
    actual = tuple(
        (todo.specialist.value, todo.task, todo.completion_criteria) for todo in plan.todos
    )
    if actual != _DISPATCH_CONTRACT:
        raise ValueError("unsupported specialist task or order")
    if any(
        todo.todo_id != f"todo-{index}" or todo.status != "pending"
        for index, todo in enumerate(plan.todos, 1)
    ):
        raise ValueError("invalid planner completion state")
    return plan


async def _dispatch_specialist(session, role):
    if role == "recall-intelligence":
        await session.intake()
    elif role == "product-lot-matching":
        await session.matching_task()
    elif role == "traceability-reconciliation":
        await session.lineage()
        if session.inputs.intent in {"reconciliation", "containment"}:
            await session.reconciliation()
    elif role == "containment-communications":
        await session.containment()
    else:
        raise ValueError("unknown specialist")


class FixedSpecialistsProfile:
    name = "fixed_specialists"

    async def run(self, inputs: InvestigationInput, budget: int) -> TrajectoryObservation:
        session = InvestigationSession(inputs, budget)
        try:
            plan = _validated_plan(inputs)
            applicable = {
                "intake": 1,
                "matching": 2,
                "lineage": 3,
                "reconciliation": 3,
                "containment": 4,
            }[inputs.intent]
            for todo in plan.todos[:applicable]:
                todo.status = "in_progress"
                session.specialists.append(todo.specialist.value)
                await _dispatch_specialist(session, todo.specialist.value)
                if not session.halted:
                    required = {
                        "recall-intelligence": "predicate_cited",
                        "product-lot-matching": "scope_classified",
                        "traceability-reconciliation": "lineage_supported",
                        "containment-communications": "policy_verified",
                    }[todo.specialist.value]
                    if required not in session.criteria:
                        raise ValueError("specialist completion criteria not met")
                    todo.status = "completed"
                if session.halted:
                    break
        except Exception:
            session.stop = "error"
        return session.finish()


def _coverage(expected, actual) -> float:
    return len(set(expected) & set(actual)) / len(set(expected)) if expected else 1.0


def _expected_signatures(case):
    return [(call.name, call.input_sha256) for call in case.expected_calls]


def _score_trajectory(
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
    fields = tuple(
        item for item in case.evidence_facts if item.startswith(("field:", "citation:", "source:"))
    )
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
        and set(observation.completion_criteria) == set(case.completion_criteria)
        and (
            profile == "deep_agents_live"
            or (observation.tokens is None and observation.estimated_cost is None)
        )
        and all(call.succeeded for call in calls)
    )
    return TrajectoryMetrics(
        task_success=success,
        task_accuracy=task_accuracy,
        route_accuracy=route_accuracy,
        required_field_coverage=_coverage(fields, observation.evidence_facts),
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


def score_trajectory(case, observation, profile):
    return _score_trajectory(case, observation, profile)


def _failed_observation(started, calls=()):
    return TrajectoryObservation(
        tasks=(),
        routes=tuple(
            dict.fromkeys(
                "official" if call.family == "registry" else "synthetic" for call in calls
            )
        ),
        specialists=(),
        tool_calls=tuple(calls),
        evidence_facts=(),
        completion_criteria=(),
        safe_stop="error",
        duration_ms=(perf_counter() - started) * 1000.0,
    )


def _redact_unsupported_facts(case, observation):
    """Persist only audited normalized facts; unknown worker text is hashed, never echoed."""
    return observation.model_copy(
        update={
            "evidence_facts": tuple(
                fact
                if fact in case.evidence_facts
                else "unsupported_fact:" + canonical_sha256(fact)
                for fact in observation.evidence_facts
            )
        }
    )


def _grading_error_result(case, profile, observation):
    """Independent conservative failure contract, with no call to either scorer."""
    calls = observation.tool_calls
    required_specialists = case.required_specialists if profile != "bounded_single_agent" else ()
    fields = [f for f in case.evidence_facts if f.startswith(("field:", "citation:", "source:"))]
    observation = observation.model_copy(
        update={
            "tasks": (),
            "specialists": (),
            "evidence_facts": (),
            "completion_criteria": (),
            "safe_stop": "error",
            "routes": tuple(
                dict.fromkeys(
                    "official" if call.family == "registry" else "synthetic" for call in calls
                )
            ),
        }
    )
    metrics = TrajectoryMetrics(
        task_success=False,
        task_accuracy=0.0,
        route_accuracy=0.0,
        required_field_coverage=0.0,
        evidence_fact_coverage=0.0,
        completion_criteria_coverage=0.0,
        delegation_accuracy=0.0,
        missing_specialist_count=len(required_specialists),
        duplicate_tool_calls=len(calls) - len({(c.name, c.input_sha256) for c in calls}),
        duplicate_work_count=0,
        tool_order_correct=False,
        prohibited_tool_call_count=sum(
            c.name not in READ_TOOLS
            or c.name in case.prohibited_tool_names
            or c.family != _family(c.name)
            for c in calls
        ),
        safe_stop_correct=False,
        budget_compliant=len(calls) <= case.max_tool_calls,
        tool_call_count=len(calls),
        required_task_count=len(case.expected_tasks),
        required_field_count=len(fields),
        required_fact_count=len(case.evidence_facts),
        required_completion_count=len(case.completion_criteria),
        required_specialist_count=len(required_specialists),
    )
    return OrchestrationCaseResult(
        case_id=case.id, observation=observation, metrics=metrics, grading_error=True
    )


def _validate_result(case, row, profile):
    if row.grading_error:
        if row != _grading_error_result(case, profile, row.observation):
            raise ValueError("invalid independent grading-error row")
    elif row.metrics != _score_trajectory(case, row.observation, profile):
        raise ValueError("trajectory metrics mismatch")


async def _run_profile(corpus, adapter):
    results = []
    for case in corpus.cases:
        started = perf_counter()
        calls = []
        token = _CASE_READS.set((calls, case.max_tool_calls))
        try:
            inputs = InvestigationInput.model_validate(
                case.model_dump(include=set(InvestigationInput.model_fields))
            )
            observation = await adapter.run(inputs, case.max_tool_calls)
            observation = TrajectoryObservation.model_validate(observation.model_dump(mode="json"))
            if observation.tool_calls != tuple(calls):
                raise ValueError("adapter trace differs from captured reads")
            observation = _redact_unsupported_facts(case, observation)
        except Exception:
            observation = _failed_observation(started, calls)
        finally:
            _CASE_READS.reset(token)
        try:
            row = OrchestrationCaseResult(
                case_id=case.id,
                observation=observation,
                metrics=score_trajectory(case, observation, adapter.name),
            )
        except Exception:
            row = _grading_error_result(case, adapter.name, observation)
        results.append(row)
    exposed = _exposed_tools()
    return ProfileResult(
        name=adapter.name,
        exposed_tool_names=exposed,
        results=tuple(results),
        metrics=_aggregate(results, exposed),
    )


class LiveUsage(FrozenContract):
    """Provider-runner supplied usage; absent values remain explicitly unavailable."""

    tokens: Count | None = None
    estimated_cost: Milliseconds | None = None


@dataclass(frozen=True, slots=True)
class LiveProgram:
    """An injected model coordinator operating only via the observable capture API."""

    invoke: Callable[[InvestigationInput, LiveCapture], Awaitable[LiveUsage]]
    exposed_tool_names: tuple[str, ...] = READ_TOOLS


@dataclass(frozen=True, slots=True)
class LiveRunnerFactory:
    """Explicit opt-in adapter for a model/provider; credentials stay with its caller.

    The factory gets no evidence authority. Its program is validated before the
    capture (and its sealed Deep Agents read capabilities) is supplied to invoke.
    """

    provider: str
    model: str
    factory: Callable[[], LiveProgram]
    repetitions: int = 1
    prompt: str = SUPERVISOR_PROMPT

    def __post_init__(self):
        if type(self.repetitions) is not int or not 1 <= self.repetitions <= 3:
            raise ValueError("live repetitions must be 1 through 3")
        if type(self.provider) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.provider):
            raise ValueError("invalid live provider")
        if (
            type(self.model) is not str
            or not self.model.strip()
            or type(self.prompt) is not str
            or not self.prompt.strip()
        ):
            raise ValueError("live model and prompt must be explicit")


@dataclass(slots=True)
class _LiveController:
    session: InvestigationSession
    facade: LiveCapture
    roles: tuple[str, ...]
    prompt: str


_LIVE_CONTROLLER: ContextVar[_LiveController | None] = ContextVar("live_controller", default=None)


def _live_controller(facade):
    controller = _LIVE_CONTROLLER.get()
    if controller is None or controller.facade is not facade:
        raise ValueError("capture is outside its active invocation")
    return controller


class _SealedCaptureMeta(type):
    def __setattr__(cls, name, value):
        raise TypeError("capture class is immutable")

    def __delattr__(cls, name):
        raise TypeError("capture class is immutable")


class LiveCapture(metaclass=_SealedCaptureMeta):
    """Stateless sealed facade; trusted execution state is held outside the object.

    This is a capability boundary, not a sandbox for arbitrary Python module access.
    Ordinary attribute access, including object.__getattribute__, reveals no controller.
    """

    __slots__ = ()

    def __getattribute__(self, name):
        if name not in {"execute", "required_roles", "prompt", "halted", "__class__"}:
            raise AttributeError("capture exposes only its role API")
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        raise AttributeError("capture is immutable")

    def __delattr__(self, name):
        raise AttributeError("capture is immutable")

    def __dir__(self):
        return ["execute", "halted", "prompt", "required_roles"]

    @property
    def required_roles(self):
        return _live_controller(self).roles

    @property
    def prompt(self):
        return _live_controller(self).prompt

    @property
    def halted(self):
        return _live_controller(self).session.halted

    async def execute(self, role: str) -> tuple[str, ...]:
        controller = _live_controller(self)
        session = controller.session
        index = len(session.specialists)
        if session.halted or index >= len(controller.roles) or role != controller.roles[index]:
            raise ValueError("live delegation is duplicated, out of order, or beyond scope")
        session.specialists.append(role)
        await _dispatch_specialist(session, role)
        return tuple(session.criteria)


async def _run_live_profile(corpus, model: Any) -> LiveProfileStatus:
    started = perf_counter()
    if type(model) is not LiveRunnerFactory:
        # Compatibility with the earlier model-string surface: inspect its sealed
        # factory without invoking an unadapted graph. Executable callers provide
        # LiveRunnerFactory so observations and provider usage have typed contracts.
        identifier = (
            model if type(model) is str else f"{type(model).__module__}.{type(model).__qualname__}"
        )
        provider = identifier.split(":", 1)[0] if ":" in identifier else "unspecified"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
            provider = "unspecified"
        code = "factory_error"
        try:
            supervisor = build_deep_supervisor(model=model)
            surface = set(supervisor.parent_tool_names) | set(supervisor.exposed_read_tool_names)
            surface.update(
                name for names in supervisor.subagent_tool_names.values() for name in names
            )
            if supervisor.operational_write_tool_names or surface & set(OPERATIONS_TOOLS):
                code = "prohibited_tool_exposure"
        except Exception:
            pass
        return LiveProfileStatus(
            status="error",
            error_code=code,
            provider=provider,
            model_sha256=canonical_sha256(identifier),
            prompt_sha256=canonical_sha256(SUPERVISOR_PROMPT),
            duration_ms=(perf_counter() - started) * 1000.0,
        )

    metadata = dict(
        provider=model.provider,
        model_sha256=canonical_sha256(model.model),
        prompt_sha256=canonical_sha256(model.prompt),
    )
    try:
        program = model.factory()
        if type(program) is not LiveProgram or not callable(program.invoke):
            raise ValueError("invalid live runner factory")
        if set(program.exposed_tool_names) != set(READ_TOOLS) or len(
            program.exposed_tool_names
        ) != len(READ_TOOLS):
            return LiveProfileStatus(
                status="error",
                error_code="prohibited_tool_exposure",
                duration_ms=(perf_counter() - started) * 1000.0,
                **metadata,
            )
    except Exception:
        return LiveProfileStatus(
            status="error",
            error_code="factory_error",
            duration_ms=(perf_counter() - started) * 1000.0,
            **metadata,
        )

    metadata["repetitions"] = model.repetitions
    results, usage_rows = [], []
    had_error = False
    for _ in range(model.repetitions):
        for case in corpus.cases:
            case_started, calls = perf_counter(), []
            token = _CASE_READS.set((calls, case.max_tool_calls))
            controller_token = None
            try:
                inputs = InvestigationInput.model_validate(
                    case.model_dump(include=set(InvestigationInput.model_fields))
                )
                capture = LiveCapture()
                count = {
                    "intake": 1,
                    "matching": 2,
                    "lineage": 3,
                    "reconciliation": 3,
                    "containment": 4,
                }[inputs.intent]
                controller = _LiveController(
                    InvestigationSession(inputs, case.max_tool_calls, sealed=True),
                    capture,
                    tuple(row[0] for row in _DISPATCH_CONTRACT[:count]),
                    model.prompt,
                )
                controller_token = _LIVE_CONTROLLER.set(controller)
                usage = await program.invoke(inputs, capture)
                usage = LiveUsage.model_validate(usage.model_dump(mode="json"))
                observation = controller.session.finish()
                observation = TrajectoryObservation.model_validate(
                    {
                        **observation.model_dump(mode="json"),
                        "tokens": usage.tokens,
                        "estimated_cost": usage.estimated_cost,
                    }
                )
                if observation.tool_calls != tuple(calls):
                    raise ValueError("live trace differs from observed sealed reads")
                observation = _redact_unsupported_facts(case, observation)
                usage_rows.append(usage)
            except Exception:
                had_error = True
                observation = _failed_observation(case_started, calls)
                usage_rows.append(LiveUsage())
            finally:
                if controller_token is not None:
                    _LIVE_CONTROLLER.reset(controller_token)
                _CASE_READS.reset(token)
            try:
                row = OrchestrationCaseResult(
                    case_id=case.id,
                    observation=observation,
                    metrics=_score_trajectory(case, observation, "deep_agents_live"),
                )
            except Exception:
                had_error = True
                row = _grading_error_result(case, "deep_agents_live", observation)
            results.append(row)
    tokens = (
        sum(row.tokens for row in usage_rows)
        if all(row.tokens is not None for row in usage_rows)
        else None
    )
    cost = (
        sum(row.estimated_cost for row in usage_rows)
        if all(row.estimated_cost is not None for row in usage_rows)
        else None
    )
    return LiveProfileStatus(
        status="error" if had_error else "completed",
        error_code="runner_error" if had_error else None,
        results=tuple(results),
        executed_case_count=len(results),
        tokens=tokens,
        estimated_cost=cost,
        tokens_available=tokens is not None,
        cost_available=cost is not None,
        duration_ms=(perf_counter() - started) * 1000.0,
        **metadata,
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
            _validate_result(case, row, profile.name)
        if profile.metrics != _aggregate(profile.results, profile.exposed_tool_names):
            raise ValueError("aggregate metrics mismatch")
    deltas, gates, passed = _comparison(report.profiles)
    if (report.deltas, report.metrics, report.gate_passed) != (deltas, gates, passed):
        raise ValueError("comparison gates or deltas mismatch")
    live = report.live_status
    if live.repetitions:
        expected_cases = corpus.cases * live.repetitions
        if tuple(row.case_id for row in live.results) != tuple(case.id for case in expected_cases):
            raise ValueError("live repetition/case matrix mismatch")
        for case, row in zip(expected_cases, live.results, strict=True):
            _validate_result(case, row, "deep_agents_live")
        observations = [row.observation for row in live.results]
        tokens = (
            sum(row.tokens for row in observations)
            if all(row.tokens is not None for row in observations)
            else None
        )
        cost = (
            sum(row.estimated_cost for row in observations)
            if all(row.estimated_cost is not None for row in observations)
            else None
        )
        if (live.tokens, live.estimated_cost) != (tokens, cost):
            raise ValueError("live usage totals mismatch")
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
        await _run_live_profile(corpus, live_model)
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
