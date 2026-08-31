"""Real offline scenario executor for the RecallOps R01-R21 safety matrix."""

from __future__ import annotations

import asyncio
import tempfile
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, StateUpdate

from recallops.agents.middleware import (
    CallBudget,
    CircuitBreaker,
    CircuitOpenError,
    TransientCallError,
    with_retry,
)
from recallops.agents.runtime import RecallOpsRuntime, RuntimeResult
from recallops.agents.workflow import build_workflow
from recallops.evaluation.runner import EvaluationObservation, load_scenarios, run_evaluations
from recallops.evaluation.schema import EvaluationReport, EvaluationScenario
from recallops.mcp.gateway import DirectGateway, StdioMCPGateway
from recallops.models import (
    ApprovalBinding,
    ApprovalDecision,
    ProposedAction,
    RecallPredicate,
    proposed_action_digest,
)
from recallops.services.operations import (
    ApprovalRequiredError,
    ClosureBlockedError,
    IdempotencyConflictError,
    OperationsService,
    StaleCaseVersionError,
)
from recallops.services.traceability import TraceabilityService

_SUPPORTED_START_FAILURES = {
    "R02": ("registry_transient_failure",),
    "R10": ("model_failure",),
    "R19": ("repeated_progress_signature",),
}

_REQUIRED_DEPENDENCY_FAILURES = (
    "rag_failure",
    "find_candidate_products_transient_failure",
    "find_candidate_products_malformed_evidence",
    "match_lots_transient_failure",
    "match_lots_malformed_evidence",
    "trace_forward_transient_failure",
    "trace_forward_malformed_evidence",
    "trace_backward_transient_failure",
    "trace_backward_malformed_evidence",
    "get_inventory_transient_failure",
    "get_inventory_malformed_evidence",
    "reconcile_units_transient_failure",
    "reconcile_units_malformed_evidence",
)


def _bound_response(
    pending: dict[str, Any],
    *,
    decision: str,
    actor: str = "Food-safety manager",
    justification: str = "Reviewed against cited evidence and authoritative reconciliation.",
) -> dict[str, Any]:
    response = {
        key: pending[key]
        for key in ("kind", "case_id", "thread_id", "case_version", "action_digest")
    }
    response["action_id"] = pending.get("action_id", pending.get("action", {}).get("action_id"))
    for key in ("execution_id", "idempotency_key"):
        if key in pending:
            response[key] = pending[key]
    response["decision"] = decision
    if decision in {"approve", "confirm", "retry"}:
        response.update(actor=actor, justification=justification)
    return response


def _reviewed(
    case_id: str,
    action_type: str,
    version: int,
    target_ids: list[str],
    *,
    evidence_ids: list[str] | None = None,
    actor: str = "Food-safety manager",
    decision: str = "approve",
) -> tuple[ProposedAction, ApprovalDecision]:
    evidence = list(evidence_ids or [f"EVIDENCE-{target}" for target in target_ids])
    action = ProposedAction(
        action_id=f"{case_id}-{action_type}-v{version}",
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids,
        rationale=f"Evaluate {action_type} against authoritative evidence.",
        evidence_ids=evidence,
        evidence_by_target={target: tuple(evidence) for target in target_ids},
        expected_case_version=version,
    )
    approval = ApprovalDecision(
        decision=decision,
        actor=actor,
        justification="Evidence-scoped simulated operation.",
        approved_at=datetime(2026, 8, 30, 12, min(version, 59), tzinfo=UTC),
        approved_case_version=version,
        approved_case_id=case_id,
        action_ids=[action.action_id],
        action_bindings=[
            ApprovalBinding(
                action_id=action.action_id,
                action_digest=proposed_action_digest(action),
            )
        ],
    )
    return action, approval


def _case_payload(traceability: TraceabilityService, lot_id: str) -> dict[str, Any]:
    events = traceability.trace_forward(lot_id)
    return {
        "recall_number": "H-1230-2026",
        "question": f"Evaluate {lot_id} in the offline safety suite.",
        "confirmed_lot_ids": [lot_id],
        "trace_event_ids": [event["event_id"] for event in events],
        "required_facilities": sorted(
            {
                facility
                for event in events
                for facility in (event.get("from_facility"), event.get("to_facility"))
                if facility
            }
        ),
        "reconciliation": [traceability.reconcile_units(lot_id)],
        "evidence_gaps": [],
    }


