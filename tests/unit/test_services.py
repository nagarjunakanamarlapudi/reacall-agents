import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

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
    StaleCaseVersionError,
    _workflow_authorization_broker,
)
from recallops.services.operations import OperationsService as RawOperationsService
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService

TRACEABILITY = TraceabilityService()


def _predicate() -> RecallPredicate:
    return RecallPredicate(
        product_terms=["eggs"],
        upcs=["011110609038"],
        plant_codes=["P-1950", "0840962"],
        julian_start=157,
        julian_end=184,
        geography=["Texas"],
        hazard="Possible Salmonella Enteritidis",
    )


def _case_input(*, unaccounted: int = 0) -> dict:
    traceability = TraceabilityService()
    lot_id = "LOT-EXACT-170" if unaccounted else "LOT-PROBABLE-160"
    events = traceability.trace_forward(lot_id)
    return {
        "recall_number": "H-1230-2026",
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
        "evidence_gaps": (
            [f"{lot_id}: {reconciliation.unaccounted} unaccounted units"]
            if (reconciliation := traceability.reconcile_units(lot_id)).unaccounted
            else []
        ),
    }


def _bound_approval(action: ProposedAction, *, decision: str = "approve") -> ApprovalDecision:
    return ApprovalDecision(
        decision=decision,
        actor="food-safety-manager",
        justification="reviewed the exact action payload",
        approved_at=datetime(2026, 8, 30, tzinfo=UTC),
        approved_case_version=action.expected_case_version,
        approved_case_id=action.case_id,
        action_ids=[action.action_id],
        action_bindings=[
            ApprovalBinding(
                action_id=action.action_id,
                action_digest=proposed_action_digest(action),
            )
        ],
    )


def _create_action(case_id: str) -> ProposedAction:
    inputs = _case_input()
    return ProposedAction(
        action_id=f"{case_id}-create",
        action_type="create_case",
        case_id=case_id,
        target_ids=inputs["confirmed_lot_ids"],
        rationale="Open the simulated recall case from reviewed evidence.",
        evidence_ids=inputs["trace_event_ids"],
        evidence_by_target={
            lot_id: inputs["trace_event_ids"] for lot_id in inputs["confirmed_lot_ids"]
        },
        expected_case_version=0,
    )


def _review(
    action_type: str,
    case_id: str,
    version: int,
    target_ids: list[str],
    *,
    evidence_ids: list[str] | None = None,
    decision: str = "approve",
) -> dict:
    if evidence_ids is not None:
        action_evidence = evidence_ids
    else:
        lot_by_facility = {
            "DC-SOUTH": "LOT-PROBABLE-160",
            "STORE-03": "LOT-PROBABLE-160",
            "DC-NORTH": "LOT-EXACT-170",
            "STORE-01": "LOT-EXACT-170",
            "STORE-02": "LOT-EXACT-170",
        }
        action_evidence = sorted(
            {
                identifier
                for target_id in target_ids
                for identifier in (
                    {event["event_id"] for event in TRACEABILITY.trace_forward(target_id)}
                    | {
                        position["position_id"]
                        for position in TRACEABILITY.get_inventory(target_id)
                    }
                    if target_id.startswith("LOT-")
                    else {
                        event["event_id"]
                        for event in TRACEABILITY.trace_forward(lot_by_facility[target_id])
                    }
                    | {
                        position["position_id"]
                        for position in TRACEABILITY.get_inventory(lot_by_facility[target_id])
                    }
                )
            }
        )
    action = ProposedAction(
        action_id=f"{case_id}-{action_type}-{version}",
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids,
        rationale=f"Reviewed {action_type} against the case evidence.",
        evidence_ids=action_evidence,
        evidence_by_target={target_id: action_evidence for target_id in target_ids},
        expected_case_version=version,
    )
    return {"proposed_action": action, "approval": _bound_approval(action, decision=decision)}


