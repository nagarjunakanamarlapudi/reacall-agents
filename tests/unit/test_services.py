from datetime import UTC, datetime
from pathlib import Path

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
    OperationsService,
    StaleCaseVersionError,
)
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService


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
        "evidence_gaps": [],
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
    action = ProposedAction(
        action_id=f"{case_id}-{action_type}-{version}",
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids,
        rationale=f"Reviewed {action_type} against the case evidence.",
        evidence_ids=evidence_ids or [],
        expected_case_version=version,
    )
    return {"proposed_action": action, "approval": _bound_approval(action, decision=decision)}


@pytest.mark.parametrize(
    "change",
    [
        {"case_id": "CASE-OTHER"},
        {"action_type": "apply_inventory_hold"},
        {"target_ids": ["LOT-EXACT-170"]},
        {"evidence_ids": ["EV-UNREVIEWED"]},
        {"rationale": "Changed after human review."},
    ],
)
def test_operations_service_rejects_cross_case_or_mutated_reviewed_action(
    tmp_path: Path, change: dict
) -> None:
    case_id = "CASE-BOUND-ACTION"
    reviewed = _create_action(case_id)
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
    with pytest.raises(ClosureBlockedError, match="unaccounted"):
        operations.close_case(
            case_id="CASE-CLOSE",
            **_review("close_case", "CASE-CLOSE", 1, []),
            expected_case_version=1,
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
    operations.create_facility_tasks(
        case_id="CASE-SAFE",
        facility_ids=["DC-SOUTH", "STORE-03"],
        **_review("create_facility_tasks", "CASE-SAFE", 1, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=1,
        idempotency_key="safe-tasks",
    )
    for version, facility_id in enumerate(("DC-SOUTH", "STORE-03"), start=2):
        operations.record_acknowledgment(
            case_id="CASE-SAFE",
            facility_id=facility_id,
            **_review("record_acknowledgment", "CASE-SAFE", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"safe-ack-{facility_id}",
        )
    receipt = operations.close_case(
        case_id="CASE-SAFE",
        **_review("close_case", "CASE-SAFE", 4, []),
        expected_case_version=4,
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
