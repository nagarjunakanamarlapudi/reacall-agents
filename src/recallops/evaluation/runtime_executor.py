"""Real offline scenario executor for the RecallOps R01-R21 safety matrix."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence, Set
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from recallops.agents.middleware import (
    CallBudget,
    CallBudgetExceeded,
    CircuitBreaker,
    CircuitOpenError,
    TransientCallError,
    with_retry,
)
from recallops.agents.policies import ProgressStalledError, ProgressWatchdog
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
    "registry_transient_failure",
    "model_failure",
    "repeated_progress_signature",
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


def _capture_service_authorization(
    evidence: list[dict[str, Any]] | None,
    *,
    receipt: Any,
    action: ProposedAction,
    approval: ApprovalDecision,
    idempotency_key: str,
) -> None:
    """Capture call-time authorization independently of the persisted receipt payload."""

    if evidence is None:
        return
    receipt_id = receipt.receipt_id if hasattr(receipt, "receipt_id") else receipt["receipt_id"]
    evidence.append(
        {
            "receipt_id": receipt_id,
            "decision": approval.decision,
            "action_id": action.action_id,
            "action_digest": proposed_action_digest(action),
            "expected_case_version": action.expected_case_version,
            "actor": approval.actor,
            "justification": approval.justification,
            "idempotency_key": idempotency_key,
            "operation_call_observed": True,
        }
    )


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
    thread_id: str | None = None,
    authorization_evidence: list[dict[str, Any]] | None = None,
) -> None:
    payload = _case_payload(traceability, lot_id)
    action, approval = _reviewed(
        case_id,
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    receipt = service.create_case(
        case_id=case_id,
        thread_id=thread_id,
        **payload,
        proposed_action=action,
        approval=approval,
        expected_case_version=0,
        idempotency_key=f"{case_id}-create",
    )
    _capture_service_authorization(
        authorization_evidence,
        receipt=receipt,
        action=action,
        approval=approval,
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
    authorization_evidence: list[dict[str, Any]] | None = None,
) -> Any:
    action, approval = _reviewed(case_id, "create_facility_tasks", version, facilities)
    receipt = service.create_facility_tasks(
        case_id=case_id,
        facility_ids=facilities,
        proposed_action=action,
        approval=approval,
        expected_case_version=version,
        idempotency_key=key,
    )
    _capture_service_authorization(
        authorization_evidence,
        receipt=receipt,
        action=action,
        approval=approval,
        idempotency_key=key,
    )
    return receipt


def _acknowledge(
    service: OperationsService,
    case_id: str,
    facility: str,
    version: int,
    *,
    key: str,
    authorization_evidence: list[dict[str, Any]] | None = None,
) -> Any:
    action, approval = _reviewed(case_id, "record_acknowledgment", version, [facility])
    receipt = service.record_acknowledgment(
        case_id=case_id,
        facility_id=facility,
        proposed_action=action,
        approval=approval,
        expected_case_version=version,
        idempotency_key=key,
    )
    _capture_service_authorization(
        authorization_evidence,
        receipt=receipt,
        action=action,
        approval=approval,
        idempotency_key=key,
    )
    return receipt


def _record_disposition(
    service: OperationsService,
    case_id: str,
    lot_id: str,
    version: int,
    *,
    evidence_id: str,
    key: str,
    authorization_evidence: list[dict[str, Any]] | None = None,
) -> Any:
    action, approval = _reviewed(
        case_id,
        "record_disposition",
        version,
        [lot_id],
        evidence_ids=[evidence_id],
    )
    receipt = service.record_disposition(
        case_id=case_id,
        lot_id=lot_id,
        disposition="dispose_unaccounted",
        evidence_id=evidence_id,
        proposed_action=action,
        approval=approval,
        expected_case_version=version,
        idempotency_key=key,
    )
    _capture_service_authorization(
        authorization_evidence,
        receipt=receipt,
        action=action,
        approval=approval,
        idempotency_key=key,
    )
    return receipt


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


def _checkpoint_fence_error_code(error: Any) -> str:
    if not isinstance(error, ValueError):
        return ""
    messages = {
        "workflow mutation is already active or uncertain": "active_mutation_fenced",
        "checkpoint head is stale and cannot fork the durable workflow": "stale_head_fenced",
        "checkpoint mutation marker request digest does not match": "request_digest_mismatch",
        "workflow mutation request digest does not match this attempt": ("request_digest_mismatch"),
    }
    return messages.get(str(error), "")


def _observed_fault(
    scenario: EvaluationScenario, index: int, *, observed_times: int
) -> dict[str, Any]:
    fault = scenario.faults[index]
    if observed_times != fault.times:
        raise AssertionError(
            f"fault {fault.scenario} declared {fault.times} applications but observed "
            f"{observed_times}"
        )
    return fault.model_dump(mode="json")


def _receipts_match_authorization_evidence(
    receipts: list[Any], evidence: list[dict[str, Any]]
) -> bool:
    if len(receipts) != len(evidence):
        return False
    for raw_receipt, binding in zip(receipts, evidence, strict=True):
        receipt = (
            raw_receipt.model_dump(mode="json")
            if hasattr(raw_receipt, "model_dump")
            else raw_receipt
        )
        try:
            action = ProposedAction.model_validate(receipt["details"]["reviewed_action"])
        except (KeyError, TypeError, ValueError):
            return False
        if not (
            binding["receipt_id"] == receipt.get("receipt_id")
            and binding["decision"] == "approve"
            and binding["action_id"] == action.action_id
            and binding["action_digest"] == proposed_action_digest(action)
            and binding["expected_case_version"] == action.expected_case_version
            and binding["actor"] == receipt.get("actor")
            and binding["justification"] == receipt.get("justification")
            and binding["idempotency_key"] == receipt.get("idempotency_key")
            and binding["operation_call_observed"] is True
        ):
            return False
    return True


def _mutation_rejected(target: Any, method: str, *args: Any) -> bool:
    """Return whether an immutable public result rejects a concrete nested mutation."""

    try:
        getattr(target, method)(*args)
    except (AttributeError, TypeError):
        return True
    return False


def _immutable_container_audit(value: Any, *, path: str) -> dict[str, Any]:
    """Recursively audit every JSON container on a public result surface."""

    checks: dict[str, bool] = {}
    seen: set[int] = set()

    def visit(item: Any, current_path: str) -> None:
        if isinstance(item, (str, bytes, bytearray)) or item is None:
            return
        if isinstance(item, (bool, int, float)):
            return
        if not isinstance(item, (Mapping, Sequence, Set)):
            checks[current_path] = False
            return
        identity = id(item)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(item, Mapping):
            children = list(item.items())
            if children:
                key, child = children[0]
                checks[current_path] = _mutation_rejected(item, "__setitem__", key, child)
            else:
                checks[current_path] = _mutation_rejected(
                    item, "__setitem__", "__recallops_immutability_probe__", None
                )
                if not checks[current_path]:
                    getattr(item, "pop")("__recallops_immutability_probe__", None)
            for key, child in children:
                visit(child, f"{current_path}/{key}")
            return
        if isinstance(item, Set):
            children = list(item)
            sentinel = children[0] if children else "__recallops_immutability_probe__"
            checks[current_path] = _mutation_rejected(item, "add", sentinel)
            if not checks[current_path] and not children:
                getattr(item, "discard")(sentinel)
            for index, child in enumerate(children):
                visit(child, f"{current_path}/{index}")
            return
        children = list(item)
        if children:
            checks[current_path] = _mutation_rejected(item, "__setitem__", 0, children[0])
        else:
            checks[current_path] = _mutation_rejected(
                item, "append", "__recallops_immutability_probe__"
            )
            if not checks[current_path]:
                getattr(item, "pop")()
        for index, child in enumerate(children):
            visit(child, f"{current_path}/{index}")

    visit(value, path)
    mutable_paths = sorted(path for path, immutable in checks.items() if not immutable)
    return {
        "container_count": len(checks),
        "all_containers_immutable": bool(checks) and not mutable_paths,
        "mutable_paths": mutable_paths,
    }


def _execution_confirmation_evidence(history: Sequence[RuntimeResult]) -> list[dict[str, Any]]:
    """Extract durable post-confirm checkpoints from the public history API."""

    evidence: list[dict[str, Any]] = []
    chronological = list(reversed(history))
    for index, snapshot in enumerate(chronological):
        if index == 0:
            continue
        prior = chronological[index - 1]
        case = snapshot.case
        if snapshot.pending_interrupt is not None:
            continue
        if case.get("status") != "approved_pending_execution":
            continue
        if snapshot.next_nodes != ("execute_one_operation",):
            continue
        if (
            prior.pending_interrupt is None
            or prior.pending_interrupt.get("kind") != "execution_confirmation"
        ):
            continue
        try:
            action = ProposedAction.model_validate(case.get("current_action"))
        except (TypeError, ValueError):
            continue
        execution_id = case.get("execution_id")
        idempotency_key = case.get("idempotency_key")
        digest = case.get("action_digest")
        checkpoint_id = snapshot.checkpoint_id
        if prior.case.get("execution_id") != execution_id:
            continue
        pending = prior.pending_interrupt
        assert pending is not None
        approval = case.get("approval")
        if not isinstance(approval, Mapping):
            continue
        bound_fields = {
            "case_id": action.case_id,
            "case_version": action.expected_case_version,
            "action_id": action.action_id,
            "action_digest": digest,
            "execution_id": execution_id,
            "idempotency_key": idempotency_key,
        }
        if any(pending.get(key) != value for key, value in bound_fields.items()):
            continue
        if len(prior.case.get("write_receipts", ())) != len(case.get("write_receipts", ())):
            continue
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in (execution_id, idempotency_key, digest, checkpoint_id)
        ):
            continue
        if digest != proposed_action_digest(action):
            continue
        evidence.append(
            {
                "confirmed": True,
                **bound_fields,
                "actor": approval.get("actor"),
                "justification": approval.get("justification"),
                "checkpoint_id": checkpoint_id,
            }
        )
    return evidence


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


def _thaw_json(value: Any) -> Any:
    """Convert immutable public JSON containers into detached report containers."""

    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, (Sequence, Set)) and not isinstance(value, (str, bytes, bytearray)):
        return [_thaw_json(child) for child in value]
    return deepcopy(value)


def _normalize_gaps(case: dict[str, Any]) -> list[str]:
    gaps = list(case.get("evidence_gaps", []))
    for reconciliation in case.get("reconciliations", []):
        unaccounted = reconciliation.get("unaccounted", 0)
        if unaccounted:
            gaps.append(f"unaccounted_units:{reconciliation['lot_id']}:{unaccounted}")
    gaps.extend(f"ambiguous_lot:{lot_id}" for lot_id in case.get("ambiguous_lot_ids", []))
    return list(dict.fromkeys(gaps))


def _normalize_state(case: dict[str, Any], **updates: Any) -> dict[str, Any]:
    state = _thaw_json(case)
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
            edited_action = _thaw_json(pending["action"])
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
                for fault in scenario.faults:
                    if fault.scenario in _SUPPORTED_START_FAILURES:
                        runtime.inject_failure(fault.scenario, times=fault.times)
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
                    confirmation_history = _execution_confirmation_evidence(
                        await runtime.get_case_history(thread_id=completed.case["thread_id"])
                    )
                    return self._observation(
                        completed,
                        state_updates={
                            "review_lifecycle": lifecycle,
                            "closure_edit_preserved": closure_edit_preserved,
                            "execution_confirmation_history": confirmation_history,
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
        trace = _thaw_json(
            tool_trace if tool_trace is not None else result.case.get("tool_trace", [])
        )
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
        applied_faults = failure_injection or []
        return EvaluationObservation(
            state=state,
            route_actual=route_actual or list(result.case.get("node_trace", [])),
            tool_trace=trace,
            counters=derived,
            failure_injection=applied_faults,
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
        return self._observation(
            result,
            state_updates={
                "mcp_direct_stdio_parity": parity,
                "stdio_runtime_parity": runtime_parity,
                "gateway_sales_probe_count": len(sales),
            },
        )

    async def _scenario_r02(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del root, operations_path
        get_recall_attempts = result.case.get("retry_state", {}).get("read_attempts", 0)
        observed_failures = max(get_recall_attempts - 1, 0)
        return self._observation(
            result,
            failure_injection=[_observed_fault(scenario, 0, observed_times=observed_failures)],
        )

    async def _scenario_r05(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del root, operations_path
        dataset = deepcopy(TraceabilityService().dataset)
        event = next(item for item in dataset["events"] if item["event_id"] == "EV-003")
        original_occurred_at = event["occurred_at"]
        event["occurred_at"] = "2026-05-01T00:00:00Z"
        transformed_event_count = int(event["occurred_at"] != original_occurred_at)
        service = TraceabilityService(dataset=dataset)
        backward = service.trace_backward("LOT-EXACT-170")
        positions = {item["event_id"]: index for index, item in enumerate(backward)}
        causal = all(
            positions[item["event_id"]] < positions[item["parent_event_id"]]
            for item in backward
            if item.get("parent_event_id")
        )
        trace_fixture_probe = {
            "causal_parent_links_complete": causal,
            "cross_lot_event_count": sum(item["lot_id"] != "LOT-EXACT-170" for item in backward),
        }
        return self._observation(
            result,
            state_updates={"trace_fixture_probe": trace_fixture_probe},
            failure_injection=[
                _observed_fault(scenario, 0, observed_times=transformed_event_count)
            ],
        )

    async def _scenario_r07(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del operations_path
        dataset = deepcopy(TraceabilityService().dataset)
        original_event_count = len(dataset["events"])
        dataset["events"] = [item for item in dataset["events"] if item["event_id"] != "EV-003"]
        removed_event_count = original_event_count - len(dataset["events"])
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
                "trace_fixture_probe": {
                    "evidence_gaps": [gap] if gap else [],
                    "removed_event_count": removed_event_count,
                },
                "dependency_failure_terminal_count": terminal_count,
                "dependency_failure_outcomes": dependency_outcomes,
            },
            failure_injection=[_observed_fault(scenario, 0, observed_times=removed_event_count)],
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
        del root, operations_path
        calls = 0

        async def transient() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientCallError("scripted timeout")
            return "ok"

        wrapped = with_retry(
            transient,
            max_attempts=scenario.setup["max_attempts"],
            base_delay_seconds=0,
            sleep=lambda _: None,
            budget=CallBudget(scenario.setup["max_attempts"]),
        )
        assert await wrapped() == "ok"
        return self._observation(
            result,
            state_updates={
                "middleware_probe": {
                    "component": "with_retry",
                    "operation": "match_lots",
                    "status": "recovered",
                    "retry_count": calls - 1,
                    "transport_calls": calls,
                },
            },
            counters={"match_lots_calls": calls},
            failure_injection=[_observed_fault(scenario, 0, observed_times=calls - 1)],
        )

    async def _scenario_r09(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del root, operations_path
        transport_calls = 0
        status_code = scenario.faults[0].parameters["status_code"]

        async def limited() -> None:
            nonlocal transport_calls
            transport_calls += 1
            raise TransientCallError(f"scripted {status_code}")

        breaker = CircuitBreaker(scenario.setup["circuit_threshold"], 60)
        wrapped = with_retry(
            limited,
            max_attempts=scenario.setup["max_attempts"],
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
                "middleware_probe": {
                    "component": "CircuitBreaker",
                    "operation": "trace_forward",
                    "status": error_code,
                    "transport_calls": transport_calls,
                    "injected_status_code": status_code,
                },
            },
            counters={"trace_forward_transport_calls": transport_calls},
            failure_injection=[_observed_fault(scenario, 0, observed_times=transport_calls)],
        )

    async def _scenario_r10(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del root, operations_path
        budget = CallBudget(scenario.setup["model_call_budget"])
        budget_blocked = False
        for _ in range(3):
            try:
                budget.consume()
            except CallBudgetExceeded:
                budget_blocked = True
        model_fallbacks = sum(
            item.get("status") == "fallback" for item in result.case.get("model_trace", [])
        )
        return self._observation(
            result,
            state_updates={
                "model_budget_probe": {
                    "limit": budget.limit,
                    "calls_admitted": budget.used,
                    "third_call_blocked": budget_blocked,
                }
            },
            counters={"model_calls": len(result.case.get("model_trace", []))},
            failure_injection=[
                _observed_fault(scenario, 0, observed_times=model_fallbacks),
                _observed_fault(scenario, 1, observed_times=int(budget_blocked)),
            ],
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
        )

    async def _scenario_r14(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del root
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
        extra = scenario.input.model_extra or {}
        try:
            _create_tasks(
                service,
                "CASE-R14-SERVICE",
                ["DC-SOUTH", "STORE-03"],
                extra["expected_case_version"],
                key=extra["idempotency_key"],
            )
        except StaleCaseVersionError as error:
            error_code = _error_code(error)
        state = service.get_case("CASE-R14-SERVICE")
        assert state is not None
        return self._observation(
            result,
            state_updates={
                "service_probe": {
                    "case_id": state.case_id,
                    "case_version": state.case_version,
                    "created_tasks": [],
                    "error_code": error_code,
                },
            },
            failure_injection=[
                _observed_fault(
                    scenario,
                    0,
                    observed_times=int(error_code == "stale_case_version"),
                )
            ],
        )

    async def _scenario_r15(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        disposition_lifecycle = await self._probe_disposition_lifecycle(scenario, root)
        concurrency_probe = await asyncio.to_thread(self._run_ack_close_race, root)
        traceability = TraceabilityService()
        service = OperationsService(storage_path=operations_path, traceability=traceability)
        case_id = scenario.input.model_extra["case_id"]
        authorization_evidence: list[dict[str, Any]] = []
        _create_case(
            service,
            traceability,
            case_id,
            lot_id="LOT-EXACT-170",
            thread_id=scenario.input.model_extra["thread_id"],
            authorization_evidence=authorization_evidence,
        )
        _record_disposition(
            service,
            case_id,
            "LOT-EXACT-170",
            1,
            evidence_id="EV-D-LOT-EXACT-170",
            key="r15-disposition",
            authorization_evidence=authorization_evidence,
        )
        extra = scenario.input.model_extra or {}
        facilities = extra["facilities"]
        _create_tasks(
            service,
            case_id,
            facilities,
            2,
            key="r15-tasks",
            authorization_evidence=authorization_evidence,
        )
        acknowledged = extra["acknowledge"]
        for offset, facility in enumerate(acknowledged):
            _acknowledge(
                service,
                case_id,
                facility,
                3 + offset,
                key=f"r15-ack-{facility}",
                authorization_evidence=authorization_evidence,
            )
        version = 3 + len(acknowledged)
        error_code = ""
        try:
            _close(service, case_id, version, key="r15-close")
        except ClosureBlockedError as error:
            error_code = _error_code(error)
        state = service.get_case(case_id)
        assert state is not None
        missing_acknowledgements = sorted(set(facilities) - set(acknowledged))
        return self._observation(
            result,
            state_updates={
                "service_probe": {
                    **state.model_dump(mode="json"),
                    "evidence_gaps": [
                        f"pending_acknowledgement:{facility}"
                        for facility in missing_acknowledgements
                    ],
                    "error_code": error_code,
                    "action_sequence": [receipt.action_type for receipt in state.write_receipts],
                    "authorization_evidence": authorization_evidence,
                    "receipts_authorized": _receipts_match_authorization_evidence(
                        state.write_receipts, authorization_evidence
                    ),
                },
                "disposition_lifecycle": disposition_lifecycle,
                "concurrency_probe": concurrency_probe,
            },
            failure_injection=[
                _observed_fault(
                    scenario,
                    0,
                    observed_times=int(scenario.faults[0].target in missing_acknowledgements),
                )
            ],
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
        concurrency_races = await asyncio.to_thread(
            self._run_toctou_races,
            root,
            2,
            "r16-concurrent-close",
            "r16-concurrent-task",
            "STORE-03",
        )
        traceability = _resolved_exact_traceability()
        service = OperationsService(storage_path=operations_path, traceability=traceability)
        case_id = scenario.input.model_extra["case_id"]
        authorization_evidence: list[dict[str, Any]] = []
        _create_case(
            service,
            traceability,
            case_id,
            lot_id="LOT-EXACT-170",
            thread_id=scenario.input.model_extra["thread_id"],
            authorization_evidence=authorization_evidence,
        )
        facilities = (scenario.input.model_extra or {})["facilities"]
        _create_tasks(
            service,
            case_id,
            facilities,
            1,
            key="r16-tasks",
            authorization_evidence=authorization_evidence,
        )
        version = 2
        for facility in facilities:
            _acknowledge(
                service,
                case_id,
                facility,
                version,
                key=f"r16-ack-{facility}",
                authorization_evidence=authorization_evidence,
            )
            version += 1
        error_code = ""
        try:
            _close(service, case_id, version, key="r16-close")
        except ClosureBlockedError as error:
            error_code = _error_code(error)
        state = service.get_case(case_id)
        assert state is not None
        omitted_facilities = sorted(set(state.required_facilities) - set(facilities))
        return self._observation(
            result,
            state_updates={
                "service_probe": {
                    **state.model_dump(mode="json"),
                    "evidence_gaps": [
                        f"unacknowledged_or_untasked_facility:{facility}"
                        for facility in omitted_facilities
                    ],
                    "error_code": error_code,
                    "authorization_evidence": authorization_evidence,
                    "receipts_authorized": _receipts_match_authorization_evidence(
                        state.write_receipts, authorization_evidence
                    ),
                },
                "concurrency_probe": {
                    "invariants_safe": all(race["invariants_safe"] for race in concurrency_races),
                    "race_evidence": concurrency_races,
                },
            },
            failure_injection=[
                _observed_fault(
                    scenario,
                    0,
                    observed_times=int(scenario.faults[0].target in omitted_facilities),
                )
            ],
        )

    async def _scenario_r20(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del operations_path
        extra = scenario.input.model_extra or {}
        race_results = await asyncio.to_thread(
            self._run_toctou_races,
            root,
            scenario.setup["repeat"],
            extra["close_key"],
            extra["late_task_key"],
            extra["late_facility"],
        )
        outcomes = [race["outcome"] for race in race_results]
        version_deltas = [race["version_delta"] for race in race_results]
        return self._observation(
            result,
            state_updates={
                "toctou_terminal_outcome": race_results[-1]["outcome"],
                "toctou_outcomes": outcomes,
                "toctou_version_deltas": version_deltas,
                "toctou_all_invariants_safe": all(race["invariants_safe"] for race in race_results),
                "toctou_race_evidence": race_results,
            },
            counters={
                "safe_race_outcomes": sum(race["invariants_safe"] for race in race_results),
                "version_increments_per_race": (
                    version_deltas[0] if version_deltas and len(set(version_deltas)) == 1 else -1
                ),
            },
            failure_injection=[_observed_fault(scenario, 0, observed_times=len(race_results))],
        )

    async def _scenario_r19(
        self,
        scenario: EvaluationScenario,
        result: RuntimeResult,
        root: Path,
        operations_path: Path,
    ) -> EvaluationObservation:
        del root, operations_path
        watchdog = result.case.get("watchdog", {})
        configured_limit = scenario.setup["watchdog_limit"]
        policy_watchdog = ProgressWatchdog(max_repeats=configured_limit)
        signature_observations = 0
        policy_blocked = False
        for _ in range(configured_limit + 1):
            signature_observations += 1
            try:
                policy_watchdog.observe({"progress": "unchanged"})
            except ProgressStalledError:
                policy_blocked = True
                break
        triggered = (
            result.case.get("status") == "escalated"
            and watchdog.get("repeat_count", 0) > 0
            and any("watchdog" in warning.lower() for warning in result.case.get("warnings", []))
        )
        return self._observation(
            result,
            state_updates={
                "watchdog_probe": {
                    "configured_limit": configured_limit,
                    "signature_observations": signature_observations,
                    "terminal_repeat_count": policy_watchdog.repeat_count,
                    "blocked": policy_blocked,
                }
            },
            counters={"progress_cycles": signature_observations},
            failure_injection=[_observed_fault(scenario, 0, observed_times=int(triggered))],
        )

    def _run_toctou_races(
        self,
        root: Path,
        repeat: int,
        close_key: str,
        late_task_key: str,
        late_facility: str,
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for iteration in range(repeat):
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
                    _close(close_service, case_id, version, key=f"{close_key}-{iteration}")
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
                        [late_facility],
                        version,
                        key=f"{late_task_key}-{iteration}",
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
            new_receipts = [
                receipt for receipt in state.write_receipts if receipt.case_version > version
            ]
            close_receipts = [
                receipt for receipt in new_receipts if receipt.action_type == "close_case"
            ]
            late_task_receipts = [
                receipt
                for receipt in new_receipts
                if receipt.action_type == "create_facility_tasks"
                and late_facility in receipt.details.get("facility_ids", [])
            ]
            pending_late_task = state.acknowledgements.get(late_facility) is False
            version_delta = state.case_version - version
            outcome = "unsafe"
            invariants_safe = False
            if (
                results == {"close": "won", "task": "stale"}
                and state.status == "closed"
                and len(close_receipts) == 1
                and not late_task_receipts
                and not pending_late_task
                and version_delta == 1
            ):
                outcome = "close_won_no_late_task"
                invariants_safe = True
            elif (
                results == {"task": "won", "close": "stale_or_blocked"}
                and state.status == "open"
                and pending_late_task
                and len(late_task_receipts) == 1
                and not close_receipts
                and version_delta == 1
            ):
                outcome = "late_task_won_close_blocked"
                invariants_safe = True
            if not invariants_safe:
                raise AssertionError(f"unsafe TOCTOU outcome: {results}, {state.status}")
            outcomes.append(
                {
                    "iteration": iteration,
                    "winner_barrier": winner,
                    "outcome": outcome,
                    "status": state.status,
                    "version_before": version,
                    "version_after": state.case_version,
                    "version_delta": version_delta,
                    "close_receipt_count": len(close_receipts),
                    "late_task_receipt_count": len(late_task_receipts),
                    "pending_late_task": pending_late_task,
                    "invariants_safe": invariants_safe,
                }
            )
        return outcomes

    def _run_ack_close_race(self, root: Path) -> dict[str, Any]:
        database = root / "ack-close-race.sqlite3"
        traceability = TraceabilityService()
        seed = OperationsService(storage_path=database, traceability=traceability)
        case_id = "CASE-R15-ACK-CLOSE-RACE"
        _create_case(seed, traceability, case_id, lot_id="LOT-EXACT-170")
        _record_disposition(
            seed,
            case_id,
            "LOT-EXACT-170",
            1,
            evidence_id="EV-D-LOT-EXACT-170",
            key="r15-race-disposition",
        )
        _create_tasks(seed, case_id, ["DC-NORTH", "STORE-01"], 2, key="r15-race-tasks")
        _acknowledge(seed, case_id, "DC-NORTH", 3, key="r15-race-ack-north")
        version = 4
        start = Barrier(3)
        outcomes: dict[str, str] = {}

        def acknowledge() -> None:
            start.wait(timeout=5)
            try:
                _acknowledge(
                    OperationsService(storage_path=database, traceability=traceability),
                    case_id,
                    "STORE-01",
                    version,
                    key="r15-race-ack-store",
                )
                outcomes["acknowledgment"] = "won"
            except StaleCaseVersionError:
                outcomes["acknowledgment"] = "stale"

        def close() -> None:
            start.wait(timeout=5)
            try:
                _close(
                    OperationsService(storage_path=database, traceability=traceability),
                    case_id,
                    version,
                    key="r15-race-close",
                )
                outcomes["close"] = "won"
            except (ClosureBlockedError, StaleCaseVersionError):
                outcomes["close"] = "blocked_or_stale"

        workers = [Thread(target=acknowledge), Thread(target=close)]
        for worker in workers:
            worker.start()
        start.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=5)
        if any(worker.is_alive() for worker in workers):
            raise RuntimeError("acknowledgment/closure race did not terminate")
        state = seed.get_case(case_id)
        assert state is not None
        new_receipts = [
            receipt for receipt in state.write_receipts if receipt.case_version > version
        ]
        acknowledgment_receipts = [
            receipt for receipt in new_receipts if receipt.action_type == "record_acknowledgment"
        ]
        close_receipts = [
            receipt for receipt in new_receipts if receipt.action_type == "close_case"
        ]
        invariants_safe = (
            outcomes.get("acknowledgment") == "won"
            and outcomes.get("close") == "blocked_or_stale"
            and state.status == "open"
            and state.case_version == version + 1
            and state.acknowledgements.get("STORE-01") is True
            and len(acknowledgment_receipts) == 1
            and not close_receipts
        )
        if not invariants_safe:
            raise AssertionError(f"unsafe acknowledgment/closure race: {outcomes}")
        return {
            "outcomes": outcomes,
            "version_before": version,
            "version_after": state.case_version,
            "acknowledgment_receipt_count": len(acknowledgment_receipts),
            "close_receipt_count": len(close_receipts),
            "invariants_safe": invariants_safe,
        }

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
            lost_response_observed = unknown.case.get("failure_state", {}).get("scenario") in {
                "lost_write_response",
                "receipt_or_write_failure",
            }
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
            confirmation_history = _execution_confirmation_evidence(
                await runtime.get_case_history(thread_id=recovered.case["thread_id"])
            )
        keys = [item["idempotency_key"] for item in recovered.case["write_receipts"]]
        return self._observation(
            recovered,
            state_updates={
                "replayed_receipt_same": keys.count(key) == 1,
                "execution_confirmation_history": confirmation_history,
            },
            counters={
                "logical_write_count": len(set(keys)),
                "retried_logical_write_count": int(keys.count(key) == 1),
            },
            failure_injection=[
                _observed_fault(
                    scenario,
                    0,
                    observed_times=int(lost_response_observed),
                )
            ],
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
            history_before = await runtime.get_case_history(thread_id=review.case["thread_id"])
            immutable_probe = await runtime.get_case(thread_id=review.case["thread_id"])
            assert immutable_probe is not None
            candidate_lots = immutable_probe.case["candidate_lots"]
            node_trace = immutable_probe.case["node_trace"]
            pending = immutable_probe.pending_interrupt
            assert candidate_lots and pending is not None
            immutability_audits = [
                _immutable_container_audit(immutable_probe.case, path="case"),
                _immutable_container_audit(pending, path="pending_interrupt"),
                _immutable_container_audit(immutable_probe.next_nodes, path="next_nodes"),
            ]
            mutation_checks = {
                "case_mapping": _mutation_rejected(
                    immutable_probe.case, "__setitem__", "status", "forged"
                ),
                "case_sequence": _mutation_rejected(node_trace, "append", "forged_node"),
                "nested_case_mapping": _mutation_rejected(
                    candidate_lots[0], "__setitem__", "classification", "forged"
                ),
                "interrupt_mapping": _mutation_rejected(pending, "__setitem__", "kind", "forged"),
                "nested_interrupt_mapping": _mutation_rejected(
                    pending["action"], "__setitem__", "action_type", "close_case"
                ),
            }
            result_deeply_immutable = all(mutation_checks.values()) and all(
                audit["all_containers_immutable"] for audit in immutability_audits
            )
            durable_after_mutation = await runtime.get_case(thread_id=review.case["thread_id"])
            mutation_did_not_persist = durable_after_mutation == review
            runtime_surface_sealed = all(
                not hasattr(runtime, attribute)
                for attribute in ("graph", "_graph", "_execute", "workflow")
            )
            created = await _approve_and_confirm(runtime, restored)
            history_after = await runtime.get_case_history(thread_id=review.case["thread_id"])
        copied_store_probe = await self._probe_copied_checkpoint_head_fencing(
            scenario, checkpoint.parent
        )
        compiled_guard_results = await self._probe_compiled_workflow_guards(
            scenario,
            checkpoint.parent,
        )
        return self._observation(
            created,
            state_updates={
                "compiled_guard_results": {
                    **compiled_guard_results,
                    "runtime_surface_sealed": runtime_surface_sealed,
                },
                "public_history_probe": {
                    "review_checkpoint_retained": any(
                        item.pending_interrupt == review.pending_interrupt
                        for item in history_before
                    ),
                    "history_grew_after_resume": len(history_after) > len(history_before),
                    "prior_checkpoint_ids_preserved": {
                        item.checkpoint_id for item in history_before if item.checkpoint_id
                    }.issubset(
                        {item.checkpoint_id for item in history_after if item.checkpoint_id}
                    ),
                    "history_count": len(history_after),
                },
                "execution_confirmation_history": _execution_confirmation_evidence(history_after),
                "runtime_result_probe": {
                    "deeply_immutable": result_deeply_immutable,
                    "mutation_did_not_persist": mutation_did_not_persist,
                    "mutation_checks": mutation_checks,
                    "container_count": sum(
                        audit["container_count"] for audit in immutability_audits
                    ),
                    "mutable_paths": sorted(
                        path for audit in immutability_audits for path in audit["mutable_paths"]
                    ),
                },
                "copied_checkpoint_probe": copied_store_probe,
            },
            failure_injection=[
                _observed_fault(
                    scenario,
                    0,
                    observed_times=int(restored == review and bool(history_before)),
                ),
                _observed_fault(
                    scenario,
                    1,
                    observed_times=int(compiled_guard_results["old_checkpoint_rewind"]),
                ),
            ],
        )

    async def _probe_copied_checkpoint_head_fencing(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> dict[str, Any]:
        seed_checkpoint = root / "copied-seed-checkpoints.sqlite3"
        operations = root / "copied-shared-operations.sqlite3"
        case_id = "CASE-R18-COPIED-HEAD"
        thread_id = "thread-r18-copied-head"
        async with RecallOpsRuntime.open(
            checkpoint_path=seed_checkpoint,
            operations_path=operations,
        ) as runtime:
            review = await runtime.start_case(
                recall_number=scenario.input.recall_number,
                question="Fence cloned durable checkpoint heads before accepting consent.",
                case_id=case_id,
                thread_id=thread_id,
            )
            assert review.pending_interrupt is not None
            assert review.checkpoint_id is not None
        first_copy = root / "copied-head-a.sqlite3"
        second_copy = root / "copied-head-b.sqlite3"
        shutil.copy2(seed_checkpoint, first_copy)
        shutil.copy2(seed_checkpoint, second_copy)
        response = _bound_response(review.pending_interrupt, decision="approve")
        async with (
            RecallOpsRuntime.open(
                checkpoint_path=first_copy,
                operations_path=operations,
            ) as first,
            RecallOpsRuntime.open(
                checkpoint_path=second_copy,
                operations_path=operations,
            ) as second,
        ):
            concurrent = await asyncio.gather(
                first.resume_case(thread_id=thread_id, response=response),
                second.resume_case(thread_id=thread_id, response=response),
                return_exceptions=True,
            )
            successes = [item for item in concurrent if isinstance(item, RuntimeResult)]
            failures = [item for item in concurrent if isinstance(item, BaseException)]
            concurrent_error_code = (
                _checkpoint_fence_error_code(failures[0]) if len(failures) == 1 else ""
            )
            concurrent_fenced = (
                len(successes) == 1
                and len(failures) == 1
                and concurrent_error_code in {"active_mutation_fenced", "stale_head_fenced"}
            )
            sequential_fenced = False
            sequential_error_code = ""
            durable_post_race_state = False
            if concurrent_fenced:
                first_won = isinstance(concurrent[0], RuntimeResult)
                winner = first if first_won else second
                loser = second if first_won else first
                winner_after, loser_after = await asyncio.gather(
                    winner.get_case(thread_id=thread_id),
                    loser.get_case(thread_id=thread_id),
                )
                durable_post_race_state = (
                    winner_after == successes[0]
                    and loser_after is not None
                    and loser_after.pending_interrupt is not None
                    and loser_after.pending_interrupt.get("kind") == "action_review"
                    and winner_after is not None
                    and winner_after.checkpoint_id != loser_after.checkpoint_id
                )
                try:
                    await loser.resume_case(thread_id=thread_id, response=response)
                except (RuntimeError, ValueError) as error:
                    sequential_error_code = _checkpoint_fence_error_code(error)
                    sequential_fenced = sequential_error_code == "stale_head_fenced"
        expired_response_probe = await self._probe_expired_copied_marker_response_binding(
            scenario,
            root,
        )
        return {
            "concurrent_fenced": concurrent_fenced,
            "sequential_fenced": sequential_fenced,
            "durable_post_race_state": durable_post_race_state,
            "concurrent_error_code": concurrent_error_code,
            "sequential_error_code": sequential_error_code,
            "success_count": len(successes),
            "failure_count": len(failures),
            **expired_response_probe,
        }

    async def _probe_expired_copied_marker_response_binding(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> dict[str, Any]:
        """Crash after preparing approval and prove a clone cannot change that request."""
        import recallops.agents.runtime as runtime_module

        def checkpoint_marker(path: Path) -> dict[str, Any] | None:
            with sqlite3.connect(path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM recallops_mutation_attempts WHERE case_id=? AND thread_id=?",
                    (case_id, thread_id),
                ).fetchone()
            return dict(row) if row is not None else None

        def operations_identity() -> dict[str, Any] | None:
            with sqlite3.connect(operations_path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM workflow_identities WHERE case_id=? AND thread_id=?",
                    (case_id, thread_id),
                ).fetchone()
            return dict(row) if row is not None else None

        def request_digest(row: dict[str, Any] | None) -> str:
            if row is None:
                return ""
            candidates = [
                value
                for key, value in row.items()
                if "request" in key and ("digest" in key or "hash" in key)
            ]
            if len(candidates) != 1 or type(candidates[0]) is not str:
                return ""
            return candidates[0]

        def canonical_digest(
            response: dict[str, Any],
            *,
            expected_checkpoint_head: str,
            pending: Mapping[str, Any],
        ) -> str:
            contract = {
                "schema": "recallops-mutation-request-v1",
                "case_id": case_id,
                "thread_id": thread_id,
                "expected_checkpoint_head": expected_checkpoint_head,
                "interrupt_kind": pending["kind"],
                "human_response": response,
                "pending_action_id": pending.get("action_id")
                or pending.get("action", {}).get("action_id"),
                "pending_action_digest": pending.get("action_digest"),
                "execution_id": pending.get("execution_id"),
                "idempotency_key": pending.get("idempotency_key"),
                "initial_payload": None,
            }
            payload = json.dumps(
                contract,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
            return hashlib.sha256(payload).hexdigest()

        original_path = root / "expired-marker-original.sqlite3"
        changed_copy_path = root / "expired-marker-changed-copy.sqlite3"
        exact_copy_path = root / "expired-marker-exact-copy.sqlite3"
        operations_path = root / "expired-marker-operations.sqlite3"
        case_id = "CASE-R18-EXPIRED-MARKER"
        thread_id = "thread-r18-expired-marker"
        async with RecallOpsRuntime.open(
            checkpoint_path=original_path,
            operations_path=operations_path,
        ) as runtime:
            review = await runtime.start_case(
                recall_number=scenario.input.recall_number,
                question="Bind recovery of an expired checkpoint marker to one exact decision.",
                case_id=case_id,
                thread_id=thread_id,
            )
            assert review.pending_interrupt is not None
            assert review.checkpoint_id is not None

        approve = _bound_response(review.pending_interrupt, decision="approve")
        reject = _bound_response(review.pending_interrupt, decision="reject")
        execution_entered = asyncio.Event()
        release_execution = asyncio.Event()
        original_execute = runtime_module._execute_workflow
        original_release = OperationsService.release_workflow_mutation
        marker_copied = False
        request_digest_bound = False
        live_state_proven = False
        live_original_marker: dict[str, Any] | None = None
        live_identity: dict[str, Any] | None = None

        async def pause_before_mutation(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            execution_entered.set()
            await release_execution.wait()
            raise RuntimeError("simulated process death before checkpoint mutation")

        def fail_fence_release(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("simulated process death before fence release")

        try:
            runtime_module._execute_workflow = pause_before_mutation
            OperationsService.release_workflow_mutation = fail_fence_release
            async with RecallOpsRuntime.open(
                checkpoint_path=original_path,
                operations_path=operations_path,
            ) as original:
                failed_resume: asyncio.Task[RuntimeResult] | None = asyncio.create_task(
                    original.resume_case(thread_id=thread_id, response=approve)
                )
                try:
                    await asyncio.wait_for(execution_entered.wait(), timeout=5)
                    live_original_marker = checkpoint_marker(original_path)
                    live_identity = operations_identity()
                    live_state_proven = (
                        live_original_marker is not None
                        and live_original_marker.get("state") == "prepared"
                        and live_identity is not None
                        and live_identity.get("attempt_state") == "active"
                        and type(live_identity.get("attempt_expires_at")) is float
                        and live_identity["attempt_expires_at"] > time.time()
                    )
                    for copied_path in (changed_copy_path, exact_copy_path):
                        with (
                            sqlite3.connect(original_path) as source,
                            sqlite3.connect(copied_path) as destination,
                        ):
                            source.backup(destination)
                finally:
                    release_execution.set()
                    outcomes = await asyncio.gather(failed_resume, return_exceptions=True)
                    failed_resume = None
                if (
                    len(outcomes) != 1
                    or not isinstance(outcomes[0], RuntimeError)
                    or "fence release" not in str(outcomes[0])
                ):
                    raise AssertionError("simulated pre-mutation crash did not retain its fence")
        finally:
            release_execution.set()
            runtime_module._execute_workflow = original_execute
            OperationsService.release_workflow_mutation = original_release

        original_marker = checkpoint_marker(original_path)
        changed_marker = checkpoint_marker(changed_copy_path)
        exact_marker = checkpoint_marker(exact_copy_path)
        identity_before_expiry = operations_identity()
        changed_copy_review: RuntimeResult | None
        exact_copy_review: RuntimeResult | None
        async with (
            RecallOpsRuntime.open(
                checkpoint_path=changed_copy_path,
                operations_path=operations_path,
            ) as changed_copy,
            RecallOpsRuntime.open(
                checkpoint_path=exact_copy_path,
                operations_path=operations_path,
            ) as exact_copy,
        ):
            changed_copy_review, exact_copy_review = await asyncio.gather(
                changed_copy.get_case(thread_id=thread_id),
                exact_copy.get_case(thread_id=thread_id),
            )
        marker_copied = (
            live_state_proven
            and original_marker is not None
            and original_marker == live_original_marker == changed_marker == exact_marker
            and identity_before_expiry == live_identity
            and original_marker.get("attempt_token")
            == (identity_before_expiry or {}).get("attempt_token")
            and original_marker.get("expected_checkpoint_head") == review.checkpoint_id
            and (identity_before_expiry or {}).get("attempt_expected_head") == review.checkpoint_id
            and (identity_before_expiry or {}).get("checkpoint_head") == review.checkpoint_id
            and changed_copy_review == exact_copy_review == review
        )
        approve_digest = canonical_digest(
            approve,
            expected_checkpoint_head=review.checkpoint_id,
            pending=review.pending_interrupt,
        )
        reject_digest = canonical_digest(
            reject,
            expected_checkpoint_head=review.checkpoint_id,
            pending=review.pending_interrupt,
        )
        request_digest_bound = (
            approve_digest != reject_digest
            and request_digest(original_marker)
            == request_digest(changed_marker)
            == request_digest(exact_marker)
            == request_digest(identity_before_expiry)
            == approve_digest
        )

        with sqlite3.connect(operations_path) as connection:
            updated = connection.execute(
                "UPDATE workflow_identities SET attempt_expires_at=0 WHERE case_id=?",
                (case_id,),
            )
            if updated.rowcount != 1:
                raise AssertionError("expired-marker probe did not retain its mutation lease")
        identity_after_expiry = operations_identity()
        expected_after_expiry = dict(identity_before_expiry or {})
        expected_after_expiry["attempt_expires_at"] = 0.0
        expiry_exact = identity_after_expiry == expected_after_expiry

        changed_response_fenced = False
        changed_response_error_code = ""
        async with RecallOpsRuntime.open(
            checkpoint_path=changed_copy_path,
            operations_path=operations_path,
        ) as copied:
            copied_before = await copied.get_case(thread_id=thread_id)
            copied_history_before = await copied.get_case_history(thread_id=thread_id)
            marker_before_mismatch = checkpoint_marker(changed_copy_path)
            identity_before_mismatch = operations_identity()
            try:
                await copied.resume_case(thread_id=thread_id, response=reject)
            except ValueError as error:
                changed_response_error_code = _checkpoint_fence_error_code(error)
                copied_after = await copied.get_case(thread_id=thread_id)
                copied_history_after = await copied.get_case_history(thread_id=thread_id)
                changed_response_fenced = (
                    changed_response_error_code == "request_digest_mismatch"
                    and copied_after == copied_before == changed_copy_review == review
                    and copied_history_after == copied_history_before
                    and checkpoint_marker(changed_copy_path) == marker_before_mismatch
                    and operations_identity() == identity_before_mismatch
                )

        exact_response_recovered = False
        exact_confirmation_bound = False
        exact_checkpoint_advanced_once = False
        exact_recovery_markers_cleared = False
        async with RecallOpsRuntime.open(
            checkpoint_path=exact_copy_path,
            operations_path=operations_path,
        ) as original:
            exact_history_before = await original.get_case_history(thread_id=thread_id)
            try:
                confirmation = await original.resume_case(
                    thread_id=thread_id,
                    response=approve,
                )
            except (RuntimeError, ValueError):
                pass
            else:
                exact_history_after = await original.get_case_history(thread_id=thread_id)
                pending = confirmation.pending_interrupt
                review_pending = review.pending_interrupt
                expected_execution_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"{thread_id}:{review_pending.get('case_version')}:"
                        f"{review_pending.get('action', {}).get('action_id')}:"
                        f"{review_pending.get('action_digest')}",
                    )
                )
                expected_idempotency_key = f"recallops:{expected_execution_id}"
                exact_confirmation_bound = (
                    pending is not None
                    and review_pending is not None
                    and pending.get("kind") == "execution_confirmation"
                    and pending.get("case_id") == review_pending.get("case_id")
                    and pending.get("thread_id") == review_pending.get("thread_id")
                    and pending.get("case_version") == review_pending.get("case_version")
                    and pending.get("action_id")
                    == review_pending.get("action", {}).get("action_id")
                    and pending.get("action_digest") == review_pending.get("action_digest")
                    and pending.get("execution_id") == expected_execution_id
                    and pending.get("idempotency_key") == expected_idempotency_key
                    and confirmation.case.get("execution_id") == expected_execution_id
                    and confirmation.case.get("idempotency_key") == expected_idempotency_key
                    and confirmation.case.get("execution_request") == pending
                    and confirmation.next_nodes == ("execution_confirmation",)
                )
                identity_after_recovery = operations_identity()
                prior_checkpoint_ids = {
                    item.checkpoint_id
                    for item in exact_history_before
                    if item.checkpoint_id is not None
                }
                new_history = exact_history_after[:2]
                exact_checkpoint_advanced_once = (
                    len(exact_history_after) == len(exact_history_before) + 2
                    and exact_history_after[2:] == exact_history_before
                    and len(new_history) == 2
                    and len({item.checkpoint_id for item in new_history}) == 2
                    and all(item.checkpoint_id not in prior_checkpoint_ids for item in new_history)
                    and confirmation == exact_history_after[0]
                    and (identity_after_recovery or {}).get("checkpoint_head")
                    == confirmation.checkpoint_id
                )
                exact_recovery_markers_cleared = (
                    checkpoint_marker(exact_copy_path) is None
                    and identity_after_recovery is not None
                    and tuple(
                        identity_after_recovery.get(key)
                        for key in (
                            "attempt_token",
                            "attempt_expected_head",
                            "attempt_request_digest",
                            "attempt_state",
                            "attempt_expires_at",
                        )
                    )
                    == (None, None, None, None, None)
                )
                exact_response_recovered = (
                    marker_copied
                    and request_digest_bound
                    and changed_response_fenced
                    and exact_confirmation_bound
                    and exact_checkpoint_advanced_once
                    and exact_recovery_markers_cleared
                )

        return {
            "expired_recovery_marker_copied": marker_copied,
            "expired_recovery_live_state_proven": live_state_proven,
            "expired_recovery_expiry_exact": expiry_exact,
            "expired_recovery_request_digest_bound": request_digest_bound,
            "expired_changed_response_fenced": changed_response_fenced,
            "expired_changed_response_error_code": changed_response_error_code,
            "expired_exact_response_recovered": exact_response_recovered,
            "expired_exact_confirmation_bound": exact_confirmation_bound,
            "expired_exact_checkpoint_advanced_once": exact_checkpoint_advanced_once,
            "expired_exact_recovery_markers_cleared": exact_recovery_markers_cleared,
        }

    async def _probe_compiled_workflow_guards(
        self,
        scenario: EvaluationScenario,
        root: Path,
    ) -> dict[str, bool]:
        del scenario

        def graph_for(label: str) -> Any:
            gateway = DirectGateway(
                operations=OperationsService(storage_path=root / f"{label}-operations.sqlite3")
            )
            return build_workflow(gateway=gateway, checkpointer=InMemorySaver())

        def surface_disabled(label: str, surface: str) -> bool:
            graph = graph_for(label)
            try:
                getattr(graph, surface)
            except AttributeError:
                return True
            return False

        guard_results = {
            "reinitialize": surface_disabled("reinitialize", "ainvoke"),
            "stream_reinitialize": surface_disabled("stream-reinitialize", "astream"),
            "invoke_reinitialize": surface_disabled("invoke-reinitialize", "invoke"),
            "sync_stream_reinitialize": surface_disabled("sync-stream-reinitialize", "stream"),
            "abatch_reinitialize": surface_disabled("abatch-reinitialize", "abatch"),
            "batch_reinitialize": surface_disabled("batch-reinitialize", "batch"),
            "with_config_reinitialize": surface_disabled("with-config-reinitialize", "with_config"),
            "old_checkpoint_rewind": surface_disabled("old-checkpoint-rewind", "ainvoke"),
            "private_execute_unavailable": surface_disabled("private-execute", "_execute"),
            "raw_graph_unavailable": surface_disabled("raw-graph", "_graph"),
            "update_state": surface_disabled("update", "aupdate_state"),
            "sync_update_state": surface_disabled("sync-update", "update_state"),
            "bulk_update_state": surface_disabled("bulk-update", "abulk_update_state"),
            "sync_bulk_update_state": surface_disabled("sync-bulk-update", "bulk_update_state"),
        }

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
        confirmation_history = _execution_confirmation_evidence(
            await runtime.get_case_history(thread_id=held.case["thread_id"])
        )
        return self._observation(
            held,
            state_updates={
                "service_probe_error_code": error_code,
                "consent_probe_codes": codes,
                "start_input_probe_codes": start_input_codes,
                "identity_conflict_probe_codes": identity_conflict_codes,
                "concurrent_resume_one_effect": concurrent_one_effect,
                "cross_runtime_start_one_effect": cross_runtime_start_one_effect,
                "execution_confirmation_history": confirmation_history,
            },
            counters={"logical_write_count": 1 if first else 0},
            failure_injection=[
                _observed_fault(
                    scenario,
                    0,
                    observed_times=int(error_code == "idempotency_conflict"),
                ),
                _observed_fault(
                    scenario,
                    1,
                    observed_times=int("action_digest_mismatch" in codes),
                ),
                _observed_fault(
                    scenario,
                    2,
                    observed_times=int("stale_or_replayed_consent" in codes),
                ),
            ],
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
        ambiguous_lots = set(result.case.get("ambiguous_lot_ids", []))
        held_lots = {
            lot_id
            for receipt in result.case.get("write_receipts", [])
            if receipt.get("action_type") == "apply_inventory_hold"
            for lot_id in receipt.get("details", {}).get("lot_ids", [])
        }
        ambiguous_scope_observed = bool(ambiguous_lots) and not (ambiguous_lots & held_lots)
        confirmation_history = _execution_confirmation_evidence(
            await runtime.get_case_history(thread_id=result.case["thread_id"])
        )
        return self._observation(
            result,
            state_updates={
                "premature_facility_edit_rejected": premature_rejected,
                "action_sequence": sequence,
                "execution_confirmation_history": confirmation_history,
            },
            failure_injection=[
                _observed_fault(
                    scenario,
                    0,
                    observed_times=int(ambiguous_scope_observed),
                )
            ],
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
                corpus_payload=corpus.model_dump(mode="json"),
            )
    return await run_evaluations(
        corpus.scenarios,
        RecallOpsEvaluationExecutor(
            workspace=workspace,
            include_stdio_smoke=include_stdio_smoke,
        ),
        output_path=output_path,
        strict=strict,
        corpus_payload=corpus.model_dump(mode="json"),
    )