def _trusted_execute(
    service: RawOperationsService,
    method_name: str,
    /,
    **kwargs: Any,
):
    operation = getattr(RawOperationsService, method_name).__get__(service, RawOperationsService)
    case_id = kwargs["case_id"]
    expected = kwargs["expected_case_version"]
    key = kwargs["idempotency_key"]
    if service.get_receipt(key) is not None:
        return operation(**kwargs)
    state = service.get_case(case_id)
    thread_id = kwargs.get("thread_id") or (state.thread_id if state else case_id)
    with sqlite3.connect(service.storage_path) as connection:
        owner_row = connection.execute(
            "SELECT owner_token FROM workflow_identities WHERE case_id=?", (case_id,)
        ).fetchone()
    owner = owner_row[0] if owner_row and owner_row[0] else str(uuid4())
    broker = _workflow_authorization_broker(service, owner)
    if service.get_thread_for_case(case_id) is None:
        broker.reserve_workflow_identity(case_id, thread_id)
    with sqlite3.connect(service.storage_path) as connection:
        head = connection.execute(
            "SELECT checkpoint_head FROM workflow_identities WHERE case_id=?", (case_id,)
        ).fetchone()[0]
    request_digest = hashlib.sha256(
        f"{case_id}:{thread_id}:{head}:{method_name}:{expected}:{key}".encode()
    ).hexdigest()
    attempt = str(uuid4())
    execution_id = f"SERVICE-TEST-EXECUTION:{method_name}:{expected}:{key}"
    execution_request_digest = hashlib.sha256(
        f"service-test:{case_id}:{method_name}:{expected}:{key}".encode()
    ).hexdigest()
    broker.claim_workflow_mutation(
        case_id,
        thread_id,
        head,
        attempt,
        request_digest,
        execution_id=execution_id,
        execution_request_digest=execution_request_digest,
    )
    fields = {
        "create_case": (
            "recall_number",
            "question",
            "thread_id",
            "confirmed_lot_ids",
            "trace_event_ids",
            "required_facilities",
            "reconciliation",
            "evidence_gaps",
        ),
        "apply_inventory_hold": ("lot_ids",),
        "create_facility_tasks": ("facility_ids",),
        "record_acknowledgment": ("facility_id",),
        "record_disposition": ("lot_id", "disposition", "evidence_id"),
        "close_case": (),
    }[method_name]
    details: dict[str, Any] = {}
    for field in fields:
        if field == "question":
            details[field] = kwargs.get(field, "")
        elif field == "thread_id":
            details[field] = thread_id
        elif field == "reconciliation":
            details[field] = [item.model_dump(mode="json") for item in kwargs[field]]
        else:
            details[field] = kwargs[field]
    action = kwargs["proposed_action"]
    approval = kwargs["approval"]
    try:
        grant = broker.issue_workflow_execution_grant(
            case_id=case_id,
            thread_id=thread_id,
            proposed_action=action,
            approval=approval,
            expected_case_version=expected,
            idempotency_key=key,
            execution_id=execution_id,
            execution_request_digest=execution_request_digest,
            details=details,
            target_ids=list(action.target_ids),
            evidence_ids=(
                list(kwargs["trace_event_ids"])
                if method_name == "create_case"
                else [kwargs["evidence_id"]]
                if method_name == "record_disposition"
                else None
            ),
        )
        receipt = operation(**kwargs, execution_grant=grant)
    except BaseException:
        broker.release_workflow_mutation(case_id, thread_id, head, attempt, request_digest)
        raise
    broker.advance_workflow_mutation(
        case_id,
        thread_id,
        head,
        f"SERVICE-TEST-CHECKPOINT:{case_id}:{receipt.case_version}:{receipt.receipt_id}",
        attempt,
        request_digest,
    )
    return receipt


class OperationsService(RawOperationsService):
    """Drive service contract tests through the production workflow-grant boundary."""

    def _write(self, method_name: str, kwargs: dict[str, Any]):
        return _trusted_execute(self, method_name, **kwargs)

    def create_case(self, **kwargs: Any):
        return self._write("create_case", kwargs)

    def apply_inventory_hold(self, **kwargs: Any):
        return self._write("apply_inventory_hold", kwargs)

    def create_facility_tasks(self, **kwargs: Any):
        return self._write("create_facility_tasks", kwargs)

    def record_acknowledgment(self, **kwargs: Any):
        return self._write("record_acknowledgment", kwargs)

    def record_disposition(self, **kwargs: Any):
        return self._write("record_disposition", kwargs)

    def close_case(self, **kwargs: Any):
        return self._write("close_case", kwargs)


@pytest.mark.parametrize(
    "change",
    [
        {"case_id": "CASE-OTHER"},
        {"action_type": "apply_inventory_hold"},
        {
            "target_ids": ["LOT-EXACT-170"],
        },
        {
            "evidence_ids": ["EV-UNREVIEWED"],
            "evidence_by_target": {"LOT-PROBABLE-160": ["EV-UNREVIEWED"]},
        },
        {"rationale": "Changed after human review."},
    ],
)
def test_operations_service_rejects_cross_case_or_mutated_reviewed_action(
    tmp_path: Path, change: dict
) -> None:
    case_id = "CASE-BOUND-ACTION"
    reviewed = _create_action(case_id)
    if "target_ids" in change:
        change = {
            **change,
            "evidence_by_target": {
                change["target_ids"][0]: list(reviewed.evidence_ids),
            },
        }
    changed = ProposedAction.model_validate({**reviewed.model_dump(), **change})
    service = OperationsService(storage_path=tmp_path / f"{next(iter(change))}.sqlite3")

    with pytest.raises(ApprovalRequiredError, match="approval binding|reviewed action"):
        service.create_case(
            case_id=case_id,
            **_case_input(),
            proposed_action=changed,
            approval=_bound_approval(reviewed),
            expected_case_version=0,
            idempotency_key=f"create-{next(iter(change))}",
        )


