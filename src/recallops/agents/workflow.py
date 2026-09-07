"""Explicit LangGraph orchestration for evidence-first recall operations."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import weakref
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Overwrite, interrupt
from pydantic import ValidationError

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
    AuditReceipt,
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
from recallops.services.operations import (
    ApprovalRequiredError,
    ClosureBlockedError,
    OperationsService,
    _WorkflowAuthorizationBroker,
)

OFFICIAL_PROVENANCE = "OFFICIAL_OPENFDA_SNAPSHOT"
SYNTHETIC_ORIGIN = "SYNTHETIC_RETAILER_DIGITAL_TWIN"


class _WorkflowInternals:
    __slots__ = ("graph", "locks")

    def __init__(self, graph: Any) -> None:
        self.graph = graph
        self.locks: dict[str, tuple[asyncio.Lock, int]] = {}


class ReadOnlyWorkflow:
    """An opaque checkpoint reader whose executor capability is stored out-of-object."""

    __slots__ = ("__weakref__",)

    async def aget_state(self, config: Mapping[str, Any]) -> Any:
        return await _workflow_internals(self).graph.aget_state(config)

    def aget_state_history(self, config: Mapping[str, Any], **kwargs: Any) -> Any:
        return _workflow_internals(self).graph.aget_state_history(config, **kwargs)

    def get_state(self, config: Mapping[str, Any]) -> Any:
        return _workflow_internals(self).graph.get_state(config)

    def get_state_history(self, config: Mapping[str, Any], **kwargs: Any) -> Any:
        return _workflow_internals(self).graph.get_state_history(config, **kwargs)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"compiled graph surface {name!r} is disabled and not exposed")


_WORKFLOW_INTERNALS: weakref.WeakKeyDictionary[ReadOnlyWorkflow, _WorkflowInternals] = (
    weakref.WeakKeyDictionary()
)


def _workflow_internals(workflow: ReadOnlyWorkflow) -> _WorkflowInternals:
    try:
        return _WORKFLOW_INTERNALS[workflow]
    except KeyError as error:  # pragma: no cover - only possible after invalid manual construction
        raise RuntimeError("workflow capability is unavailable") from error


def _execution_thread_id(config: Mapping[str, Any] | None) -> str:
    if type(config) is not dict or set(config) != {"configurable"}:
        raise ValueError("execution config must contain only configurable.thread_id")
    configurable = config.get("configurable")
    if type(configurable) is not dict or set(configurable) != {"thread_id"}:
        raise ValueError("execution configurable keys must contain only thread_id")
    thread_id = configurable.get("thread_id")
    if type(thread_id) is not str or not thread_id.strip():
        raise ValueError("configurable.thread_id must be a nonblank string")
    return thread_id


def _has_checkpoint(snapshot: Any) -> bool:
    config = snapshot.config or {}
    checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
    return type(checkpoint_id) is str and bool(checkpoint_id)


def _workflow_active_lock_count(workflow: ReadOnlyWorkflow) -> int:
    return len(_workflow_internals(workflow).locks)


async def _execute_workflow(
    workflow: ReadOnlyWorkflow,
    input: Any,
    config: Mapping[str, Any] | None = None,
) -> Any:
    """Execute only for trusted module callers with fixed durability settings."""
    internals = _workflow_internals(workflow)
    thread_id = _execution_thread_id(config)
    lock, users = internals.locks.get(thread_id, (asyncio.Lock(), 0))
    internals.locks[thread_id] = (lock, users + 1)
    try:
        async with lock:
            if isinstance(input, dict):
                if (
                    type(input.get("case_id")) is not str
                    or not input.get("case_id", "").strip()
                    or type(input.get("thread_id")) is not str
                    or input.get("thread_id") != thread_id
                ):
                    raise ValueError(
                        "initial case_id must be nonblank and thread_id must equal config thread_id"
                    )
                if _has_checkpoint(await internals.graph.aget_state(config)):
                    raise ValueError(f"thread {thread_id!r} already has a durable checkpoint")
            elif isinstance(input, Command):
                if (
                    input.resume is None
                    or input.update is not None
                    or input.graph is not None
                    or input.goto != ()
                ):
                    raise ValueError("compiled graph accepts resume-only Command values")
            else:
                raise TypeError("compiled graph input must be initial state or resume-only Command")
            return await internals.graph.ainvoke(
                input,
                config,
                version="v2",
                stream_mode="values",
                durability="sync",
            )
    finally:
        current_lock, current_users = internals.locks[thread_id]
        if current_lock is lock and current_users == 1:
            internals.locks.pop(thread_id)
        elif current_lock is lock:
            internals.locks[thread_id] = (lock, current_users - 1)


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


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        strict_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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
    if state.get("ambiguous_lot_ids"):
        return None
    disposition_lots = {
        receipt.get("details", {}).get("lot_id")
        for receipt in state.get("write_receipts", [])
        if receipt.get("action_type") == "record_disposition"
    }
    for lot_id in state["confirmed_lot_ids"]:
        planned = _planned_disposition(state, lot_id)
        if planned is None or lot_id in disposition_lots:
            continue
        disposition, evidence_id = planned
        return _action(
            state=state,
            action_type="record_disposition",
            targets=[lot_id],
            evidence_by_target={lot_id: [evidence_id]},
            rationale=(
                f"Record the reviewed {disposition} disposition for {lot_id} using "
                f"authoritative evidence {evidence_id}."
            ),
        )
    if state.get("evidence_gaps"):
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


def _planned_disposition(
    state: RecallOpsGraphState,
    lot_id: str,
) -> tuple[str, str] | None:
    reconciliation = next(
        (item for item in state.get("reconciliations", []) if item.get("lot_id") == lot_id),
        None,
    )
    if reconciliation is None:
        return None
    component_evidence = reconciliation.get("component_evidence", {})
    if reconciliation.get("unaccounted", 0) > 0:
        identifiers = component_evidence.get("unaccounted", [])
        if identifiers:
            return "dispose_unaccounted", identifiers[0]
    return None


def _remaining_actions(state: RecallOpsGraphState, current: ProposedAction) -> list[str]:
    receipts = state.get("write_receipts", [])
    completed = {receipt["action_type"] for receipt in receipts}
    current_type = current.action_type
    remaining: list[str] = []

    create_done = "create_case" in completed or current_type == "create_case"
    if not create_done:
        remaining.append("create_case")
    hold_done = "apply_inventory_hold" in completed or current_type == "apply_inventory_hold"
    if not hold_done:
        remaining.append("apply_inventory_hold")
    if state.get("ambiguous_lot_ids"):
        return remaining

    disposition_lots = {
        receipt.get("details", {}).get("lot_id")
        for receipt in receipts
        if receipt.get("action_type") == "record_disposition"
    }
    if current_type == "record_disposition":
        disposition_lots.add(current.target_ids[0])
    for lot_id in state.get("confirmed_lot_ids", []):
        if lot_id not in disposition_lots and _planned_disposition(state, lot_id) is not None:
            remaining.append("record_disposition")
    resolvable_lots = {
        lot_id
        for lot_id in state.get("confirmed_lot_ids", [])
        if _planned_disposition(state, lot_id) is not None
    }
    unresolved_gaps = [
        gap
        for gap in state.get("evidence_gaps", [])
        if not any(lot_id in gap for lot_id in resolvable_lots)
    ]
    if unresolved_gaps:
        return remaining

    tasks_done = "create_facility_tasks" in completed or current_type == "create_facility_tasks"
    if not tasks_done:
        remaining.append("create_facility_tasks")
    acknowledgements = dict(state.get("acknowledgements", {}))
    if current_type == "record_acknowledgment":
        acknowledgements[current.target_ids[0]] = True
    for facility_id in state.get("required_facilities", []):
        if not acknowledgements.get(facility_id, False):
            remaining.append("record_acknowledgment")

    if current_type != "close_case" and "close_case" not in completed:
        remaining.append("close_case")
    return remaining


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


def _validate_interrupt_response(response: Any, pending: Mapping[str, Any]) -> dict[str, Any]:
    normalized = strict_json_value(response)
    if not isinstance(normalized, dict):
        raise TypeError("interrupt response must be a JSON object")
    for key in ("kind", "case_id", "thread_id", "case_version"):
        expected = pending.get(key)
        if (
            key not in normalized
            or type(normalized[key]) is not type(expected)
            or normalized[key] != expected
        ):
            raise ValueError(f"resume {key} does not match the pending interrupt")
    if pending["kind"] in {"action_review", "closure_review"}:
        if (
            type(normalized.get("action_id")) is not str
            or normalized.get("action_id") != pending["action"]["action_id"]
        ):
            raise ValueError("resume action_id does not match the pending interrupt")
        if (
            type(normalized.get("action_digest")) is not str
            or normalized.get("action_digest") != pending["action_digest"]
        ):
            raise ValueError("resume action_digest does not match the pending interrupt")
    else:
        for key in ("action_id", "action_digest", "execution_id", "idempotency_key"):
            expected = pending.get(key)
            if (
                key not in normalized
                or type(normalized[key]) is not type(expected)
                or normalized[key] != expected
            ):
                raise ValueError(f"resume {key} does not match the pending interrupt")
    return normalized


async def _read(
    *,
    operation: Any,
    name: str,
    recorder: TraceRecorder,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    failures: FailureController | None = None,
) -> Any:
    trusted_operation = operation
    if failures is not None and failures.consume(f"{name}_transient_failure"):

        async def injected_transient(*_: Any, **__: Any) -> Any:
            raise TransientCallError(f"injected transient failure for {name}")

        trusted_operation = injected_transient
    wrapped = with_retry(
        trusted_operation,
        max_attempts=2,
        base_delay_seconds=0,
        budget=CallBudget(2),
        breaker=CircuitBreaker(2, 0),
        recorder=recorder,
        operation_name=name,
        operation_kind="read",
    )
    result = await wrapped(*args, **(kwargs or {}))
    if failures is not None and failures.consume(f"{name}_malformed_evidence"):
        return {"malformed": True, "tool": name}
    return result


def build_workflow(
    *,
    gateway: Gateway | None = None,
    retriever: AgenticRetriever | None = None,
    failures: FailureController | None = None,
    operations_service: OperationsService | None = None,
    authorization_broker: _WorkflowAuthorizationBroker | None = None,
    checkpointer: BaseCheckpointSaver,
) -> Any:
    """Compile the explicit coordinator with trusted dependencies in node closures."""
    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TypeError("checkpointer must be a real BaseCheckpointSaver")
    async_methods = {
        "aget_tuple": inspect.iscoroutinefunction,
        "alist": inspect.isasyncgenfunction,
        "aput": inspect.iscoroutinefunction,
        "aput_writes": inspect.iscoroutinefunction,
        "adelete_thread": inspect.iscoroutinefunction,
    }
    unsupported = [
        name
        for name, predicate in async_methods.items()
        if getattr(type(checkpointer), name, None) is getattr(BaseCheckpointSaver, name)
        or not predicate(getattr(type(checkpointer), name, None))
    ]
    if unsupported:
        raise TypeError(
            "checkpointer must be async-compatible; missing concrete async methods: "
            f"{', '.join(unsupported)}"
        )
    trusted_gateway = gateway or DirectGateway()
    trusted_operations = operations_service
    if trusted_operations is None and isinstance(trusted_gateway, DirectGateway):
        trusted_operations = trusted_gateway.operations
    if not isinstance(trusted_operations, OperationsService):
        raise TypeError("operations_service must be an authoritative OperationsService")
    if authorization_broker is not None and not isinstance(
        authorization_broker, _WorkflowAuthorizationBroker
    ):
        raise TypeError("authorization_broker must be the runtime-only workflow broker")
    trusted_retriever = retriever or AgenticRetriever(ClosedRetrievalGateway.direct())
    failure_controller = failures or FailureController()

    def read_failure(
        node_name: str,
        error: Exception,
        recorder: TraceRecorder | None = None,
    ) -> dict[str, Any]:
        return _node(
            node_name,
            status="escalated",
            failure_state={"stage": node_name, "error": str(error)},
            warnings=[f"Required evidence failed validation in {node_name}; stopped fail-closed."],
            tool_trace=recorder.to_dicts() if recorder is not None else [],
        )

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
            action_queue=[],
            acknowledgements={},
            watchdog={"signature": None, "repeat_count": 0, "max_repeats": 2},
            retry_state={"read_attempts": 0, "write_attempts": 0},
            failure_state={},
        )

    async def retrieve_context(state: RecallOpsGraphState) -> dict[str, Any]:
        try:
            if failure_controller.consume("rag_failure"):
                raise TransientCallError("injected agentic RAG failure")
            loop_state = trusted_retriever.start(
                state["question"],
                authoritative_facts={"recall_number": state["recall_number"]},
            )
            result = await trusted_retriever.resume(loop_state)
            while isinstance(result, RetrievalInterruption):
                loop_state = result.state
                result = await trusted_retriever.resume(loop_state)
            if not isinstance(result, AgenticRetrievalResult):
                raise TypeError("agentic RAG returned an invalid result")
        except Exception as error:
            return _node(
                "retrieve_context",
                status="escalated",
                failure_state={"stage": "retrieve_context", "error": str(error)},
                warnings=["Agentic RAG failed; investigation stopped before operational reads."],
                rag_state={
                    "budgets": trusted_retriever.budgets.model_dump(mode="json"),
                    "failed": True,
                },
            )
        watchdog = {
            "signature": f"{result.stop_reason}:{result.read_count}:{len(result.evidence)}",
            "repeat_count": result.model_dump(mode="json").get("progress_repeat_count", 0),
            "max_repeats": 2,
        }
        durable_rag_state = {
            "budgets": trusted_retriever.budgets.model_dump(mode="json"),
            "hop_count": result.hop_count,
            "query_count": result.query_count,
            "read_count": result.read_count,
            "rewrite_used": result.rewrite_used,
            "stop_reason": result.stop_reason,
            "progress_signature": watchdog["signature"],
            "progress_repeat_count": watchdog["repeat_count"],
        }
        if failure_controller.consume("repeated_progress_signature"):
            return _node(
                "retrieve_context",
                rag_result=result.model_dump(mode="json"),
                rag_state=durable_rag_state,
                watchdog={**watchdog, "repeat_count": 2},
                status="escalated",
                warnings=["Progress watchdog escalated a repeated reasoning signature."],
            )
        return _node(
            "retrieve_context",
            rag_result=result.model_dump(mode="json"),
            rag_state=durable_rag_state,
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
            if failure_controller.consume("unavailable_evidence"):
                raw = None
            else:
                operation = trusted_gateway.get_recall
                if failure_controller.consume("registry_transient_failure"):

                    async def unavailable_registry(_: str) -> Any:
                        raise TransientCallError("injected registry outage")

                    operation = unavailable_registry
                raw = await _read(
                    operation=operation,
                    name="get_recall",
                    recorder=recorder,
                    args=(state["recall_number"],),
                    failures=failure_controller,
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
            retry_state=(
                {**state["retry_state"], "read_attempts": 2} if warnings else state["retry_state"]
            ),
        )

    def route_after_regulatory(state: RecallOpsGraphState) -> str:
        return "end" if state.get("status") == "escalated" else "continue"

    async def product_lot_match(state: RecallOpsGraphState) -> dict[str, Any]:
        recorder = TraceRecorder(case_id=state["case_id"], thread_id=state["thread_id"])
        try:
            predicate = RecallPredicate.model_validate(state["recall_predicate"])
            products = await _read(
                operation=trusted_gateway.find_candidate_products,
                name="find_candidate_products",
                recorder=recorder,
                args=(predicate,),
                failures=failure_controller,
            )
            lots = await _read(
                operation=trusted_gateway.match_lots,
                name="match_lots",
                recorder=recorder,
                args=(predicate,),
                failures=failure_controller,
            )
            scope = set(state.get("scope_lot_ids", []))
            if scope:
                lots = [lot for lot in lots if lot["lot_id"] in scope]
                missing = scope - {lot["lot_id"] for lot in lots}
                if missing:
                    raise ValueError(f"scope lots unavailable: {sorted(missing)}")
            assessment = assess_product_lots(
                predicate=predicate,
                candidate_products=products,
                candidate_lots=lots,
            )
        except Exception as error:
            return read_failure("product_lot_match", error, recorder)
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
        try:
            for lot_id in relevant:
                lot_forward = await _read(
                    operation=trusted_gateway.trace_forward,
                    name="trace_forward",
                    recorder=recorder,
                    args=(lot_id,),
                    failures=failure_controller,
                )
                lot_backward = await _read(
                    operation=trusted_gateway.trace_backward,
                    name="trace_backward",
                    recorder=recorder,
                    args=(lot_id,),
                    failures=failure_controller,
                )
                lot_inventory = await _read(
                    operation=trusted_gateway.get_inventory,
                    name="get_inventory",
                    recorder=recorder,
                    args=(lot_id,),
                    failures=failure_controller,
                )
                reconciliation = await _read(
                    operation=trusted_gateway.reconcile_units,
                    name="reconcile_units",
                    recorder=recorder,
                    args=(lot_id,),
                    failures=failure_controller,
                )
                events.extend(lot_forward)
                inventory.extend(lot_inventory)
                reconciliations.append(reconciliation)
                forward[lot_id] = [item["event_id"] for item in lot_forward]
                backward[lot_id] = [item["event_id"] for item in lot_backward]
        except Exception as error:
            return read_failure("trace_forward_backward", error, recorder)
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
        try:
            relevant = [*state["confirmed_lot_ids"], *state["ambiguous_lot_ids"]]
            assessment = assess_traceability(
                lot_ids=relevant,
                events=state["trace_events"],
                inventory_positions=state["inventory_positions"],
                reconciliations=state["reconciliations"],
            )
        except Exception as error:
            return read_failure("reconcile", error)
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
        if failure_controller.consume("independent_verifier_failure"):
            violations.append("independent verifier rejected the proposed evidence packet")
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
        if state.get("verification", {}).get("passed") is not True:
            return _node(
                "prepare_action_review",
                status="escalated",
                action_queue=[],
                closure_outcome={
                    "eligible": False,
                    "reason": "independent verification did not pass",
                },
            )
        action = _next_action(state)
        if action is None:
            return _node(
                "prepare_action_review",
                status="open_closure_blocked",
                action_queue=[],
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
            action_queue=[action.model_dump(mode="json")],
            action_digest=packet["action_digest"],
            review_packet=packet,
            remaining_action_types=packet["remaining_action_types"],
            node_trace=["prepare_action_review", "action_review"],
        )

    async def action_review(state: RecallOpsGraphState) -> dict[str, Any]:
        response = _validate_interrupt_response(
            interrupt(state["review_packet"]), state["review_packet"]
        )
        return _handle_review_response(state, response, node_name="action_review")

    async def closure_review(state: RecallOpsGraphState) -> dict[str, Any]:
        response = _validate_interrupt_response(
            interrupt(state["review_packet"]), state["review_packet"]
        )
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
            current = ProposedAction.model_validate(state["current_action"])
            rejection_reason: str | None = None
            try:
                edited = ProposedAction.model_validate(response.get("edited_action"))
            except (TypeError, ValidationError, ValueError) as error:
                edited = current
                rejection_reason = f"edited action is invalid: {error}"
            immutable_fields = (
                "action_id",
                "action_type",
                "case_id",
                "target_ids",
                "evidence_ids",
                "evidence_by_target",
                "expected_case_version",
            )
            if rejection_reason is None:
                changed = [
                    field
                    for field in immutable_fields
                    if getattr(edited, field) != getattr(current, field)
                ]
                if changed:
                    rejection_reason = (
                        "edit may change rationale only; immutable reviewed fields changed: "
                        f"{', '.join(changed)}"
                    )
            required = _next_action(state)
            if rejection_reason is None and (
                required is None
                or any(
                    getattr(edited, field) != getattr(required, field) for field in immutable_fields
                )
            ):
                rejection_reason = "edited action does not match the required version transition"
            if rejection_reason is not None:
                history["accepted"] = False
                history["rejection_reason"] = rejection_reason
                return _node(
                    node_name,
                    status="review_required",
                    current_action=current.model_dump(mode="json"),
                    action_queue=[current.model_dump(mode="json")],
                    action_digest=state["action_digest"],
                    review_packet=state["review_packet"],
                    warnings=[rejection_reason],
                    review_history=[history],
                )
            return _node(
                node_name,
                status="investigating",
                current_action=edited.model_dump(mode="json"),
                action_queue=[edited.model_dump(mode="json")],
                action_digest=proposed_action_digest(edited),
                review_history=[history],
            )
        if decision == "reject":
            return _node(node_name, status="open", action_queue=[], review_history=[history])
        if decision == "escalate":
            return _node(node_name, status="escalated", action_queue=[], review_history=[history])
        raise ValueError("review decision must be approve, edit, reject, or escalate")

    def route_review(state: RecallOpsGraphState) -> str:
        if state["status"] == "approved_pending_execution":
            return "approve"
        if state["status"] == "investigating":
            return "edit"
        if state["status"] == "review_required":
            return "re_review"
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
            status=(
                "closure_review_required"
                if action.action_type == "close_case"
                else "review_required"
            ),
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
        response = _validate_interrupt_response(
            interrupt(state["execution_request"]), state["execution_request"]
        )
        if response.get("decision") == "confirm":
            return _node("execution_confirmation", status="approved_pending_execution")
        if response.get("decision") == "cancel":
            return _node("execution_confirmation", status="open", action_queue=[])
        raise ValueError("execution decision must be confirm or cancel")

    def route_execution(state: RecallOpsGraphState) -> str:
        return "execute" if state["status"] == "approved_pending_execution" else "end"

    async def execute_one_operation(state: RecallOpsGraphState) -> dict[str, Any]:
        action = ProposedAction.model_validate(state["current_action"])
        approval = ApprovalDecision.model_validate(state["approval"])
        if state.get("verification", {}).get("passed") is not True:
            raise ValueError("action execution requires an independent passing verification")
        ApprovalGuard().validate(
            approval,
            proposed_action=action,
            expected_case_version=state["case_version"],
        )
        # One invocation receives exactly one write attempt; unknown outcomes are
        # checkpointed and require a new human-triggered invocation with the same key.
        CallBudget(1).consume()
        if authorization_broker is None:
            raise ApprovalRequiredError(
                "operation execution requires the private runtime authorization broker"
            )
        kwargs: dict[str, Any] = {
            "case_id": state["case_id"],
            "proposed_action": action,
            "approval": approval,
            "expected_case_version": state["case_version"],
            "idempotency_key": state["idempotency_key"],
        }
        try:
            if action.action_type == "create_case":
                confirmed = set(state["confirmed_lot_ids"])
                trace_events = [
                    event for event in state["trace_events"] if event["lot_id"] in confirmed
                ]
                reconciliations = [
                    item for item in state["reconciliations"] if item["lot_id"] in confirmed
                ]
                details = {
                    "recall_number": state["recall_number"],
                    "question": state["question"],
                    "thread_id": state["thread_id"],
                    "confirmed_lot_ids": state["confirmed_lot_ids"],
                    "trace_event_ids": [event["event_id"] for event in trace_events],
                    "required_facilities": state["required_facilities"],
                    "reconciliation": reconciliations,
                    "evidence_gaps": [
                        f"{item['lot_id']}: {item['unaccounted']} unaccounted units"
                        for item in reconciliations
                        if item["unaccounted"]
                    ],
                }
                grant = authorization_broker.issue_workflow_execution_grant(
                    **kwargs,
                    thread_id=state["thread_id"],
                    execution_id=state["execution_id"],
                    execution_request_digest=_canonical_digest(state["execution_request"]),
                    details=details,
                    target_ids=list(action.target_ids),
                    evidence_ids=details["trace_event_ids"],
                )
                receipt = await trusted_gateway.create_case(
                    **kwargs,
                    execution_grant=grant,
                    **details,
                )
            elif action.action_type == "apply_inventory_hold":
                details = {"lot_ids": list(action.target_ids)}
                grant = authorization_broker.issue_workflow_execution_grant(
                    **kwargs,
                    thread_id=state["thread_id"],
                    execution_id=state["execution_id"],
                    execution_request_digest=_canonical_digest(state["execution_request"]),
                    details=details,
                    target_ids=list(action.target_ids),
                )
                receipt = await trusted_gateway.apply_inventory_hold(
                    **kwargs, execution_grant=grant, **details
                )
            elif action.action_type == "create_facility_tasks":
                details = {"facility_ids": list(action.target_ids)}
                grant = authorization_broker.issue_workflow_execution_grant(
                    **kwargs,
                    thread_id=state["thread_id"],
                    execution_id=state["execution_id"],
                    execution_request_digest=_canonical_digest(state["execution_request"]),
                    details=details,
                    target_ids=list(action.target_ids),
                )
                receipt = await trusted_gateway.create_facility_tasks(
                    **kwargs, execution_grant=grant, **details
                )
            elif action.action_type == "record_acknowledgment":
                details = {"facility_id": action.target_ids[0]}
                grant = authorization_broker.issue_workflow_execution_grant(
                    **kwargs,
                    thread_id=state["thread_id"],
                    execution_id=state["execution_id"],
                    execution_request_digest=_canonical_digest(state["execution_request"]),
                    details=details,
                    target_ids=list(action.target_ids),
                )
                receipt = await trusted_gateway.record_acknowledgment(
                    **kwargs, execution_grant=grant, **details
                )
            elif action.action_type == "record_disposition":
                planned = _planned_disposition(state, action.target_ids[0])
                if planned is None:
                    raise ValueError("record_disposition lacks authoritative disposition evidence")
                disposition, evidence_id = planned
                details = {
                    "lot_id": action.target_ids[0],
                    "disposition": disposition,
                    "evidence_id": evidence_id,
                }
                grant = authorization_broker.issue_workflow_execution_grant(
                    **kwargs,
                    thread_id=state["thread_id"],
                    execution_id=state["execution_id"],
                    execution_request_digest=_canonical_digest(state["execution_request"]),
                    details=details,
                    target_ids=list(action.target_ids),
                    evidence_ids=[evidence_id],
                )
                receipt = await trusted_gateway.record_disposition(
                    **kwargs,
                    execution_grant=grant,
                    **details,
                )
            elif action.action_type == "close_case":
                grant = authorization_broker.issue_workflow_execution_grant(
                    **kwargs,
                    thread_id=state["thread_id"],
                    execution_id=state["execution_id"],
                    execution_request_digest=_canonical_digest(state["execution_request"]),
                    details={},
                    target_ids=[],
                )
                receipt = await trusted_gateway.close_case(**kwargs, execution_grant=grant)
            else:
                raise ValueError(f"unsupported runtime action {action.action_type}")
            typed_receipt = AuditReceipt.model_validate(receipt)
            _validate_receipt(state, action, approval, typed_receipt)
        except ClosureBlockedError as error:
            return _node(
                "execute_one_operation",
                status="open_closure_blocked",
                action_queue=[],
                closure_outcome={"eligible": False, "reason": str(error)},
                failure_state={"stage": "close_case", "error": str(error)},
            )
        except Exception as error:
            if action.action_type == "close_case" and "closure blocked:" in str(error).lower():
                return _node(
                    "execute_one_operation",
                    status="open_closure_blocked",
                    action_queue=[],
                    closure_outcome={"eligible": False, "reason": str(error)},
                    failure_state={"stage": "close_case", "error": str(error)},
                )
            return _unknown_write_outcome(state, scenario="receipt_or_write_failure", error=error)
        if failure_controller.consume("lost_write_response"):
            return _unknown_write_outcome(
                state,
                scenario="lost_write_response",
                error=RuntimeError("write response intentionally treated as lost"),
            )
        return _after_receipt(state, typed_receipt)

    def _unknown_write_outcome(
        state: RecallOpsGraphState,
        *,
        scenario: str,
        error: Exception,
    ) -> dict[str, Any]:
        return _node(
            "execute_one_operation",
            status="write_outcome_unknown",
            failure_state={
                "stage": "execute_one_operation",
                "scenario": scenario,
                "error": str(error),
                "idempotency_key": state["idempotency_key"],
            },
            retry_state={
                **state["retry_state"],
                "write_attempts": state["retry_state"].get("write_attempts", 0) + 1,
            },
        )

    def _validate_receipt(
        state: RecallOpsGraphState,
        action: ProposedAction,
        approval: ApprovalDecision,
        receipt: AuditReceipt,
    ) -> None:
        exact_fields = {
            "case_id": state["case_id"],
            "action_type": action.action_type,
            "idempotency_key": state["idempotency_key"],
            "case_version": state["case_version"] + 1,
            "status": "simulated",
            "actor": approval.actor,
            "justification": approval.justification,
        }
        receipt_json = receipt.model_dump(mode="json")
        for field_name, expected in exact_fields.items():
            actual = receipt_json.get(field_name)
            if type(actual) is not type(expected) or actual != expected:
                raise ValueError(f"receipt {field_name} does not match the approved write")
        if receipt_json.get("details", {}).get("reviewed_action") != action.model_dump(mode="json"):
            raise ValueError("receipt reviewed_action does not match the approved action")

        authoritative_receipt = trusted_operations.get_receipt(state["idempotency_key"])
        authoritative_case = trusted_operations.get_case(state["case_id"])
        if authoritative_receipt is None or authoritative_case is None:
            raise ValueError("receipt is absent from the authoritative Operations store")
        if authoritative_receipt.model_dump(mode="json") != receipt_json:
            raise ValueError("gateway receipt does not match the authoritative Operations receipt")
        if (
            type(authoritative_case.case_version) is not int
            or authoritative_case.case_version != state["case_version"] + 1
            or authoritative_case.case_id != state["case_id"]
            or authoritative_case.thread_id != state["thread_id"]
            or not authoritative_case.write_receipts
            or authoritative_case.write_receipts[-1].model_dump(mode="json") != receipt_json
        ):
            raise ValueError("receipt does not match authoritative Operations case state")

    def _after_receipt(state: RecallOpsGraphState, raw_receipt: Any) -> dict[str, Any]:
        receipt = _json(raw_receipt)
        acknowledgements = dict(state.get("acknowledgements", {}))
        authoritative_updates: dict[str, Any] = {}
        if receipt["action_type"] == "create_facility_tasks":
            acknowledgements.update({facility: False for facility in state["required_facilities"]})
        if receipt["action_type"] == "record_acknowledgment":
            action = ProposedAction.model_validate(state["current_action"])
            acknowledgements[action.target_ids[0]] = True
        if receipt["action_type"] == "record_disposition":
            authoritative_case = trusted_operations.get_case(state["case_id"])
            if authoritative_case is None:
                raise RuntimeError("authoritative case disappeared after disposition receipt")
            authoritative_updates = {
                "reconciliations": [
                    item.model_dump(mode="json") for item in authoritative_case.reconciliation
                ],
                "evidence_gaps": list(authoritative_case.evidence_gaps),
            }
        provisional: RecallOpsGraphState = {
            **state,
            **authoritative_updates,
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
                action_queue=[],
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
                action_queue=[],
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
            **(
                {
                    "reconciliations": authoritative_updates["reconciliations"],
                    "evidence_gaps": Overwrite(authoritative_updates["evidence_gaps"]),
                }
                if authoritative_updates
                else {}
            ),
            current_action=next_action.model_dump(mode="json"),
            action_queue=[next_action.model_dump(mode="json")],
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
        response = _validate_interrupt_response(
            interrupt(state["execution_request"]), state["execution_request"]
        )
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
    graph.add_conditional_edges(
        "trace_forward_backward",
        route_after_regulatory,
        {"continue": "reconcile", "end": END},
    )
    graph.add_conditional_edges(
        "reconcile",
        route_after_regulatory,
        {"continue": "containment_draft", "end": END},
    )
    graph.add_edge("containment_draft", "verify")
    graph.add_conditional_edges(
        "verify",
        lambda state: "review" if state.get("verification", {}).get("passed") is True else "end",
        {"review": "prepare_action_review", "end": END},
    )
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
            "re_review": "action_review",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "closure_review",
        route_review,
        {
            "approve": "prepare_execution_confirmation",
            "edit": "verify_edited_action",
            "re_review": "closure_review",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "verify_edited_action",
        lambda state: (
            "end"
            if state["status"] == "escalated"
            else "closure"
            if state["status"] == "closure_review_required"
            else "review"
        ),
        {"review": "action_review", "closure": "closure_review", "end": END},
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
    workflow = ReadOnlyWorkflow()
    _WORKFLOW_INTERNALS[workflow] = _WorkflowInternals(
        graph.compile(checkpointer=checkpointer, name="recallops-runtime")
    )
    return workflow
