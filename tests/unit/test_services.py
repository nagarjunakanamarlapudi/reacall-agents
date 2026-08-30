from datetime import UTC, datetime
from pathlib import Path

import pytest

from recallops.models import ApprovalDecision, RecallPredicate, Reconciliation
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


def _approval(decision: str = "approve", version: int = 0) -> ApprovalDecision:
    return ApprovalDecision(
        decision=decision,
        actor="food-safety-manager",
        justification="evidence reviewed",
        approved_at=datetime(2026, 8, 30, tzinfo=UTC),
        approved_case_version=version,
    )


def _reconciliation(*, unaccounted: int = 0) -> Reconciliation:
    return Reconciliation(
        lot_id="LOT-EXACT-170",
        received=10,
        on_hand=10 - unaccounted,
        quarantined=0,
        sold=0,
        returned=0,
        disposed=0,
        unaccounted=unaccounted,
        evidence_ids=["EV-RECEIVE", "INV-EXACT"],
        component_evidence={
            "received": ["EV-RECEIVE"],
            "on_hand": ["INV-EXACT"],
            "quarantined": [],
            "sold": [],
            "returned": [],
            "disposed": [],
            "unaccounted": ["EV-RECEIVE", "INV-EXACT"],
        },
        verified=True,
    )


def _case_input(*, unaccounted: int = 0) -> dict:
    return {
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": ["LOT-EXACT-170"],
        "trace_event_ids": ["EV-RECEIVE"],
        "required_facilities": ["DC-NORTH"],
        "reconciliation": [_reconciliation(unaccounted=unaccounted)],
        "evidence_gaps": [],
    }


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
    assert {
        match["lot_id"]: match["classification"] for match in traceability.match_lots(_predicate())
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
    created = operations.create_case(
        case_id="CASE-001",
        **_case_input(),
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="create-001",
    )
    with pytest.raises(ApprovalRequiredError):
        operations.apply_inventory_hold(
            case_id="CASE-001",
            lot_ids=["LOT-EXACT-170"],
            approval=_approval("reject", 1),
            expected_case_version=1,
            idempotency_key="hold-rejected",
        )
    with pytest.raises(StaleCaseVersionError):
        operations.apply_inventory_hold(
            case_id="CASE-001",
            lot_ids=["LOT-EXACT-170"],
            approval=_approval(),
            expected_case_version=0,
            idempotency_key="hold-stale",
        )
    receipt = operations.apply_inventory_hold(
        case_id="CASE-001",
        lot_ids=["LOT-EXACT-170"],
        approval=_approval(version=1),
        expected_case_version=1,
        idempotency_key="hold-idempotent",
    )

    replay = operations.apply_inventory_hold(
        case_id="CASE-001",
        lot_ids=["LOT-EXACT-170"],
        approval=_approval(version=1),
        expected_case_version=1,
        idempotency_key="hold-idempotent",
    )
    assert created.status == "simulated"
    assert replay.receipt_id == receipt.receipt_id


def test_operations_blocks_closure_when_quantities_or_acknowledgements_are_unresolved(
    tmp_path: Path,
) -> None:
    operations = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    operations.create_case(
        case_id="CASE-CLOSE",
        **_case_input(unaccounted=1),
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="create-close",
    )
    with pytest.raises(ClosureBlockedError, match="unaccounted"):
        operations.close_case(
            case_id="CASE-CLOSE",
            approval=_approval(version=1),
            expected_case_version=1,
            idempotency_key="close-blocked",
        )


def test_operations_can_close_only_after_disposition_and_acknowledgement(tmp_path: Path) -> None:
    operations = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    operations.create_case(
        case_id="CASE-SAFE",
        **_case_input(unaccounted=1),
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="safe-create",
    )
    operations.record_disposition(
        case_id="CASE-SAFE",
        lot_id="LOT-EXACT-170",
        disposition="dispose_unaccounted",
        evidence_id="EV-DISPOSE",
        approval=_approval(version=1),
        expected_case_version=1,
        idempotency_key="safe-dispose",
    )
    operations.create_facility_tasks(
        case_id="CASE-SAFE",
        facility_ids=["DC-NORTH"],
        approval=_approval(version=2),
        expected_case_version=2,
        idempotency_key="safe-tasks",
    )
    operations.record_acknowledgment(
        case_id="CASE-SAFE",
        facility_id="DC-NORTH",
        approval=_approval(version=3),
        expected_case_version=3,
        idempotency_key="safe-ack",
    )
    receipt = operations.close_case(
        case_id="CASE-SAFE",
        approval=_approval(version=4),
        expected_case_version=4,
        idempotency_key="safe-close",
    )
    assert receipt.status == "simulated"


def test_operations_persists_idempotency_receipts_when_a_store_is_supplied(tmp_path: Path) -> None:
    store = tmp_path / "operations.sqlite3"
    first = OperationsService(storage_path=store)
    receipt = first.create_case(
        case_id="CASE-DURABLE",
        **_case_input(),
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="durable-create",
    )

    replay = OperationsService(storage_path=store).create_case(
        case_id="CASE-DURABLE",
        **_case_input(),
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="durable-create",
    )

    assert store.exists()
    assert replay.receipt_id == receipt.receipt_id