@pytest.mark.parametrize(
    "invalid_version",
    [True, 1.0, "1", float("nan"), float("inf"), -1],
)
def test_operations_service_requires_strict_versions_on_create_and_mutation(
    tmp_path: Path,
    invalid_version: object,
) -> None:
    case_id = "CASE-STRICT-VERSION"
    case_input = _case_input()
    create_review = _review(
        "create_case",
        case_id,
        0,
        case_input["confirmed_lot_ids"],
        evidence_ids=case_input["trace_event_ids"],
    )
    service = OperationsService(storage_path=tmp_path / f"strict-{type(invalid_version).__name__}")

    with pytest.raises(ValueError, match="strict nonnegative integer"):
        service.create_case(
            case_id=case_id,
            **case_input,
            **create_review,
            expected_case_version=invalid_version,
            idempotency_key="invalid-create",
        )

    service.create_case(
        case_id=case_id,
        **case_input,
        **create_review,
        expected_case_version=0,
        idempotency_key="valid-create",
    )
    with pytest.raises(ValueError, match="strict nonnegative integer"):
        service.apply_inventory_hold(
            case_id=case_id,
            lot_ids=case_input["confirmed_lot_ids"],
            **_review(
                "apply_inventory_hold",
                case_id,
                1,
                case_input["confirmed_lot_ids"],
            ),
            expected_case_version=invalid_version,
            idempotency_key="invalid-mutation",
        )


def test_operations_service_revalidates_model_copy_version_bypasses(tmp_path: Path) -> None:
    case_id = "CASE-COPIED-VERSION"
    case_input = _case_input()
    reviewed = _create_action(case_id)
    copied_action = reviewed.model_copy(update={"expected_case_version": False})
    copied_approval = _bound_approval(reviewed).model_copy(
        update={
            "approved_case_version": False,
            "action_bindings": (
                ApprovalBinding(
                    action_id=copied_action.action_id,
                    action_digest=proposed_action_digest(copied_action),
                ),
            ),
        }
    )
    service = OperationsService(storage_path=tmp_path / "copied-version.sqlite3")

    with pytest.raises(ValueError, match="approved_case_version"):
        service.create_case(
            case_id=case_id,
            **case_input,
            proposed_action=copied_action,
            approval=copied_approval,
            expected_case_version=0,
            idempotency_key="copied-version",
        )


def test_operations_service_rejects_post_approval_target_evidence_reallocation(
    tmp_path: Path,
) -> None:
    """Catches target allocation living outside the canonical human-reviewed action digest."""
    case_id = "CASE-TARGET-EVIDENCE"
    service = OperationsService(storage_path=tmp_path / "target-evidence.sqlite3")
    case_input = _case_input()
    create_review = _review(
        "create_case",
        case_id,
        0,
        case_input["confirmed_lot_ids"],
        evidence_ids=case_input["trace_event_ids"],
    )
    service.create_case(
        case_id=case_id,
        **case_input,
        **create_review,
        expected_case_version=0,
        idempotency_key="target-evidence-create",
    )
    reviewed = ProposedAction(
        action_id="target-evidence-hold",
        action_type="apply_inventory_hold",
        case_id=case_id,
        target_ids=["LOT-PROBABLE-160", "LOT-EXACT-170"],
        rationale="Hold each lot only for its reviewed evidence.",
        evidence_by_target={
            "LOT-PROBABLE-160": ["EV-A"],
            "LOT-EXACT-170": ["EV-B"],
        },
        evidence_ids=["EV-A", "EV-B"],
        expected_case_version=1,
    )
    changed = ProposedAction.model_validate(
        {
            **reviewed.model_dump(mode="python"),
            "evidence_by_target": {
                "LOT-PROBABLE-160": ["EV-B"],
                "LOT-EXACT-170": ["EV-A"],
            },
        }
    )

    with pytest.raises(ApprovalRequiredError, match="approval binding"):
        service.apply_inventory_hold(
            case_id=case_id,
            lot_ids=["LOT-PROBABLE-160", "LOT-EXACT-170"],
            proposed_action=changed,
            approval=_bound_approval(reviewed),
            expected_case_version=1,
            idempotency_key="target-evidence-rejected",
        )


