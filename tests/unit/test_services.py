from datetime import UTC, datetime
from pathlib import Path

import pytest

from recallops.models import ApprovalDecision, RecallPredicate
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


def _approval(decision: str = "approve") -> ApprovalDecision:
    return ApprovalDecision(
        decision=decision,
        actor="food-safety-manager",
        justification="evidence reviewed",
        approved_at=datetime.now(UTC),
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
    assert {match["classification"] for match in traceability.match_lots(_predicate())} >= {
        "exact",
        "probable",
        "ambiguous",
        "rejected",
    }


def test_traceability_forward_backward_and_reconciliation() -> None:
    traceability = TraceabilityService()

    assert {"EV-001", "EV-002", "EV-003", "EV-008"} <= {
        event["event_id"] for event in traceability.trace_forward("LOT-EXACT-170")
    }
    assert traceability.trace_backward("LOT-EXACT-170")[0]["event_type"] == "disposal"
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


def test_operations_rejects_unapproved_and_stale_writes_and_is_idempotent(tmp_path: Path) -> None:
    operations = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    created = operations.create_case(
        case_id="CASE-001",
        recall_number="H-1230-2026",
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="create-001",
    )
    with pytest.raises(ApprovalRequiredError):
        operations.apply_inventory_hold(
            case_id="CASE-001",
            lot_ids=["LOT-EXACT-170"],
            approval=_approval("reject"),
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
        approval=_approval(),
        expected_case_version=1,
        idempotency_key="hold-idempotent",
    )

    replay = operations.apply_inventory_hold(
        case_id="CASE-001",
        lot_ids=["LOT-EXACT-170"],
        approval=_approval(),
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
        recall_number="H-1230-2026",
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="create-close",
    )
    with pytest.raises(ClosureBlockedError, match="unaccounted"):
        operations.close_case(
            case_id="CASE-CLOSE",
            approval=_approval(),
            expected_case_version=1,
            idempotency_key="close-blocked",
        )


def test_operations_can_close_only_after_disposition_and_acknowledgement(tmp_path: Path) -> None:
    operations = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    operations.create_case(
        case_id="CASE-SAFE",
        recall_number="H-1230-2026",
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="safe-create",
    )
    operations.record_disposition(
        case_id="CASE-SAFE",
        lot_id="LOT-EXACT-170",
        disposition="dispose_unaccounted",
        approval=_approval(),
        expected_case_version=1,
        idempotency_key="safe-dispose",
    )
    operations.record_acknowledgment(
        case_id="CASE-SAFE",
        facility_id="DC-NORTH",
        approval=_approval(),
        expected_case_version=2,
        idempotency_key="safe-ack",
    )
    receipt = operations.close_case(
        case_id="CASE-SAFE",
        approval=_approval(),
        expected_case_version=3,
        idempotency_key="safe-close",
    )
    assert receipt.status == "simulated"


def test_operations_persists_idempotency_receipts_when_a_store_is_supplied(tmp_path: Path) -> None:
    store = tmp_path / "operations.sqlite3"
    first = OperationsService(storage_path=store)
    receipt = first.create_case(
        case_id="CASE-DURABLE",
        recall_number="H-1230-2026",
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="durable-create",
    )

    replay = OperationsService(storage_path=store).create_case(
        case_id="CASE-DURABLE",
        recall_number="H-1230-2026",
        approval=_approval(),
        expected_case_version=0,
        idempotency_key="durable-create",
    )

    assert store.exists()
    assert replay.receipt_id == receipt.receipt_id
