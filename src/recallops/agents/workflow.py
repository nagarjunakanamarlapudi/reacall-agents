"""Explicit LangGraph orchestration for evidence-first recall operations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from recallops.agents.middleware import CallBudget, CircuitBreaker, TransientCallError, with_retry
from recallops.agents.planner import plan_investigation
from recallops.agents.policies import ApprovalGuard, strict_json_value
from recallops.agents.specialists import (
    ProductLotAssessment,
    TraceabilityAssessment,
    assess_product_lots,
    assess_traceability,
    draft_containment,
    investigate_recall,
)
from recallops.agents.state import RecallOpsGraphState
from recallops.agents.telemetry import TraceRecorder
from recallops.data.loaders import load_recall_snapshot
from recallops.mcp.gateway import DirectGateway, Gateway
from recallops.models import (
    ApprovalBinding,
    ApprovalDecision,
    ProposedAction,
    RecallPredicate,
    RecallRecord,
    proposed_action_digest,
)
from recallops.retrieval.agentic import (
    AgenticRetrievalResult,
    AgenticRetriever,
    ClosedRetrievalGateway,
    RetrievalInterruption,
)

OFFICIAL_PROVENANCE = "OFFICIAL_OPENFDA_SNAPSHOT"
SYNTHETIC_ORIGIN = "SYNTHETIC_RETAILER_DIGITAL_TWIN"


class FailureController:
    """Bounded, explicit demo failure injection shared by runtime node closures."""

    def __init__(self) -> None:
        self._remaining: Counter[str] = Counter()

    def inject(self, scenario: str, times: int) -> None:
        if not scenario.strip():
            raise ValueError("failure scenario must be nonblank")
        if isinstance(times, bool) or not isinstance(times, int) or times <= 0:
            raise ValueError("failure times must be a positive integer")
        self._remaining[scenario] += times

    def consume(self, scenario: str) -> bool:
        if self._remaining[scenario] <= 0:
            return False
        self._remaining[scenario] -= 1
        return True


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return strict_json_value(value)


def _node(name: str, **updates: Any) -> dict[str, Any]:
    return {"node_trace": [name], **updates}


def _ordered(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _event_evidence_by_lot(state: RecallOpsGraphState) -> dict[str, list[str]]:
    confirmed = set(state["confirmed_lot_ids"])
    result: dict[str, list[str]] = {lot_id: [] for lot_id in state["confirmed_lot_ids"]}
    for event in state["trace_events"]:
        lot_id = event["lot_id"]
        if lot_id in confirmed:
            result[lot_id].append(event["event_id"])
    return {lot_id: _ordered(values) for lot_id, values in result.items()}


def _union(evidence: Mapping[str, list[str]]) -> list[str]:
    return _ordered([item for values in evidence.values() for item in values])


def _action(
    *,
    state: RecallOpsGraphState,
    action_type: str,
    targets: list[str],
    evidence_by_target: Mapping[str, list[str]],
    rationale: str,
) -> ProposedAction:
    version = state["case_version"]
    return ProposedAction(
        action_id=f"{state['case_id']}-{action_type}-v{version}",
        action_type=action_type,
        case_id=state["case_id"],
        target_ids=tuple(targets),
        rationale=rationale,
        evidence_ids=tuple(_union(evidence_by_target)),
        evidence_by_target={key: tuple(value) for key, value in evidence_by_target.items()},
        expected_case_version=version,
    )


def _next_action(state: RecallOpsGraphState) -> ProposedAction | None:
    completed = [receipt["action_type"] for receipt in state.get("write_receipts", [])]
    if "create_case" not in completed:
        evidence = _event_evidence_by_lot(state)
        return _action(
            state=state,
            action_type="create_case",
            targets=state["confirmed_lot_ids"],
            evidence_by_target=evidence,
            rationale=(
                "Create the simulated case from authoritative lot, trace, facility, and "
                "reconciliation evidence."
            ),
        )
    if "apply_inventory_hold" not in completed:
        evidence = {
            lot_id: state["evidence_by_lot"][lot_id] for lot_id in state["confirmed_lot_ids"]
        }
        return _action(
            state=state,
            action_type="apply_inventory_hold",
            targets=state["confirmed_lot_ids"],
            evidence_by_target=evidence,
            rationale="Hold only exact or probable lots after separate human confirmation.",
        )
    if state.get("evidence_gaps") or state.get("ambiguous_lot_ids"):
        return None
    if "create_facility_tasks" not in completed:
        evidence = {
            facility: state["evidence_by_facility"][facility]
            for facility in state["required_facilities"]
        }
        return _action(
            state=state,
            action_type="create_facility_tasks",
            targets=state["required_facilities"],
            evidence_by_target=evidence,
            rationale="Create verification tasks at every authoritatively traced facility.",
        )
    acknowledged = state.get("acknowledgements", {})
    for facility in state["required_facilities"]:
        if not acknowledged.get(facility, False):
            evidence = {facility: state["evidence_by_facility"][facility]}
            return _action(
                state=state,
                action_type="record_acknowledgment",
                targets=[facility],
                evidence_by_target=evidence,
                rationale=f"Record the simulated verification acknowledgment from {facility}.",
            )
    return _action(
        state=state,
        action_type="close_case",
        targets=[],
        evidence_by_target={},
        rationale="Close only after Operations revalidates every authoritative closure gate.",
    )


def _remaining_actions(state: RecallOpsGraphState, current: ProposedAction) -> list[str]:
    ordered = [
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "close_case",
    ]
    return ordered[ordered.index(current.action_type) + 1 :]


def _review_packet(state: RecallOpsGraphState, action: ProposedAction) -> dict[str, Any]:
    digest = proposed_action_digest(action)
    return {
        "kind": "closure_review" if action.action_type == "close_case" else "action_review",
        "case_id": state["case_id"],
        "thread_id": state["thread_id"],
        "case_version": state["case_version"],
        "action": action.model_dump(mode="json"),
        "action_digest": digest,
        "official_evidence": state["official_evidence"],
        "synthetic_evidence": state["synthetic_evidence"],
        "rag_citations": state.get("rag_result", {}).get("citations", []),
        "rag_gaps": state.get("rag_result", {}).get("evidence_gaps", []),
        "evidence_gaps": state.get("evidence_gaps", []),
        "verification": state["verification"],
        "remaining_action_types": _remaining_actions(state, action),
    }


async def _read(
    *,
    operation: Any,
    name: str,
    recorder: TraceRecorder,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> Any:
    wrapped = with_retry(
        operation,
        max_attempts=2,
        base_delay_seconds=0,
        budget=CallBudget(2),
        breaker=CircuitBreaker(2, 0),
        recorder=recorder,
        operation_name=name,
        operation_kind="read",
    )
    return await wrapped(*args, **(kwargs or {}))


def build_workflow(
    *,
    gateway: Gateway | None = None,
    retriever: AgenticRetriever | None = None,
    failures: FailureController | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Compile the explicit coordinator with trusted dependencies in node closures."""
    trusted_gateway = gateway or DirectGateway()
    trusted_retriever = retriever or AgenticRetriever(ClosedRetrievalGateway.direct())
    failure_controller = failures or FailureController()

    async def intake(state: RecallOpsGraphState) -> dict[str, Any]:
        return _node(
            "intake",
            status="investigating",
            case_version=0,
            source_mode="snapshot",
            specialist_outputs={},
            evidence_gaps=[],
            warnings=[],
            review_history=[],
            write_receipts=[],
            acknowledgements={},
            watchdog={"signature": None, "repeat_count": 0, "max_repeats": 2},
            retry_state={"read_attempts": 0, "write_attempts": 0},
            failure_state={},
        )

    async def retrieve_context(state: RecallOpsGraphState) -> dict[str, Any]:
        loop_state = trusted_retriever.start(
            state["question"], authoritative_facts={"recall_number": state["recall_number"]}
        )
        result = await trusted_retriever.resume(loop_state)
        while isinstance(result, RetrievalInterruption):
            loop_state = result.state
            result = await trusted_retriever.resume(loop_state)
        assert isinstance(result, AgenticRetrievalResult)
        watchdog = {
            "signature": f"{result.stop_reason}:{result.read_count}:{len(result.evidence)}",
            "repeat_count": result.model_dump(mode="json").get("progress_repeat_count", 0),
            "max_repeats": 2,
        }
        if failure_controller.consume("repeated_progress_signature"):
            return _node(
                "retrieve_context",
                rag_result=result.model_dump(mode="json"),
                rag_state=loop_state.model_dump(mode="json"),
                watchdog={**watchdog, "repeat_count": 2},
                status="escalated",
                warnings=["Progress watchdog escalated a repeated reasoning signature."],
            )
        return _node(
            "retrieve_context",
            rag_result=result.model_dump(mode="json"),
            rag_state=loop_state.model_dump(mode="json"),
            watchdog=watchdog,
        )

    def route_after_retrieval(state: RecallOpsGraphState) -> str:
        return "end" if state.get("status") == "escalated" else "continue"

    async def plan(state: RecallOpsGraphState) -> dict[str, Any]:
        value = plan_investigation(case_id=state["case_id"], question=state["question"])
        updates: dict[str, Any] = {"plan": value.model_dump(mode="json")}
        if failure_controller.consume("model_failure"):
            updates.update(
                warnings=["Model unavailable; deterministic four-specialist plan used."],
                model_trace=[
                    {
                        "operation": "deep_supervisor_reasoning",
                        "status": "fallback",
                        "fallback": "deterministic_fixed_specialists",
                    }
                ],
            )
        return _node("plan", **updates)

    async def regulatory_intake(state: RecallOpsGraphState) -> dict[str, Any]:
        recorder = TraceRecorder(case_id=state["case_id"], thread_id=state["thread_id"])
        warnings: list[str] = []
        try:
            if failure_controller.consume("registry_transient_failure"):
                raise TransientCallError("injected registry outage")
            raw = await _read(
                operation=trusted_gateway.get_recall,
                name="get_recall",
                recorder=recorder,
                args=(state["recall_number"],),
            )
            if raw is None:
                raise ValueError("recall evidence is unavailable")
            if failure_controller.consume("malformed_evidence"):
                raw = {**raw, "provenance": "UNTRUSTED"}
            recall = RecallRecord.model_validate(raw)
        except TransientCallError:
            recall = load_recall_snapshot()
            warnings.append(
                "Registry transient failure; used pinned OFFICIAL_OPENFDA_SNAPSHOT fallback."
            )
        except (TypeError, ValueError) as error:
            return _node(
                "regulatory_intake",
                status="escalated",
                failure_state={"stage": "regulatory_intake", "error": str(error)},
                warnings=["Authoritative recall evidence was unavailable or malformed."],
                tool_trace=recorder.to_dicts(),
            )
        intelligence = investigate_recall(recall)
        return _node(
            "regulatory_intake",
            recall=recall.model_dump(mode="json"),
            recall_predicate=intelligence.predicate.model_dump(mode="json"),
            official_evidence={
                "provenance": recall.provenance,
                "recall_number": recall.recall_number,
                "citations": intelligence.citations,
                "source_url": recall.source_url,
                "sha256": recall.sha256,
            },
            specialist_outputs={"recall-intelligence": intelligence.model_dump(mode="json")},
            warnings=warnings,
            tool_trace=recorder.to_dicts(),
        )

    def route_after_regulatory(state: RecallOpsGraphState) -> str:
        return "end" if state.get("status") == "escalated" else "continue"

    async def product_lot_match(state: RecallOpsGraphState) -> dict[str, Any]:
        recorder = TraceRecorder(case_id=state["case_id"], thread_id=state["thread_id"])
        predicate = RecallPredicate.model_validate(state["recall_predicate"])
        products = await _read(
            operation=trusted_gateway.find_candidate_products,
            name="find_candidate_products",
            recorder=recorder,
            args=(predicate,),
        )
        lots = await _read(
            operation=trusted_gateway.match_lots,
            name="match_lots",
            recorder=recorder,
            args=(predicate,),
        )
        scope = set(state.get("scope_lot_ids", []))
        if scope:
            lots = [lot for lot in lots if lot["lot_id"] in scope]
            missing = scope - {lot["lot_id"] for lot in lots}
            if missing:
                return _node(
                    "product_lot_match",
                    status="escalated",
                    failure_state={
                        "stage": "product_lot_match",
                        "error": f"scope lots unavailable: {sorted(missing)}",
                    },
                    warnings=["Requested lot scope is unavailable in authoritative data."],
                    tool_trace=recorder.to_dicts(),
                )
        assessment = assess_product_lots(
            predicate=predicate,
            candidate_products=products,
            candidate_lots=lots,
        )
        if not assessment.confirmed_lot_ids:
            return _node(
                "product_lot_match",
                status="escalated",
                failure_state={"stage": "product_lot_match", "error": "no confirmed lots"},
                warnings=["No exact or probable lots are eligible for containment."],
                tool_trace=recorder.to_dicts(),
            )
        return _node(
            "product_lot_match",
            candidate_products=products,
            candidate_lots=lots,
            match_decisions=[item.model_dump(mode="json") for item in assessment.decisions],
            confirmed_lot_ids=assessment.confirmed_lot_ids,
            ambiguous_lot_ids=assessment.ambiguous_lot_ids,
            specialist_outputs={"product-lot-matching": assessment.model_dump(mode="json")},
            tool_trace=recorder.to_dicts(),
        )

    async def trace_forward_backward(state: RecallOpsGraphState) -> dict[str, Any]:
        recorder = TraceRecorder(case_id=state["case_id"], thread_id=state["thread_id"])
        relevant = [*state["confirmed_lot_ids"], *state["ambiguous_lot_ids"]]
        events: list[dict[str, Any]] = []
        inventory: list[dict[str, Any]] = []
        reconciliations: list[dict[str, Any]] = []
        forward: dict[str, list[str]] = {}
        backward: dict[str, list[str]] = {}
        for lot_id in relevant:
            lot_forward = await _read(
                operation=trusted_gateway.trace_forward,
                name="trace_forward",
                recorder=recorder,
                args=(lot_id,),
            )
            lot_backward = await _read(
                operation=trusted_gateway.trace_backward,
                name="trace_backward",
                recorder=recorder,
                args=(lot_id,),
            )
            lot_inventory = await _read(
                operation=trusted_gateway.get_inventory,
                name="get_inventory",
                recorder=recorder,
                args=(lot_id,),
            )
            reconciliation = await _read(
                operation=trusted_gateway.reconcile_units,
                name="reconcile_units",
                recorder=recorder,
                args=(lot_id,),
            )
            events.extend(lot_forward)
            inventory.extend(lot_inventory)
            reconciliations.append(reconciliation)
            forward[lot_id] = [item["event_id"] for item in lot_forward]
            backward[lot_id] = [item["event_id"] for item in lot_backward]
        return _node(
            "trace_forward_backward",
            trace_events=events,
            inventory_positions=inventory,
            reconciliations=reconciliations,
            forward_traces=forward,
            backward_traces=backward,
            tool_trace=recorder.to_dicts(),
        )

    async def reconcile(state: RecallOpsGraphState) -> dict[str, Any]:
        relevant = [*state["confirmed_lot_ids"], *state["ambiguous_lot_ids"]]
        assessment = assess_traceability(
            lot_ids=relevant,
            events=state["trace_events"],
            inventory_positions=state["inventory_positions"],
            reconciliations=state["reconciliations"],
        )
        evidence_by_lot = {
            coverage.lot_id: _ordered(
                [
                    *coverage.event_ids,
                    *coverage.inventory_evidence_ids,
                    *coverage.reconciliation_evidence_ids,
                ]
            )
            for coverage in assessment.coverage
        }
        evidence_by_facility: dict[str, list[str]] = {}
        for coverage in assessment.coverage:
            if coverage.lot_id not in state["confirmed_lot_ids"]:
                continue
            for facility, identifiers in coverage.facility_evidence.items():
                evidence_by_facility.setdefault(facility, []).extend(identifiers)
                evidence_by_facility[facility].extend(coverage.reconciliation_evidence_ids)
        evidence_by_facility = {
            key: _ordered(value) for key, value in sorted(evidence_by_facility.items())
        }
        required = sorted(evidence_by_facility)
        return _node(
            "reconcile",
            required_facilities=required,
            evidence_by_lot=evidence_by_lot,
            evidence_by_facility=evidence_by_facility,
            evidence_gaps=assessment.evidence_gaps,
            specialist_outputs={"traceability-reconciliation": assessment.model_dump(mode="json")},
        )

    async def containment_draft(state: RecallOpsGraphState) -> dict[str, Any]:
        matching = ProductLotAssessment.model_validate(
            state["specialist_outputs"]["product-lot-matching"]
        )
        traceability = TraceabilityAssessment.model_validate(
            state["specialist_outputs"]["traceability-reconciliation"]
        )
        proposal = draft_containment(
            case_id=state["case_id"],
            expected_case_version=state["case_version"],
            matching=matching,
            traceability=traceability,
        )
        return _node(
            "containment_draft",
            specialist_outputs={"containment-communications": proposal.model_dump(mode="json")},
            synthetic_evidence={
                "origin": SYNTHETIC_ORIGIN,
                "lot_ids": [*state["confirmed_lot_ids"], *state["ambiguous_lot_ids"]],
                "facility_ids": state["required_facilities"],
                "evidence_ids": _ordered(
                    [item for values in state["evidence_by_lot"].values() for item in values]
                ),
            },
        )

    async def verify(state: RecallOpsGraphState) -> dict[str, Any]:
        violations: list[str] = []
        if set(state["confirmed_lot_ids"]) & set(state["ambiguous_lot_ids"]):
            violations.append("ambiguous lots overlap the confirmed containment scope")
        if set(state["required_facilities"]) != set(state["evidence_by_facility"]):
            violations.append("facility scope lacks exact evidence coverage")
        if violations:
            return _node(
                "verify",
                status="escalated",
                verification={"passed": False, "violations": violations},
            )
        return _node(
            "verify",
            verification={
                "passed": True,
                "verifier": "independent-deterministic-policy-verifier",
                "authoritative_controls": [
                    "predicate matching",
                    "trace reconciliation",
                    "operations approval and closure gates",
                ],
                "rag_role": "advisory_context_only",
                "violations": [],
            },
        )

    async def prepare_action_review(state: RecallOpsGraphState) -> dict[str, Any]:
        action = _next_action(state)
        if action is None:
            return _node(
                "prepare_action_review",
                status="open_closure_blocked",
                closure_outcome={
                    "eligible": False,
                    "reason": "unresolved ambiguity or reconciliation evidence gap",
                },
            )
        packet = _review_packet(state, action)
        return _node(
            "prepare_action_review",
            status="review_required",
            current_action=action.model_dump(mode="json"),
            action_digest=packet["action_digest"],
            review_packet=packet,
            remaining_action_types=packet["remaining_action_types"],
            node_trace=["prepare_action_review", "action_review"],
        )

    async def action_review(state: RecallOpsGraphState) -> dict[str, Any]:
        response = interrupt(state["review_packet"])
        return _handle_review_response(state, response, node_name="action_review")

    async def closure_review(state: RecallOpsGraphState) -> dict[str, Any]:
        response = interrupt(state["review_packet"])
        return _handle_review_response(state, response, node_name="closure_review")

    def _handle_review_response(
        state: RecallOpsGraphState, response: Any, *, node_name: str
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise TypeError("review response must be an object")
        decision = response.get("decision")
        history = {**response, "recorded_at": datetime.now(UTC).isoformat()}
        if decision == "approve":
            action = ProposedAction.model_validate(state["current_action"])
            digest = proposed_action_digest(action)
            approval = ApprovalDecision(
                decision="approve",
                actor=response["actor"],
                justification=response["justification"],
                approved_at=datetime.now(UTC),
                approved_case_version=state["case_version"],
                approved_case_id=state["case_id"],
                action_ids=(action.action_id,),
                action_bindings=(
                    ApprovalBinding(action_id=action.action_id, action_digest=digest),
                ),
            )
            return _node(
                node_name,
                status="approved_pending_execution",
                approval=approval.model_dump(mode="json"),
                review_history=[history],
            )
        if decision == "edit":
            edited = ProposedAction.model_validate(response["edited_action"])
            if (
                edited.case_id != state["case_id"]
                or edited.expected_case_version != state["case_version"]
            ):
                raise ValueError("edited action must retain the pending case and version")
            return _node(
                node_name,
                status="investigating",
                current_action=edited.model_dump(mode="json"),
                action_digest=proposed_action_digest(edited),
                review_history=[history],
            )
        if decision == "reject":
            return _node(node_name, status="open", review_history=[history])
        if decision == "escalate":
            return _node(node_name, status="escalated", review_history=[history])
        raise ValueError("review decision must be approve, edit, reject, or escalate")

    def route_review(state: RecallOpsGraphState) -> str:
        if state["status"] == "approved_pending_execution":
            return "approve"
        if state["status"] == "investigating":
            return "edit"
        return "end"

    async def verify_edited_action(state: RecallOpsGraphState) -> dict[str, Any]:
        action = ProposedAction.model_validate(state["current_action"])
        violations: list[str] = []
        if action.action_type == "apply_inventory_hold" and set(action.target_ids) & set(
            state["ambiguous_lot_ids"]
        ):
            violations.append("ambiguous lots cannot be included in an inventory hold")
        if violations:
            return _node(
                "verify_edited_action",
                status="escalated",
                verification={"passed": False, "violations": violations},
            )
        packet = _review_packet(state, action)
        return _node(
            "verify_edited_action",
            status="review_required",
            verification={
                **state["verification"],
                "passed": True,
                "edited_action_reverified": True,
                "violations": [],
            },
            action_digest=packet["action_digest"],
            review_packet=packet,
            node_trace=["verify_edited_action", "action_review"],
        )

    async def prepare_execution_confirmation(state: RecallOpsGraphState) -> dict[str, Any]:
        action = ProposedAction.model_validate(state["current_action"])
        digest = proposed_action_digest(action)
        execution_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{state['thread_id']}:{state['case_version']}:{action.action_id}:{digest}",
            )
        )
        idempotency_key = f"recallops:{execution_id}"
        request = {
            "kind": "execution_confirmation",
            "case_id": state["case_id"],
            "thread_id": state["thread_id"],
            "case_version": state["case_version"],
            "action_id": action.action_id,
            "action_digest": digest,
            "execution_id": execution_id,
            "idempotency_key": idempotency_key,
            "control_label": "Simulate approved actions",
            "action": action.model_dump(mode="json"),
        }
        return _node(
            "prepare_execution_confirmation",
            execution_id=execution_id,
            idempotency_key=idempotency_key,
            execution_request=request,
            node_trace=["prepare_execution_confirmation", "execution_confirmation"],
        )

    async def execution_confirmation(state: RecallOpsGraphState) -> dict[str, Any]:
        response = interrupt(state["execution_request"])
        if not isinstance(response, dict):
            raise TypeError("execution response must be an object")
        if response.get("decision") == "confirm":
            return _node("execution_confirmation", status="approved_pending_execution")
        if response.get("decision") == "cancel":
            return _node("execution_confirmation", status="open")
        raise ValueError("execution decision must be confirm or cancel")

    def route_execution(state: RecallOpsGraphState) -> str:
        return "execute" if state["status"] == "approved_pending_execution" else "end"

    async def execute_one_operation(state: RecallOpsGraphState) -> dict[str, Any]:
        action = ProposedAction.model_validate(state["current_action"])
        approval = ApprovalDecision.model_validate(state["approval"])
        ApprovalGuard().validate(
            approval,
            proposed_action=action,
            expected_case_version=state["case_version"],
        )
        kwargs = {
            "case_id": state["case_id"],
            "proposed_action": action,
            "approval": approval,
            "expected_case_version": state["case_version"],
            "idempotency_key": state["idempotency_key"],
        }
        if action.action_type == "create_case":
            confirmed = set(state["confirmed_lot_ids"])
            trace_events = [
                event for event in state["trace_events"] if event["lot_id"] in confirmed
            ]
            reconciliations = [
                item for item in state["reconciliations"] if item["lot_id"] in confirmed
            ]
            receipt = await trusted_gateway.create_case(
                **kwargs,
                recall_number=state["recall_number"],
                confirmed_lot_ids=state["confirmed_lot_ids"],
                trace_event_ids=[event["event_id"] for event in trace_events],
                required_facilities=state["required_facilities"],
                reconciliation=reconciliations,
                evidence_gaps=_ordered([*state["evidence_gaps"], *state["ambiguous_lot_ids"]]),
                question=state["question"],
            )
        elif action.action_type == "apply_inventory_hold":
            receipt = await trusted_gateway.apply_inventory_hold(
                **kwargs, lot_ids=list(action.target_ids)
            )
        elif action.action_type == "create_facility_tasks":
            receipt = await trusted_gateway.create_facility_tasks(
                **kwargs, facility_ids=list(action.target_ids)
            )
        elif action.action_type == "record_acknowledgment":
            receipt = await trusted_gateway.record_acknowledgment(
                **kwargs, facility_id=action.target_ids[0]
            )
        elif action.action_type == "close_case":
            receipt = await trusted_gateway.close_case(**kwargs)
        else:
            raise ValueError(f"unsupported runtime action {action.action_type}")
        if failure_controller.consume("lost_write_response"):
            return _node(
                "execute_one_operation",
                status="write_outcome_unknown",
                failure_state={
                    "stage": "execute_one_operation",
                    "scenario": "lost_write_response",
                    "idempotency_key": state["idempotency_key"],
                },
                retry_state={
                    **state["retry_state"],
                    "write_attempts": state["retry_state"].get("write_attempts", 0) + 1,
                },
            )
        return _after_receipt(state, receipt)

    def _after_receipt(state: RecallOpsGraphState, raw_receipt: Any) -> dict[str, Any]:
        receipt = _json(raw_receipt)
        acknowledgements = dict(state.get("acknowledgements", {}))
        if receipt["action_type"] == "create_facility_tasks":
            acknowledgements.update({facility: False for facility in state["required_facilities"]})
        if receipt["action_type"] == "record_acknowledgment":
            action = ProposedAction.model_validate(state["current_action"])
            acknowledgements[action.target_ids[0]] = True
        provisional: RecallOpsGraphState = {
            **state,
            "case_version": receipt["case_version"],
            "write_receipts": [*state.get("write_receipts", []), receipt],
            "acknowledgements": acknowledgements,
            "approval": {},
            "execution_request": {},
        }
        if receipt["action_type"] == "close_case":
            return _node(
                "execute_one_operation",
                case_version=receipt["case_version"],
                write_receipts=[receipt],
                acknowledgements=acknowledgements,
                approval={},
                execution_request={},
                status="closed",
                closure_outcome={"eligible": True, "closed": True},
            )
        next_action = _next_action(provisional)
        if next_action is None:
            return _node(
                "execute_one_operation",
                case_version=receipt["case_version"],
                write_receipts=[receipt],
                acknowledgements=acknowledgements,
                approval={},
                execution_request={},
                status="open_closure_blocked",
                closure_outcome={
                    "eligible": False,
                    "reason": "unresolved ambiguity or reconciliation evidence gap",
                },
            )
        packet = _review_packet(provisional, next_action)
        return _node(
            "execute_one_operation",
            case_version=receipt["case_version"],
            write_receipts=[receipt],
            acknowledgements=acknowledgements,
            approval={},
            execution_request={},
            current_action=next_action.model_dump(mode="json"),
            action_digest=packet["action_digest"],
            review_packet=packet,
            remaining_action_types=packet["remaining_action_types"],
            status=(
                "closure_review_required"
                if next_action.action_type == "close_case"
                else "review_required"
            ),
        )

    def route_after_write(state: RecallOpsGraphState) -> str:
        if state["status"] == "write_outcome_unknown":
            return "unknown"
        if state["status"] == "review_required":
            return "review"
        if state["status"] == "closure_review_required":
            return "closure"
        return "end"

    async def prepare_next_action_review(state: RecallOpsGraphState) -> dict[str, Any]:
        return _node(
            "prepare_action_review",
            node_trace=["prepare_action_review", "action_review"],
        )

    async def prepare_closure_review(state: RecallOpsGraphState) -> dict[str, Any]:
        return _node(
            "prepare_closure_review",
            node_trace=["prepare_closure_review", "closure_review"],
        )

    async def prepare_write_recovery(state: RecallOpsGraphState) -> dict[str, Any]:
        packet = {
            "kind": "write_outcome_recovery",
            "case_id": state["case_id"],
            "thread_id": state["thread_id"],
            "case_version": state["case_version"],
            "action_id": state["current_action"]["action_id"],
            "action_digest": state["action_digest"],
            "execution_id": state["execution_id"],
            "idempotency_key": state["idempotency_key"],
            "message": "The write response was lost. Retry only with the exact same key.",
        }
        return _node(
            "prepare_write_recovery",
            execution_request=packet,
            node_trace=["prepare_write_recovery", "write_recovery"],
        )

    async def write_recovery(state: RecallOpsGraphState) -> dict[str, Any]:
        response = interrupt(state["execution_request"])
        if not isinstance(response, dict):
            raise TypeError("write recovery response must be an object")
        if response.get("decision") == "retry":
            return _node("write_recovery", status="approved_pending_execution")
        return _node("write_recovery", status="escalated")

    def route_recovery(state: RecallOpsGraphState) -> str:
        return "retry" if state["status"] == "approved_pending_execution" else "end"

    graph = StateGraph(RecallOpsGraphState)
    graph.add_node("intake", intake)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("plan", plan)
    graph.add_node("regulatory_intake", regulatory_intake)
    graph.add_node("product_lot_match", product_lot_match)
    graph.add_node("trace_forward_backward", trace_forward_backward)
    graph.add_node("reconcile", reconcile)
    graph.add_node("containment_draft", containment_draft)
    graph.add_node("verify", verify)
    graph.add_node("prepare_action_review", prepare_action_review)
    graph.add_node("action_review", action_review)
    graph.add_node("closure_review", closure_review)
    graph.add_node("verify_edited_action", verify_edited_action)
    graph.add_node("prepare_execution_confirmation", prepare_execution_confirmation)
    graph.add_node("execution_confirmation", execution_confirmation)
    graph.add_node("execute_one_operation", execute_one_operation)
    graph.add_node("prepare_next_action_review", prepare_next_action_review)
    graph.add_node("prepare_closure_review", prepare_closure_review)
    graph.add_node("prepare_write_recovery", prepare_write_recovery)
    graph.add_node("write_recovery", write_recovery)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "retrieve_context")
    graph.add_conditional_edges(
        "retrieve_context", route_after_retrieval, {"continue": "plan", "end": END}
    )
    graph.add_edge("plan", "regulatory_intake")
    graph.add_conditional_edges(
        "regulatory_intake",
        route_after_regulatory,
        {"continue": "product_lot_match", "end": END},
    )
    graph.add_conditional_edges(
        "product_lot_match",
        route_after_regulatory,
        {"continue": "trace_forward_backward", "end": END},
    )
    graph.add_edge("trace_forward_backward", "reconcile")
    graph.add_edge("reconcile", "containment_draft")
    graph.add_edge("containment_draft", "verify")
    graph.add_edge("verify", "prepare_action_review")
    graph.add_conditional_edges(
        "prepare_action_review",
        lambda state: "end" if state["status"] == "open_closure_blocked" else "review",
        {"review": "action_review", "end": END},
    )
    graph.add_conditional_edges(
        "action_review",
        route_review,
        {
            "approve": "prepare_execution_confirmation",
            "edit": "verify_edited_action",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "closure_review",
        route_review,
        {
            "approve": "prepare_execution_confirmation",
            "edit": "verify_edited_action",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "verify_edited_action",
        lambda state: "end" if state["status"] == "escalated" else "review",
        {"review": "action_review", "end": END},
    )
    graph.add_edge("prepare_execution_confirmation", "execution_confirmation")
    graph.add_conditional_edges(
        "execution_confirmation",
        route_execution,
        {"execute": "execute_one_operation", "end": END},
    )
    graph.add_conditional_edges(
        "execute_one_operation",
        route_after_write,
        {
            "unknown": "prepare_write_recovery",
            "review": "prepare_next_action_review",
            "closure": "prepare_closure_review",
            "end": END,
        },
    )
    graph.add_edge("prepare_next_action_review", "action_review")
    graph.add_edge("prepare_closure_review", "closure_review")
    graph.add_edge("prepare_write_recovery", "write_recovery")
    graph.add_conditional_edges(
        "write_recovery",
        route_recovery,
        {"retry": "execute_one_operation", "end": END},
    )
    return graph.compile(checkpointer=checkpointer, name="recallops-runtime")