def test_registry_lookup_and_no_result_fallback() -> None:
    registry = RecallRegistryService()

    assert registry.get_recall("H-1230-2026").recall_number == "H-1230-2026"
    assert registry.search_recalls("Salmonella")[0].recall_number == "H-1230-2026"
    assert registry.search_recalls("does-not-exist") == []


def test_traceability_scores_and_classifies_products_and_lots() -> None:
    traceability = TraceabilityService()

    candidates = traceability.find_candidate_products(_predicate())
    assert candidates[0]["product_id"] == "P-EXACT"
    assert candidates[0]["score"] == 1.0
    assert candidates[0]["classification"] == "exact"
    classifications = {
        match["lot_id"]: match["classification"] for match in traceability.match_lots(_predicate())
    }
    assert {
        lot_id: classifications[lot_id]
        for lot_id in {
            "LOT-EXACT-170",
            "LOT-PROBABLE-160",
            "LOT-AMBIG-175",
            "LOT-REJECT-190",
            "LOT-CONTROL-170",
            "LOT-NEAR-150",
        }
    } == {
        "LOT-EXACT-170": "exact",
        "LOT-PROBABLE-160": "probable",
        "LOT-AMBIG-175": "ambiguous",
        "LOT-REJECT-190": "rejected",
        "LOT-CONTROL-170": "rejected",
        "LOT-NEAR-150": "rejected",
    }


def test_traceability_forward_backward_and_reconciliation() -> None:
    traceability = TraceabilityService()

    assert {"EV-001", "EV-002", "EV-003", "EV-008"} <= {
        event["event_id"] for event in traceability.trace_forward("LOT-EXACT-170")
    }
    forward = traceability.trace_forward("LOT-EXACT-170")
    backward = traceability.trace_backward("LOT-EXACT-170")
    forward_positions = {event["event_id"]: index for index, event in enumerate(forward)}
    backward_positions = {event["event_id"]: index for index, event in enumerate(backward)}
    for event in forward:
        if parent := event.get("parent_event_id"):
            assert forward_positions[parent] < forward_positions[event["event_id"]]
            assert backward_positions[event["event_id"]] < backward_positions[parent]
    reconciliation = traceability.reconcile_units("LOT-EXACT-170")
    assert reconciliation.unaccounted == 50
    assert (
        reconciliation.received
        == reconciliation.on_hand
        + reconciliation.quarantined
        + reconciliation.sold
        + reconciliation.returned
        + reconciliation.disposed
        + reconciliation.unaccounted
    )
    assert reconciliation.verified is True
    assert reconciliation.component_evidence == {
        "received": ["EV-001"],
        "on_hand": ["INV-LOT-EXACT-170"],
        "quarantined": ["EV-008"],
        "sold": ["EV-S-LOT-EXACT-170"],
        "returned": ["EV-R-LOT-EXACT-170"],
        "disposed": ["EV-D-LOT-EXACT-170"],
        "unaccounted": [
            "EV-001",
            "INV-LOT-EXACT-170",
            "EV-008",
            "EV-S-LOT-EXACT-170",
            "EV-R-LOT-EXACT-170",
            "EV-D-LOT-EXACT-170",
        ],
    }
    assert reconciliation.evidence_ids == reconciliation.component_evidence["unaccounted"]


def test_operations_rejects_unapproved_and_stale_writes_and_is_idempotent(tmp_path: Path) -> None:
    operations = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    case_input = _case_input()
    created = operations.create_case(
        case_id="CASE-001",
        **case_input,
        **_review(
            "create_case",
            "CASE-001",
            0,
            case_input["confirmed_lot_ids"],
            evidence_ids=case_input["trace_event_ids"],
        ),
        expected_case_version=0,
        idempotency_key="create-001",
    )
    with pytest.raises(ApprovalRequiredError):
        operations.apply_inventory_hold(
            case_id="CASE-001",
            lot_ids=["LOT-PROBABLE-160"],
            **_review(
                "apply_inventory_hold",
                "CASE-001",
                1,
                ["LOT-PROBABLE-160"],
                decision="reject",
            ),
            expected_case_version=1,
            idempotency_key="hold-rejected",
        )
    with pytest.raises(StaleCaseVersionError):
        operations.apply_inventory_hold(
            case_id="CASE-001",
            lot_ids=["LOT-PROBABLE-160"],
            **_review("apply_inventory_hold", "CASE-001", 0, ["LOT-PROBABLE-160"]),
            expected_case_version=0,
            idempotency_key="hold-stale",
        )
    receipt = operations.apply_inventory_hold(
        case_id="CASE-001",
        lot_ids=["LOT-PROBABLE-160"],
        **_review("apply_inventory_hold", "CASE-001", 1, ["LOT-PROBABLE-160"]),
        expected_case_version=1,
        idempotency_key="hold-idempotent",
    )

    replay = operations.apply_inventory_hold(
        case_id="CASE-001",
        lot_ids=["LOT-PROBABLE-160"],
        **_review("apply_inventory_hold", "CASE-001", 1, ["LOT-PROBABLE-160"]),
        expected_case_version=1,
        idempotency_key="hold-idempotent",
    )
    assert created.status == "simulated"
    assert replay.receipt_id == receipt.receipt_id