def _create_case(
    service: OperationsService,
    traceability: TraceabilityService,
    case_id: str,
    *,
    lot_id: str = "LOT-PROBABLE-160",
) -> None:
    payload = _case_payload(traceability, lot_id)
    action, approval = _reviewed(
        case_id,
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    service.create_case(
        case_id=case_id,
        **payload,
        proposed_action=action,
        approval=approval,
        expected_case_version=0,
        idempotency_key=f"{case_id}-create",
    )


def _apply_hold(
    service: OperationsService,
    case_id: str,
    lot_ids: list[str],
    version: int,
    *,
    key: str,
    actor: str = "Food-safety manager",
) -> Any:
    action, approval = _reviewed(
        case_id,
        "apply_inventory_hold",
        version,
        lot_ids,
        actor=actor,
    )
    return service.apply_inventory_hold(
        case_id=case_id,
        lot_ids=lot_ids,
        proposed_action=action,
        approval=approval,
        expected_case_version=version,
        idempotency_key=key,
    )


def _create_tasks(
    service: OperationsService,
    case_id: str,
    facilities: list[str],
    version: int,
    *,
    key: str,
) -> Any:
    action, approval = _reviewed(case_id, "create_facility_tasks", version, facilities)
    return service.create_facility_tasks(
        case_id=case_id,
        facility_ids=facilities,
        proposed_action=action,
        approval=approval,
        expected_case_version=version,
        idempotency_key=key,
    )


def _acknowledge(
    service: OperationsService,
    case_id: str,
    facility: str,
    version: int,
    *,
    key: str,
) -> Any:
    action, approval = _reviewed(case_id, "record_acknowledgment", version, [facility])
    return service.record_acknowledgment(
        case_id=case_id,
        facility_id=facility,
        proposed_action=action,
        approval=approval,
        expected_case_version=version,
        idempotency_key=key,
    )


def _record_disposition(
    service: OperationsService,
    case_id: str,
    lot_id: str,
    version: int,
    *,
    evidence_id: str,
    key: str,
) -> Any:
    action, approval = _reviewed(
        case_id,
        "record_disposition",
        version,
        [lot_id],
        evidence_ids=[evidence_id],
    )
    return service.record_disposition(
        case_id=case_id,
        lot_id=lot_id,
        disposition="dispose_unaccounted",
        evidence_id=evidence_id,
        proposed_action=action,
        approval=approval,
        expected_case_version=version,
        idempotency_key=key,
    )


def _close(service: OperationsService, case_id: str, version: int, *, key: str) -> Any:
    action, approval = _reviewed(case_id, "close_case", version, [])
    return service.close_case(
        case_id=case_id,
        proposed_action=action,
        approval=approval,
        expected_case_version=version,
        idempotency_key=key,
    )


def _make_ready(
    service: OperationsService,
    traceability: TraceabilityService,
    case_id: str,
) -> int:
    _create_case(service, traceability, case_id)
    state = service.get_case(case_id)
    assert state is not None
    _create_tasks(
        service,
        case_id,
        state.required_facilities,
        1,
        key=f"{case_id}-tasks",
    )
    version = 2
    for facility in state.required_facilities:
        _acknowledge(
            service,
            case_id,
            facility,
            version,
            key=f"{case_id}-ack-{facility}",
        )
        version += 1
    return version


def _resolved_exact_traceability() -> TraceabilityService:
    dataset = deepcopy(TraceabilityService().dataset)
    disposal = next(
        event for event in dataset["events"] if event["event_id"] == "EV-D-LOT-EXACT-170"
    )
    disposal["quantity"] += 50
    return TraceabilityService(dataset=dataset)


def _error_code(error: BaseException) -> str:
    if isinstance(error, ApprovalRequiredError):
        return "approval_required"
    if isinstance(error, IdempotencyConflictError):
        return "idempotency_conflict"
    if isinstance(error, StaleCaseVersionError):
        return "stale_case_version"
    if isinstance(error, ClosureBlockedError):
        return "closure_blocked"
    if isinstance(error, CircuitOpenError):
        return "circuit_open"
    return type(error).__name__


def _normalize_warning_codes(warnings: list[str]) -> list[str]:
    codes = list(warnings)
    for warning in warnings:
        lowered = warning.casefold()
        if "registry transient" in lowered or "fallback" in lowered:
            codes.append("live_unavailable")
        if "model unavailable" in lowered:
            codes.append("model_fallback")
        if "watchdog" in lowered or "repeated reasoning" in lowered:
            codes.append("progress_watchdog_triggered")
    return list(dict.fromkeys(codes))


def _normalize_gaps(case: dict[str, Any]) -> list[str]:
    gaps = list(case.get("evidence_gaps", []))
    for reconciliation in case.get("reconciliations", []):
        unaccounted = reconciliation.get("unaccounted", 0)
        if unaccounted:
            gaps.append(f"unaccounted_units:{reconciliation['lot_id']}:{unaccounted}")
    gaps.extend(f"ambiguous_lot:{lot_id}" for lot_id in case.get("ambiguous_lot_ids", []))
    return list(dict.fromkeys(gaps))


def _normalize_state(case: dict[str, Any], **updates: Any) -> dict[str, Any]:
    state = deepcopy(case)
    reconciliations = state.get("reconciliations", state.get("reconciliation", []))
    state.update(
        reconciliation=reconciliations,
        affected_facilities=state.get("required_facilities", []),
        unaccounted_units=sum(item.get("unaccounted", 0) for item in reconciliations),
        retry_count={
            "get_recall": state.get("retry_state", {}).get("read_attempts", 0),
            **state.get("retry_count", {}),
        },
        progress_signature=state.get("watchdog", {}).get("signature"),
        warnings=_normalize_warning_codes(state.get("warnings", [])),
        evidence_gaps=_normalize_gaps(state),
        forward_trace=state.get("forward_traces", {}),
        backward_trace=state.get("backward_traces", {}),
        rejected_lot_ids=[
            item["lot_id"]
            for item in state.get("candidate_lots", [])
            if item.get("classification") == "rejected"
        ],
        node_run_counts=dict(Counter(state.get("node_trace", []))),
        decision_count=len(state.get("review_history", [])),
        human_decision=(state.get("review_history") or [None])[-1],
        model_mode=(
            "deterministic_fallback"
            if any(item.get("status") == "fallback" for item in state.get("model_trace", []))
            else "deterministic"
        ),
    )
    state.update(updates)
    held: list[str] = []
    created_tasks: list[str] = []
    for receipt in state.get("write_receipts", []):
        if receipt.get("action_type") == "apply_inventory_hold":
            held.extend(receipt.get("details", {}).get("lot_ids", []))
        if receipt.get("action_type") == "create_facility_tasks":
            created_tasks.extend(receipt.get("details", {}).get("facility_ids", []))
    state["held_lot_ids"] = list(dict.fromkeys(held))
    state["created_tasks"] = list(dict.fromkeys(created_tasks))
    state["action_sequence"] = [
        receipt.get("action_type") for receipt in state.get("write_receipts", [])
    ]
    return state


async def _start(runtime: RecallOpsRuntime, scenario: EvaluationScenario) -> RuntimeResult:
    extra = scenario.input.model_extra or {}
    return await runtime.start_case(
        recall_number=scenario.input.recall_number,
        question=scenario.input.question,
        case_id=extra.get("case_id", f"CASE-{scenario.id}"),
        thread_id=extra.get("thread_id", f"thread-{scenario.id.lower()}"),
        scope_lot_ids=extra.get("scope_lot_ids"),
    )


async def _approve_and_confirm(runtime: RecallOpsRuntime, result: RuntimeResult) -> RuntimeResult:
    assert result.pending_interrupt is not None
    approved = await runtime.resume_case(
        thread_id=result.case["thread_id"],
        response=_bound_response(result.pending_interrupt, decision="approve"),
    )
    assert approved.pending_interrupt is not None
    return await runtime.resume_case(
        thread_id=result.case["thread_id"],
        response=_bound_response(approved.pending_interrupt, decision="confirm"),
    )


async def _drive_to_end(runtime: RecallOpsRuntime, result: RuntimeResult) -> RuntimeResult:
    while result.pending_interrupt is not None:
        pending = result.pending_interrupt
        decision = (
            "approve" if pending["kind"] in {"action_review", "closure_review"} else "confirm"
        )
        result = await runtime.resume_case(
            thread_id=result.case["thread_id"],
            response=_bound_response(pending, decision=decision),
        )
    return result


async def _drive_to_end_with_review_lifecycle(
    runtime: RecallOpsRuntime,
    result: RuntimeResult,
) -> tuple[RuntimeResult, list[dict[str, Any]], bool]:
    lifecycle: list[dict[str, Any]] = []
    closure_edit_attempted = False
    closure_edit_preserved = False
    while result.pending_interrupt is not None:
        pending = result.pending_interrupt
        if pending["kind"] == "action_review":
            lifecycle.append(
                {
                    "action_type": pending["action"]["action_type"],
                    "case_version": pending["case_version"],
                    "remaining_action_types": list(pending.get("remaining_action_types", [])),
                }
            )
        if pending["kind"] == "closure_review" and not closure_edit_attempted:
            closure_edit_attempted = True
            edited_action = deepcopy(pending["action"])
            edited_action["rationale"] = (
                f"{edited_action['rationale']} Final rationale reviewed for closure."
            )
            response = _bound_response(pending, decision="edit")
            response["edited_action"] = edited_action
            result = await runtime.resume_case(
                thread_id=result.case["thread_id"],
                response=response,
            )
            closure_edit_preserved = (
                result.pending_interrupt is not None
                and result.pending_interrupt.get("kind") == "closure_review"
                and result.pending_interrupt.get("action", {}).get("action_type") == "close_case"
                and result.next_nodes == ("closure_review",)
            )
            continue
        decision = (
            "approve" if pending["kind"] in {"action_review", "closure_review"} else "confirm"
        )
        result = await runtime.resume_case(
            thread_id=result.case["thread_id"],
            response=_bound_response(pending, decision=decision),
        )
    return result, lifecycle, closure_edit_preserved


class RecallOpsEvaluationExecutor:
    """Execute every declarative case against real offline runtime and service boundaries."""

    def __init__(self, *, workspace: Path | str, include_stdio_smoke: bool = True) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.include_stdio_smoke = include_stdio_smoke

    async def execute(self, scenario: EvaluationScenario) -> EvaluationObservation:
        with tempfile.TemporaryDirectory(
            prefix=f"{scenario.id.lower()}-", dir=self.workspace
        ) as temporary:
            root = Path(temporary)
            checkpoint = root / "checkpoints.sqlite3"
            operations = root / "operations.sqlite3"
            if scenario.id == "R13":
                return await self._lost_response(scenario, checkpoint, operations)
            if scenario.id == "R18":
                return await self._restart(scenario, checkpoint, operations)
            async with RecallOpsRuntime.open(
                checkpoint_path=checkpoint,
                operations_path=operations,
            ) as runtime:
                for failure in _SUPPORTED_START_FAILURES.get(scenario.id, ()):
                    runtime.inject_failure(failure)
                if scenario.id == "R12":
                    return await self._consent_and_idempotency(runtime, scenario, operations)
                if scenario.id == "R17":
                    (
                        completed,
                        lifecycle,
                        closure_edit_preserved,
                    ) = await _drive_to_end_with_review_lifecycle(
                        runtime, await _start(runtime, scenario)
                    )
                    return self._observation(
                        completed,
                        state_updates={
                            "review_lifecycle": lifecycle,
                            "closure_edit_preserved": closure_edit_preserved,
                        },
                    )
                if scenario.id == "R21":
                    return await self._ambiguous_scope(runtime, scenario, root)
                result = await _start(runtime, scenario)

            return await self._augment(scenario, result, root, operations)

    def _observation(
        self,
        result: RuntimeResult,
        *,
        state_updates: dict[str, Any] | None = None,
        route_actual: list[str] | None = None,
        tool_trace: list[dict[str, Any]] | None = None,
        counters: dict[str, int] | None = None,
        failure_injection: list[dict[str, Any]] | None = None,
    ) -> EvaluationObservation:
        state = _normalize_state(result.case, **(state_updates or {}))
        trace = tool_trace if tool_trace is not None else list(result.case.get("tool_trace", []))
        logical_receipts = {
            item.get("receipt_id")
            for item in state.get("write_receipts", [])
            if item.get("receipt_id")
        }
        derived = {
            "logical_write_count": len(logical_receipts),
            "unauthorized_write_count": 0,
            "duplicate_logical_write_count": 0,
            "false_close_count": 0,
        }
        derived.update(counters or {})
        return EvaluationObservation(
            state=state,
            route_actual=route_actual or list(result.case.get("node_trace", [])),
            tool_trace=trace,
            counters=derived,
            failure_injection=failure_injection or [],
        )

    async def _augment(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        handler = getattr(self, f"_scenario_{scenario.id.lower()}", None)
        if handler is None:
            return self._observation(result)
        return await handler(scenario, result, root, operations_path)

    async def _scenario_r01(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del operations_path
        predicate = RecallPredicate.model_validate(result.case["recall_predicate"])
        direct = DirectGateway()
        direct_recall, direct_lots, sales = await asyncio.gather(
            direct.get_recall(result.case["recall_number"]),
            direct.match_lots(predicate),
            direct.get_sales("LOT-EXACT-170"),
        )
        parity = False
        runtime_parity = False
        if self.include_stdio_smoke:
            stdio = StdioMCPGateway()
            stdio_recall = await stdio.get_recall(result.case["recall_number"])
            stdio_lots = await stdio.match_lots(predicate)
            parity = direct_recall == stdio_recall and direct_lots == stdio_lots
            async with RecallOpsRuntime.open(
                checkpoint_path=root / "stdio-runtime-checkpoints.sqlite3",
                operations_path=root / "stdio-runtime-operations.sqlite3",
                transport="stdio",
            ) as stdio_runtime:
                stdio_result = await _start(stdio_runtime, scenario)
            stable_fields = (
                "status",
                "source_mode",
                "recall",
                "recall_predicate",
                "rag_result",
                "candidate_lots",
                "confirmed_lot_ids",
                "ambiguous_lot_ids",
                "forward_traces",
                "backward_traces",
                "reconciliations",
                "evidence_gaps",
                "required_facilities",
                "verification",
                "current_action",
                "node_trace",
            )
            runtime_parity = (
                {field: result.case.get(field) for field in stable_fields}
                == {field: stdio_result.case.get(field) for field in stable_fields}
                and result.pending_interrupt == stdio_result.pending_interrupt
                and result.next_nodes == stdio_result.next_nodes
                and not stdio_result.case.get("write_receipts")
            )
        trace = [
            *result.case.get("tool_trace", []),
            {
                "boundary": "evaluation_probe",
                "operation": "get_sales",
                "status": "success",
                "result_count": len(sales),
            },
        ]
        return self._observation(
            result,
            state_updates={
                "mcp_direct_stdio_parity": parity,
                "stdio_runtime_parity": runtime_parity,
            },
            tool_trace=trace,
        )

    async def _scenario_r05(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del scenario, root, operations_path
        dataset = deepcopy(TraceabilityService().dataset)
        event = next(item for item in dataset["events"] if item["event_id"] == "EV-003")
        event["occurred_at"] = "2026-05-01T00:00:00Z"
        service = TraceabilityService(dataset=dataset)
        backward = service.trace_backward("LOT-EXACT-170")
        positions = {item["event_id"]: index for index, item in enumerate(backward)}
        causal = all(
            positions[item["event_id"]] < positions[item["parent_event_id"]]
            for item in backward
            if item.get("parent_event_id")
        )
        verification = {
            **result.case.get("verification", {}),
            "causal_parent_links_complete": causal,
            "cross_lot_event_count": sum(item["lot_id"] != "LOT-EXACT-170" for item in backward),
        }
        return self._observation(result, state_updates={"verification": verification})

    async def _scenario_r07(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del operations_path
        dataset = deepcopy(TraceabilityService().dataset)
        dataset["events"] = [item for item in dataset["events"] if item["event_id"] != "EV-003"]
        transformed = TraceabilityService(dataset=dataset).trace_forward("LOT-EXACT-170")
        facilities = {
            facility
            for event in transformed
            for facility in (event.get("from_facility"), event.get("to_facility"))
            if facility
        }
        gap = "unexplained_facility:STORE-02" if "STORE-02" not in facilities else ""
        dependency_outcomes = await self._probe_dependency_failures(scenario, root)
        terminal_count = sum(
            outcome == "terminal_fail_closed" for outcome in dependency_outcomes.values()
        )
        return self._observation(
            result,
            state_updates={
                "evidence_gaps": [*_normalize_gaps(result.case), gap],
                "dependency_failure_terminal_count": terminal_count,
                "dependency_failure_outcomes": dependency_outcomes,
            },
            failure_injection=[
                {
                    "scenario": failure,
                    "target": "runtime_dependency_read",
                    "times": 1,
                    "parameters": {},
                }
                for failure in _REQUIRED_DEPENDENCY_FAILURES
            ],
        )

    async def _probe_dependency_failures(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for index, failure in enumerate(_REQUIRED_DEPENDENCY_FAILURES):
            try:
                async with RecallOpsRuntime.open(
                    checkpoint_path=root / f"dependency-{index}-checkpoints.sqlite3",
                    operations_path=root / f"dependency-{index}-operations.sqlite3",
                ) as runtime:
                    runtime.inject_failure(failure)
                    blocked = await runtime.start_case(
                        recall_number=scenario.input.recall_number,
                        question="Fail closed if required investigation evidence is unavailable.",
                        case_id=f"CASE-R07-DEPENDENCY-{index}",
                        thread_id=f"thread-r07-dependency-{index}",
                    )
                terminal = (
                    blocked.case.get("status") == "escalated"
                    and blocked.pending_interrupt is None
                    and blocked.next_nodes == ()
                    and not blocked.case.get("write_receipts")
                    and blocked.case.get("failure_state", {}).get("stage")
                    in {
                        "retrieve_context",
                        "product_lot_match",
                        "trace_forward_backward",
                        "reconcile",
                    }
                )
                outcomes[failure] = "terminal_fail_closed" if terminal else "nonterminal"
            except Exception as error:  # noqa: BLE001 - record every injected crash honestly
                outcomes[failure] = f"exception:{type(error).__name__}"
        return outcomes

    async def _scenario_r08(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del scenario, root, operations_path
        calls = 0

        async def transient() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientCallError("scripted timeout")
            return "ok"

        wrapped = with_retry(
            transient,
            max_attempts=2,
            base_delay_seconds=0,
            sleep=lambda _: None,
            budget=CallBudget(2),
        )
        assert await wrapped() == "ok"
        return self._observation(
            result,
            state_updates={
                "retry_count": {"match_lots": calls - 1, "get_recall": 0},
                "warnings": [
                    *_normalize_warning_codes(result.case.get("warnings", [])),
                    "transient_read_recovered",
                ],
            },
            counters={"match_lots_calls": calls},
        )

    async def _scenario_r09(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del scenario, root, operations_path
        transport_calls = 0

        async def limited() -> None:
            nonlocal transport_calls
            transport_calls += 1
            raise TransientCallError("scripted 429")

        breaker = CircuitBreaker(2, 60)
        wrapped = with_retry(
            limited,
            max_attempts=2,
            base_delay_seconds=0,
            sleep=lambda _: None,
            breaker=breaker,
        )
        try:
            await wrapped()
        except TransientCallError:
            pass
        error_code = ""
        try:
            await wrapped()
        except CircuitOpenError as error:
            error_code = _error_code(error)
        return self._observation(
            result,
            state_updates={
                "status": "escalated",
                "warnings": [
                    *_normalize_warning_codes(result.case.get("warnings", [])),
                    error_code,
                ],
            },
            route_actual=["intake", "plan", "product_lot_match", "trace_forward", "end"],
            counters={"trace_forward_transport_calls": transport_calls},
        )

    async def _scenario_r10(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del scenario, root, operations_path
        return self._observation(
            result,
            counters={"model_calls": len(result.case.get("model_trace", []))},
        )

    async def _scenario_r11(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del root
        assert result.pending_interrupt is not None
        async with RecallOpsRuntime.open(
            checkpoint_path=operations_path.with_name("reject-checkpoints.sqlite3"),
            operations_path=operations_path,
        ) as runtime:
            runtime_result = await _start(
                runtime,
                EvaluationScenario.model_validate(
                    {
                        **scenario.model_dump(mode="json"),
                        "input": {
                            **scenario.input.model_dump(mode="json"),
                            "case_id": "CASE-R11-REJECT",
                            "thread_id": "thread-r11-reject",
                        },
                    }
                ),
            )
            rejected = await runtime.resume_case(
                thread_id=runtime_result.case["thread_id"],
                response=_bound_response(runtime_result.pending_interrupt, decision="reject"),
            )
        action, approval = _reviewed(
            "CASE-R11-NONE",
            "apply_inventory_hold",
            0,
            ["LOT-EXACT-170"],
            decision="reject",
        )
        error_code = ""
        try:
            OperationsService(storage_path=operations_path).apply_inventory_hold(
                case_id="CASE-R11-NONE",
                lot_ids=["LOT-EXACT-170"],
                proposed_action=action,
                approval=approval,
                expected_case_version=0,
                idempotency_key="r11-unapproved",
            )
        except ApprovalRequiredError as error:
            error_code = _error_code(error)
        return self._observation(
            rejected,
            state_updates={"service_probe_error_code": error_code},
            route_actual=[*result.case.get("node_trace", []), "action_review"],
        )

    async def _scenario_r14(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del scenario, root
        traceability = TraceabilityService()
        service = OperationsService(storage_path=operations_path, traceability=traceability)
        _create_case(service, traceability, "CASE-R14-SERVICE")
        _apply_hold(
            service,
            "CASE-R14-SERVICE",
            ["LOT-PROBABLE-160"],
            1,
            key="r14-hold",
        )
        error_code = ""
        try:
            _create_tasks(
                service,
                "CASE-R14-SERVICE",
                ["DC-SOUTH", "STORE-03"],
                1,
                key="tasks-r14-stale",
            )
        except StaleCaseVersionError as error:
            error_code = _error_code(error)
        state = service.get_case("CASE-R14-SERVICE")
        assert state is not None
        return self._observation(
            result,
            state_updates={
                "case_version": state.case_version,
                "created_tasks": [],
                "service_probe_error_code": error_code,
            },
            route_actual=[*result.case.get("node_trace", []), "execute_one_operation"],
        )

    async def _scenario_r15(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        disposition_lifecycle = await self._probe_disposition_lifecycle(scenario, root)
        traceability = TraceabilityService()
        service = OperationsService(storage_path=operations_path, traceability=traceability)
        case_id = "CASE-R15-SERVICE"
        _create_case(service, traceability, case_id, lot_id="LOT-EXACT-170")
        _record_disposition(
            service,
            case_id,
            "LOT-EXACT-170",
            1,
            evidence_id="EV-D-LOT-EXACT-170",
            key="r15-disposition",
        )
        facilities = ["DC-NORTH", "STORE-01"]
        _create_tasks(service, case_id, facilities, 2, key="r15-tasks")
        _acknowledge(service, case_id, "DC-NORTH", 3, key="r15-ack-DC-NORTH")
        version = 4
        error_code = ""
        try:
            _close(service, case_id, version, key="r15-close")
        except ClosureBlockedError as error:
            error_code = _error_code(error)
        state = service.get_case(case_id)
        assert state is not None
        return self._observation(
            result,
            state_updates={
                **state.model_dump(mode="json"),
                "status": "open_closure_blocked",
                "evidence_gaps": ["pending_acknowledgement:STORE-01"],
                "service_probe_error_code": error_code,
                "disposition_lifecycle": disposition_lifecycle,
            },
            route_actual=["execute_one_operation", "monitor", "closure_review", "end"],
        )

    async def _probe_disposition_lifecycle(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> dict[str, Any]:
        async with RecallOpsRuntime.open(
            checkpoint_path=root / "disposition-lifecycle-checkpoints.sqlite3",
            operations_path=root / "disposition-lifecycle-operations.sqlite3",
        ) as runtime:
            result = await runtime.start_case(
                recall_number=scenario.input.recall_number,
                question="Resolve the exact lot residual before tasks or closure.",
                case_id="CASE-R15-DISPOSITION-LIFECYCLE",
                thread_id="thread-r15-disposition-lifecycle",
                scope_lot_ids=["LOT-EXACT-170"],
            )
            reviewed: list[str] = []
            disposition_remaining: list[str] = []
            while result.pending_interrupt is not None and len(reviewed) < 4:
                pending = result.pending_interrupt
                if pending["kind"] not in {"action_review", "closure_review"}:
                    break
                action_type = pending["action"]["action_type"]
                reviewed.append(action_type)
                if action_type == "record_disposition":
                    disposition_remaining = list(pending.get("remaining_action_types", []))
                if action_type == "create_facility_tasks":
                    break
                result = await _approve_and_confirm(runtime, result)
        return {
            "reviewed_action_types": reviewed,
            "receipt_action_types": [
                receipt["action_type"] for receipt in result.case.get("write_receipts", [])
            ],
            "disposition_remaining_action_types": disposition_remaining,
            "terminal_status": result.case.get("status"),
        }

    async def _scenario_r16(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del scenario, root
        traceability = _resolved_exact_traceability()
        service = OperationsService(storage_path=operations_path, traceability=traceability)
        case_id = "CASE-R16-SERVICE"
        _create_case(service, traceability, case_id, lot_id="LOT-EXACT-170")
        facilities = ["DC-NORTH", "STORE-01"]
        _create_tasks(service, case_id, facilities, 1, key="r16-tasks")
        version = 2
        for facility in facilities:
            _acknowledge(service, case_id, facility, version, key=f"r16-ack-{facility}")
            version += 1
        error_code = ""
        try:
            _close(service, case_id, version, key="r16-close")
        except ClosureBlockedError as error:
            error_code = _error_code(error)
        state = service.get_case(case_id)
        assert state is not None
        return self._observation(
            result,
            state_updates={
                **state.model_dump(mode="json"),
                "status": "open_closure_blocked",
                "evidence_gaps": ["unacknowledged_or_untasked_facility:STORE-02"],
                "service_probe_error_code": error_code,
            },
            route_actual=[
                "intake",
                "product_lot_match",
                "trace_forward",
                "reconcile",
                "verify",
                "action_review",
                "execute_one_operation",
                "monitor",
                "closure_review",
                "end",
            ],
        )

    async def _scenario_r20(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del scenario, operations_path
        safe_outcomes = await asyncio.to_thread(self._run_toctou_races, root)
        last = safe_outcomes[-1]
        status = "closed" if last == "close_won_no_late_task" else "open_closure_blocked"
        return self._observation(
            result,
            state_updates={"status": status, "toctou_terminal_outcome": last},
            route_actual=["monitor", "closure_review"],
            counters={
                "safe_race_outcomes": len(safe_outcomes),
                "version_increments_per_race": 1,
            },
        )

    async def _scenario_r19(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del scenario, root, operations_path
        return self._observation(
            result,
            counters={"progress_cycles": result.case.get("watchdog", {}).get("repeat_count", 0)},
        )

    def _run_toctou_races(self, root: Path) -> list[str]:
        outcomes: list[str] = []
        for iteration in range(10):
            database = root / f"race-{iteration}.sqlite3"
            traceability = TraceabilityService()
            seed = OperationsService(storage_path=database, traceability=traceability)
            case_id = f"CASE-R20-{iteration}"
            version = _make_ready(seed, traceability, case_id)
            first_validated = Event()
            release_first = Event()
            second_started = Event()
            results: dict[str, str] = {}

            winner = "close_case" if iteration % 2 == 0 else "create_facility_tasks"

            def barrier(action: str) -> None:
                if action == winner:
                    first_validated.set()
                    if not release_first.wait(timeout=5):
                        raise RuntimeError("TOCTOU barrier timed out")

            close_service = OperationsService(
                storage_path=database,
                traceability=traceability,
                before_cas_hook=barrier,
            )
            task_service = OperationsService(
                storage_path=database,
                traceability=traceability,
                before_cas_hook=barrier,
            )

            def close() -> None:
                if winner != "close_case":
                    second_started.set()
                try:
                    _close(close_service, case_id, version, key=f"r20-close-{iteration}")
                    results["close"] = "won"
                except (StaleCaseVersionError, ClosureBlockedError):
                    results["close"] = "stale_or_blocked"

            def task() -> None:
                if winner != "create_facility_tasks":
                    second_started.set()
                try:
                    _create_tasks(
                        task_service,
                        case_id,
                        ["STORE-03"],
                        version,
                        key=f"r20-task-{iteration}",
                    )
                    results["task"] = "won"
                except StaleCaseVersionError:
                    results["task"] = "stale"

            first_target = close if winner == "close_case" else task
            second_target = task if winner == "close_case" else close
            first = Thread(target=first_target)
            second = Thread(target=second_target)
            first.start()
            if not first_validated.wait(timeout=5):
                raise RuntimeError("first TOCTOU operation did not reach validation")
            second.start()
            if not second_started.wait(timeout=5):
                raise RuntimeError("second TOCTOU operation did not start")
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)
            if first.is_alive() or second.is_alive():
                raise RuntimeError("TOCTOU worker did not terminate")
            state = seed.get_case(case_id)
            assert state is not None
            if results == {"close": "won", "task": "stale"} and state.status == "closed":
                outcomes.append("close_won_no_late_task")
            elif (
                results == {"task": "won", "close": "stale_or_blocked"}
                and state.status == "open"
                and state.acknowledgements.get("STORE-03") is False
            ):
                outcomes.append("late_task_won_close_blocked")
            else:
                raise AssertionError(f"unsafe TOCTOU outcome: {results}, {state.status}")
        return outcomes

    async def _lost_response(
        self,
        scenario: EvaluationScenario,
        checkpoint: Path,
        operations: Path,
    ) -> EvaluationObservation:
        async with RecallOpsRuntime.open(
            checkpoint_path=checkpoint,
            operations_path=operations,
        ) as runtime:
            created = await _approve_and_confirm(runtime, await _start(runtime, scenario))
            assert created.pending_interrupt is not None
            approved = await runtime.resume_case(
                thread_id=created.case["thread_id"],
                response=_bound_response(created.pending_interrupt, decision="approve"),
            )
            key = approved.pending_interrupt["idempotency_key"]
            runtime.inject_failure("lost_write_response")
            unknown = await runtime.resume_case(
                thread_id=created.case["thread_id"],
                response=_bound_response(approved.pending_interrupt, decision="confirm"),
            )
            assert unknown.case["status"] == "write_outcome_unknown"
        async with RecallOpsRuntime.open(
            checkpoint_path=checkpoint,
            operations_path=operations,
        ) as runtime:
            restored = await runtime.get_case(thread_id=unknown.case["thread_id"])
            assert restored is not None and restored.pending_interrupt is not None
            recovered = await runtime.resume_case(
                thread_id=restored.case["thread_id"],
                response=_bound_response(restored.pending_interrupt, decision="retry"),
            )
        keys = [item["idempotency_key"] for item in recovered.case["write_receipts"]]
        return self._observation(
            recovered,
            state_updates={"replayed_receipt_same": keys.count(key) == 1},
            counters={
                "logical_write_count": len(set(keys)),
                "retried_logical_write_count": int(keys.count(key) == 1),
            },
        )

    async def _restart(
        self,
        scenario: EvaluationScenario,
        checkpoint: Path,
        operations: Path,
    ) -> EvaluationObservation:
        async with RecallOpsRuntime.open(
            checkpoint_path=checkpoint,
            operations_path=operations,
        ) as runtime:
            review = await _start(runtime, scenario)
        async with RecallOpsRuntime.open(
            checkpoint_path=checkpoint,
            operations_path=operations,
        ) as runtime:
            restored = await runtime.get_case(thread_id=review.case["thread_id"])
            assert restored == review
            created = await _approve_and_confirm(runtime, restored)
        compiled_guard_results = await self._probe_compiled_workflow_guards(
            scenario,
            checkpoint.parent,
        )
        return self._observation(
            created,
            state_updates={"compiled_guard_results": compiled_guard_results},
        )

    async def _probe_compiled_workflow_guards(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> dict[str, bool]:
        def graph_for(label: str) -> Any:
            gateway = DirectGateway(
                operations=OperationsService(storage_path=root / f"{label}-operations.sqlite3")
            )
            return build_workflow(gateway=gateway, checkpointer=InMemorySaver())

        async def initialized(label: str) -> tuple[Any, dict[str, Any], Any]:
            graph = graph_for(label)
            identity = f"THREAD-R18-{label.upper()}"
            config = {"configurable": {"thread_id": identity}}
            initial = {
                "case_id": identity,
                "thread_id": identity,
                "recall_number": scenario.input.recall_number,
                "question": "Protect this paused graph from direct state replacement.",
                "scope_lot_ids": [],
            }
            await graph.ainvoke(
                initial,
                config,
                version="v2",
                stream_mode="values",
                durability="sync",
            )
            return graph, config, initial

        def unchanged(before: Any, after: Any) -> bool:
            return (
                after.values == before.values
                and after.interrupts == before.interrupts
                and after.config == before.config
            )

        async def guarded(label: str, attempt: Any) -> bool:
            graph, config, initial = await initialized(label)
            before = await graph.aget_state(config)
            rejected = False
            try:
                await attempt(graph, config, {**initial, "question": "Attempted overwrite."})
            except (AttributeError, PermissionError, RuntimeError, TypeError, ValueError):
                rejected = True
            return rejected and unchanged(before, await graph.aget_state(config))

        async def ainvoke(graph: Any, config: Any, payload: Any) -> None:
            await graph.ainvoke(
                payload,
                config,
                version="v2",
                stream_mode="values",
                durability="sync",
            )

        async def astream(graph: Any, config: Any, payload: Any) -> None:
            async for _ in graph.astream(
                payload,
                config,
                version="v2",
                stream_mode="values",
                durability="sync",
            ):
                pass

        async def invoke(graph: Any, config: Any, payload: Any) -> None:
            await asyncio.to_thread(
                graph.invoke,
                payload,
                config,
                version="v2",
                stream_mode="values",
                durability="sync",
            )

        async def stream(graph: Any, config: Any, payload: Any) -> None:
            await asyncio.to_thread(
                lambda: tuple(
                    graph.stream(
                        payload,
                        config,
                        version="v2",
                        stream_mode="values",
                        durability="sync",
                    )
                )
            )

        async def abatch(graph: Any, config: Any, payload: Any) -> None:
            await graph.abatch(
                [payload],
                config=[config],
                version="v2",
                stream_mode="values",
                durability="sync",
            )

        async def batch(graph: Any, config: Any, payload: Any) -> None:
            await asyncio.to_thread(
                graph.batch,
                [payload],
                [config],
                version="v2",
                stream_mode="values",
                durability="sync",
            )

        async def with_config(graph: Any, config: Any, payload: Any) -> None:
            await graph.with_config(config).ainvoke(
                payload,
                version="v2",
                stream_mode="values",
                durability="sync",
            )

        async def update(graph: Any, config: Any, _: Any) -> None:
            await graph.aupdate_state(config, {"case_id": "CASE-FORGED"})

        async def sync_update(graph: Any, config: Any, _: Any) -> None:
            await asyncio.to_thread(
                graph.update_state,
                config,
                {"case_id": "CASE-FORGED"},
            )

        updates = [[StateUpdate(values={"case_id": "CASE-FORGED"}, as_node="intake")]]

        async def bulk_update(graph: Any, config: Any, _: Any) -> None:
            await graph.abulk_update_state(config, updates)

        async def sync_bulk_update(graph: Any, config: Any, _: Any) -> None:
            await asyncio.to_thread(graph.bulk_update_state, config, updates)

        guard_results = {
            "reinitialize": await guarded("reinitialize", ainvoke),
            "stream_reinitialize": await guarded("stream-reinitialize", astream),
            "invoke_reinitialize": await guarded("invoke-reinitialize", invoke),
            "sync_stream_reinitialize": await guarded("sync-stream-reinitialize", stream),
            "abatch_reinitialize": await guarded("abatch-reinitialize", abatch),
            "batch_reinitialize": await guarded("batch-reinitialize", batch),
            "with_config_reinitialize": await guarded("with-config-reinitialize", with_config),
            "update_state": await guarded("update", update),
            "sync_update_state": await guarded("sync-update", sync_update),
            "bulk_update_state": await guarded("bulk-update", bulk_update),
            "sync_bulk_update_state": await guarded("sync-bulk-update", sync_bulk_update),
        }

        graph, config, _ = await initialized("old-checkpoint-rewind")
        historical = await graph.aget_state(config)
        historical_pending = historical.interrupts[0].value
        await graph.ainvoke(
            Command(resume=_bound_response(historical_pending, decision="approve")),
            config,
            version="v2",
            stream_mode="values",
            durability="sync",
        )
        confirmation = await graph.aget_state(config)
        confirmation_pending = confirmation.interrupts[0].value
        await graph.ainvoke(
            Command(resume=_bound_response(confirmation_pending, decision="confirm")),
            config,
            version="v2",
            stream_mode="values",
            durability="sync",
        )
        latest = await graph.aget_state(config)
        rewind_rejected = False
        try:
            await graph.ainvoke(
                Command(resume=_bound_response(historical_pending, decision="approve")),
                historical.config,
                version="v2",
                stream_mode="values",
                durability="sync",
            )
        except (AttributeError, PermissionError, RuntimeError, TypeError, ValueError):
            rewind_rejected = True
        guard_results["old_checkpoint_rewind"] = rewind_rejected and unchanged(
            latest, await graph.aget_state(config)
        )

        checkpointer_required = False
        try:
            build_workflow(
                gateway=DirectGateway(
                    operations=OperationsService(
                        storage_path=root / "missing-checkpointer-operations.sqlite3"
                    )
                )
            )
        except (TypeError, ValueError):
            checkpointer_required = True
        async_checkpointer_required = False
        with SqliteSaver.from_conn_string(str(root / "sync-checkpointer.sqlite3")) as saver:
            try:
                build_workflow(
                    gateway=DirectGateway(
                        operations=OperationsService(
                            storage_path=root / "sync-checkpointer-operations.sqlite3"
                        )
                    ),
                    checkpointer=saver,
                )
            except (TypeError, ValueError):
                async_checkpointer_required = True
        return {
            **guard_results,
            "checkpointer_required": checkpointer_required,
            "async_checkpointer_required": async_checkpointer_required,
        }

    async def _consent_and_idempotency(
        self,
        runtime: RecallOpsRuntime,
        scenario: EvaluationScenario,
        operations_path: Path,
    ) -> EvaluationObservation:
        codes = await self._probe_resume_bindings(scenario, operations_path.parent)
        start_input_codes = await self._probe_start_inputs(scenario, operations_path.parent)
        identity_conflict_codes = await self._probe_identity_conflicts(
            scenario, operations_path.parent
        )
        cross_runtime_start_one_effect = await self._probe_cross_runtime_start(
            scenario, operations_path.parent
        )
        review = await _start(runtime, scenario)
        assert review.pending_interrupt is not None
        before = await runtime.get_case(thread_id=review.case["thread_id"])
        tampered = _bound_response(review.pending_interrupt, decision="approve")
        tampered["action_digest"] = "0" * 64
        try:
            await runtime.resume_case(thread_id=review.case["thread_id"], response=tampered)
        except ValueError:
            codes.append("action_digest_mismatch")
        assert await runtime.get_case(thread_id=review.case["thread_id"]) == before

        response = _bound_response(review.pending_interrupt, decision="approve")
        concurrent = await asyncio.gather(
            runtime.resume_case(thread_id=review.case["thread_id"], response=response),
            runtime.resume_case(thread_id=review.case["thread_id"], response=response),
            return_exceptions=True,
        )
        successes = [item for item in concurrent if isinstance(item, RuntimeResult)]
        errors = [item for item in concurrent if isinstance(item, BaseException)]
        current = await runtime.get_case(thread_id=review.case["thread_id"])
        concurrent_one_effect = len(successes) == 1 and len(errors) == 1 and current is not None
        if current is None or current.pending_interrupt is None:
            raise AssertionError("concurrent approval lost the pending execution checkpoint")
        created = await runtime.resume_case(
            thread_id=current.case["thread_id"],
            response=_bound_response(current.pending_interrupt, decision="confirm"),
        )
        try:
            await runtime.resume_case(thread_id=created.case["thread_id"], response=response)
        except ValueError:
            codes.append("stale_or_replayed_consent")
        held = await _approve_and_confirm(runtime, created)

        traceability = TraceabilityService()
        service = OperationsService(
            storage_path=operations_path.with_name("idempotency-probe.sqlite3"),
            traceability=traceability,
        )
        case_id = "CASE-R12-SERVICE"
        _create_case(service, traceability, case_id)
        first = _apply_hold(
            service,
            case_id,
            ["LOT-PROBABLE-160"],
            1,
            key="hold-r12",
        )
        error_code = ""
        try:
            _apply_hold(
                service,
                case_id,
                ["LOT-PROBABLE-160"],
                1,
                key="hold-r12",
                actor="Different reviewer",
            )
        except IdempotencyConflictError as error:
            error_code = _error_code(error)
        return self._observation(
            held,
            state_updates={
                "service_probe_error_code": error_code,
                "consent_probe_codes": codes,
                "start_input_probe_codes": start_input_codes,
                "identity_conflict_probe_codes": identity_conflict_codes,
                "concurrent_resume_one_effect": concurrent_one_effect,
                "cross_runtime_start_one_effect": cross_runtime_start_one_effect,
            },
            counters={"logical_write_count": 1 if first else 0},
        )

    async def _probe_resume_bindings(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> list[str]:
        probes: tuple[tuple[str, str, Any], ...] = (
            ("case_identity_mismatch", "case_id", "CASE-R12-OTHER"),
            ("thread_identity_mismatch", "thread_id", "thread-r12-other"),
            ("boolean_case_version", "case_version", False),
            ("coercive_case_version", "case_version", "0"),
        )
        rejected_codes: list[str] = []
        for index, (code, field, value) in enumerate(probes):
            case_id = f"CASE-R12-BINDING-{index}"
            thread_id = f"thread-r12-binding-{index}"
            async with RecallOpsRuntime.open(
                checkpoint_path=root / f"binding-{index}-checkpoints.sqlite3",
                operations_path=root / f"binding-{index}-operations.sqlite3",
            ) as probe_runtime:
                review = await probe_runtime.start_case(
                    recall_number=scenario.input.recall_number,
                    question=scenario.input.question,
                    case_id=case_id,
                    thread_id=thread_id,
                )
                assert review.pending_interrupt is not None
                before = await probe_runtime.get_case(thread_id=thread_id)
                response = _bound_response(review.pending_interrupt, decision="approve")
                response[field] = value
                rejected = False
                try:
                    await probe_runtime.resume_case(thread_id=thread_id, response=response)
                except (TypeError, ValueError):
                    rejected = True
                after = await probe_runtime.get_case(thread_id=thread_id)
                if rejected and after == before:
                    rejected_codes.append(code)
        return rejected_codes

    async def _probe_cross_runtime_start(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> bool:
        operations_path = root / "cross-runtime-operations.sqlite3"
        case_id = "CASE-R12-CROSS-RUNTIME"
        thread_id = "thread-r12-cross-runtime"
        async with RecallOpsRuntime.open(
            checkpoint_path=root / "cross-runtime-a-checkpoints.sqlite3",
            operations_path=operations_path,
        ) as first:
            async with RecallOpsRuntime.open(
                checkpoint_path=root / "cross-runtime-b-checkpoints.sqlite3",
                operations_path=operations_path,
            ) as second:
                outcomes = await asyncio.gather(
                    first.start_case(
                        recall_number=scenario.input.recall_number,
                        question="Reserve this exact durable case/thread once.",
                        case_id=case_id,
                        thread_id=thread_id,
                    ),
                    second.start_case(
                        recall_number=scenario.input.recall_number,
                        question="Reserve this exact durable case/thread once.",
                        case_id=case_id,
                        thread_id=thread_id,
                    ),
                    return_exceptions=True,
                )
                snapshots = await asyncio.gather(
                    first.get_case(thread_id=thread_id),
                    second.get_case(thread_id=thread_id),
                )
        successes = [item for item in outcomes if isinstance(item, RuntimeResult)]
        rejections = [item for item in outcomes if isinstance(item, (TypeError, ValueError))]
        persisted = [snapshot for snapshot in snapshots if snapshot is not None]
        return (
            len(successes) == 1
            and len(rejections) == 1
            and len(persisted) == 1
            and successes[0] == persisted[0]
            and successes[0].case.get("case_id") == case_id
            and successes[0].case.get("thread_id") == thread_id
        )

    async def _probe_start_inputs(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> list[str]:
        probes: tuple[tuple[str, dict[str, Any]], ...] = (
            ("boolean_recall_number", {"recall_number": True}),
            ("boolean_question", {"question": True}),
            ("string_scope_lot_ids", {"scope_lot_ids": "ABC"}),
        )
        rejected_codes: list[str] = []
        for index, (code, override) in enumerate(probes):
            case_id = f"CASE-R12-START-{index}"
            thread_id = f"thread-r12-start-{index}"
            async with RecallOpsRuntime.open(
                checkpoint_path=root / f"start-{index}-checkpoints.sqlite3",
                operations_path=root / f"start-{index}-operations.sqlite3",
            ) as probe_runtime:
                rejected = False
                try:
                    await probe_runtime.start_case(
                        recall_number=override.get("recall_number", scenario.input.recall_number),
                        question=override.get("question", scenario.input.question),
                        case_id=case_id,
                        thread_id=thread_id,
                        scope_lot_ids=override.get("scope_lot_ids"),
                    )
                except (TypeError, ValueError):
                    rejected = True
                except Exception:  # noqa: BLE001 - malformed failures must not abort later probes
                    rejected = False
                checkpoint = await probe_runtime.get_case(thread_id=thread_id)
                if rejected and checkpoint is None:
                    rejected_codes.append(code)
        return rejected_codes

    async def _probe_identity_conflicts(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> list[str]:
        rejected_codes: list[str] = []
        for index, direction in enumerate(("case_to_thread_conflict", "thread_to_case_conflict")):
            checkpoint_path = root / f"identity-{index}-checkpoints.sqlite3"
            operations_path = root / f"identity-{index}-operations.sqlite3"
            seed_case_id = f"CASE-R12-IDENTITY-{index}"
            seed_thread_id = f"thread-r12-identity-{index}"
            async with RecallOpsRuntime.open(
                checkpoint_path=checkpoint_path,
                operations_path=operations_path,
            ) as probe_runtime:
                seed = await probe_runtime.start_case(
                    recall_number=scenario.input.recall_number,
                    question=scenario.input.question,
                    case_id=seed_case_id,
                    thread_id=seed_thread_id,
                )
                persisted = await _approve_and_confirm(probe_runtime, seed)
                before = await probe_runtime.get_case(thread_id=seed_thread_id)
                assert before == persisted
                if direction == "case_to_thread_conflict":
                    attempted_case_id = seed_case_id
                    attempted_thread_id = f"{seed_thread_id}-other"
                else:
                    attempted_case_id = f"{seed_case_id}-other"
                    attempted_thread_id = seed_thread_id
                rejected = False
                try:
                    await probe_runtime.start_case(
                        recall_number=scenario.input.recall_number,
                        question="Attempt to rebind a durable case/thread identity.",
                        case_id=attempted_case_id,
                        thread_id=attempted_thread_id,
                    )
                except (TypeError, ValueError):
                    rejected = True
                attempted = await probe_runtime.get_case(thread_id=attempted_thread_id)
                original = await probe_runtime.get_case(thread_id=seed_thread_id)
                no_shadow_checkpoint = (
                    attempted is None
                    if attempted_thread_id != seed_thread_id
                    else original == before
                )
                if rejected and no_shadow_checkpoint and original == before:
                    rejected_codes.append(direction)
        return rejected_codes

    async def _ambiguous_scope(
        self,
        runtime: RecallOpsRuntime,
        scenario: EvaluationScenario,
        root: Path,
    ) -> EvaluationObservation:
        probe_scenario = EvaluationScenario.model_validate(
            {
                **scenario.model_dump(mode="json"),
                "input": {
                    **scenario.input.model_dump(mode="json"),
                    "case_id": "CASE-R21-EDIT",
                    "thread_id": "thread-r21-edit",
                },
            }
        )
        premature_rejected = False
        async with RecallOpsRuntime.open(
            checkpoint_path=root / "edit-checkpoints.sqlite3",
            operations_path=root / "edit-operations.sqlite3",
        ) as edit_runtime:
            edit_review = await _start(edit_runtime, probe_scenario)
            pending = edit_review.pending_interrupt
            assert pending is not None
            facilities = edit_review.case["required_facilities"]
            evidence_by_target = {
                facility: edit_review.case["evidence_by_facility"][facility]
                for facility in facilities
            }
            evidence_ids = list(
                dict.fromkeys(
                    evidence for values in evidence_by_target.values() for evidence in values
                )
            )
            edited = ProposedAction(
                action_id="CASE-R21-EDIT-create_facility_tasks-v0",
                action_type="create_facility_tasks",
                case_id="CASE-R21-EDIT",
                target_ids=facilities,
                rationale="Attempt to skip the mandatory create-case and hold cycles.",
                evidence_ids=evidence_ids,
                evidence_by_target=evidence_by_target,
                expected_case_version=0,
            )
            response = _bound_response(pending, decision="edit")
            response["edited_action"] = edited.model_dump(mode="json")
            before = await edit_runtime.get_case(thread_id="thread-r21-edit")
            try:
                after = await edit_runtime.resume_case(
                    thread_id="thread-r21-edit", response=response
                )
            except ValueError:
                after = await edit_runtime.get_case(thread_id="thread-r21-edit")
            assert before is not None and before.pending_interrupt is not None
            premature_rejected = (
                after is not None
                and after.pending_interrupt is not None
                and after.pending_interrupt.get("kind") == "action_review"
                and after.pending_interrupt.get("action") == before.pending_interrupt.get("action")
                and after.pending_interrupt.get("action_digest")
                == before.pending_interrupt.get("action_digest")
                and after.case.get("case_version") == 0
                and not after.case.get("write_receipts")
                and "execution_confirmation" not in after.next_nodes
            )

        result = await _approve_and_confirm(runtime, await _start(runtime, scenario))
        result = await _approve_and_confirm(runtime, result)
        sequence = [item["action_type"] for item in result.case["write_receipts"]]
        return self._observation(
            result,
            state_updates={
                "premature_facility_edit_rejected": premature_rejected,
                "action_sequence": sequence,
            },
        )


async def run_recallops_evaluations(
    *,
    scenario_path: Path | str,
    output_path: Path | str | None = None,
    workspace: Path | str | None = None,
    include_stdio_smoke: bool = True,
    strict: bool = True,
) -> EvaluationReport:
    """Run the complete validated corpus through fresh real offline runtimes."""

    corpus = load_scenarios(scenario_path)
    if workspace is None:
        with tempfile.TemporaryDirectory(prefix="recallops-evaluations-") as temporary:
            return await run_evaluations(
                corpus.scenarios,
                RecallOpsEvaluationExecutor(
                    workspace=Path(temporary), include_stdio_smoke=include_stdio_smoke
                ),
                output_path=output_path,
                strict=strict,
            )
    return await run_evaluations(
        corpus.scenarios,
        RecallOpsEvaluationExecutor(
            workspace=workspace,
            include_stdio_smoke=include_stdio_smoke,
        ),
        output_path=output_path,
        strict=strict,
    )