def test_operations_blocks_closure_when_quantities_or_acknowledgements_are_unresolved(
    tmp_path: Path,
) -> None:
    operations = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    case_input = _case_input(unaccounted=1)
    operations.create_case(
        case_id="CASE-CLOSE",
        **case_input,
        **_review(
            "create_case",
            "CASE-CLOSE",
            0,
            case_input["confirmed_lot_ids"],
            evidence_ids=case_input["trace_event_ids"],
        ),
        expected_case_version=0,
        idempotency_key="create-close",
    )
    operations.apply_inventory_hold(
        case_id="CASE-CLOSE",
        lot_ids=case_input["confirmed_lot_ids"],
        **_review(
            "apply_inventory_hold",
            "CASE-CLOSE",
            1,
            case_input["confirmed_lot_ids"],
        ),
        expected_case_version=1,
        idempotency_key="hold-close",
    )
    with pytest.raises(ClosureBlockedError, match="unaccounted"):
        operations.close_case(
            case_id="CASE-CLOSE",
            **_review("close_case", "CASE-CLOSE", 2, []),
            expected_case_version=2,
            idempotency_key="close-blocked",
        )


def test_operations_closes_authoritative_zero_gap_after_all_acknowledgements(
    tmp_path: Path,
) -> None:
    operations = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    case_input = _case_input()
    operations.create_case(
        case_id="CASE-SAFE",
        **case_input,
        **_review(
            "create_case",
            "CASE-SAFE",
            0,
            case_input["confirmed_lot_ids"],
            evidence_ids=case_input["trace_event_ids"],
        ),
        expected_case_version=0,
        idempotency_key="safe-create",
    )
    operations.apply_inventory_hold(
        case_id="CASE-SAFE",
        lot_ids=case_input["confirmed_lot_ids"],
        **_review(
            "apply_inventory_hold",
            "CASE-SAFE",
            1,
            case_input["confirmed_lot_ids"],
        ),
        expected_case_version=1,
        idempotency_key="safe-hold",
    )
    operations.create_facility_tasks(
        case_id="CASE-SAFE",
        facility_ids=["DC-SOUTH", "STORE-03"],
        **_review("create_facility_tasks", "CASE-SAFE", 2, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=2,
        idempotency_key="safe-tasks",
    )
    for version, facility_id in enumerate(("DC-SOUTH", "STORE-03"), start=3):
        operations.record_acknowledgment(
            case_id="CASE-SAFE",
            facility_id=facility_id,
            **_review("record_acknowledgment", "CASE-SAFE", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"safe-ack-{facility_id}",
        )
    receipt = operations.close_case(
        case_id="CASE-SAFE",
        **_review("close_case", "CASE-SAFE", 5, []),
        expected_case_version=5,
        idempotency_key="safe-close",
    )
    assert receipt.status == "simulated"


def test_operations_persists_idempotency_receipts_when_a_store_is_supplied(tmp_path: Path) -> None:
    store = tmp_path / "operations.sqlite3"
    first = OperationsService(storage_path=store)
    case_input = _case_input()
    review = _review(
        "create_case",
        "CASE-DURABLE",
        0,
        case_input["confirmed_lot_ids"],
        evidence_ids=case_input["trace_event_ids"],
    )
    receipt = first.create_case(
        case_id="CASE-DURABLE",
        **case_input,
        **review,
        expected_case_version=0,
        idempotency_key="durable-create",
    )

    replay = OperationsService(storage_path=store).create_case(
        case_id="CASE-DURABLE",
        **case_input,
        **review,
        expected_case_version=0,
        idempotency_key="durable-create",
    )

    assert store.exists()
    assert replay.receipt_id == receipt.receipt_id
