from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from recallops.models import (
    ApprovalDecision,
    Lot,
    Product,
    RecallPredicate,
    RecallRecord,
    Reconciliation,
)


def test_recall_record_rejects_unknown_provenance() -> None:
    with pytest.raises(ValidationError, match="provenance"):
        RecallRecord(
            recall_number="H-1230-2026",
            source="openFDA",
            provenance="UNLABELLED",
            retrieved_at=datetime.now(UTC),
            payload={"recall_number": "H-1230-2026"},
        )


def test_lot_rejects_negative_quantities() -> None:
    product = Product(
        product_id="P-EXACT",
        name="Northstar large eggs",
        upc="011110609038",
        origin="SYNTHETIC_RETAILER_DIGITAL_TWIN",
    )
    with pytest.raises(ValidationError):
        Lot(
            lot_id="LOT-001",
            product_id=product.product_id,
            plant_code="P-1950",
            julian_date=170,
            received_units=-1,
            origin=product.origin,
        )


@pytest.mark.parametrize("decision", ["yes", "", "APPROVE"])
def test_approval_decision_rejects_malformed_decision(decision: str) -> None:
    with pytest.raises(ValidationError, match="decision"):
        ApprovalDecision(
            decision=decision,
            actor="food-safety-manager",
            justification="checked evidence",
            approved_at=datetime.now(UTC),
            approved_case_version=0,
        )


def test_approval_decision_requires_the_reviewed_case_version() -> None:
    with pytest.raises(ValidationError, match="approved_case_version"):
        ApprovalDecision(
            decision="approve",
            actor="food-safety-manager",
            justification="checked evidence",
            approved_at=datetime.now(UTC),
        )


def test_verified_reconciliation_requires_balanced_nonnegative_evidence() -> None:
    with pytest.raises(ValidationError, match="equation"):
        Reconciliation(
            lot_id="LOT-001",
            received=10,
            on_hand=8,
            quarantined=0,
            sold=0,
            returned=0,
            disposed=0,
            unaccounted=1,
            verified=True,
            evidence_ids=["EV-RECEIVE", "INV-001"],
            component_evidence={
                "received": ["EV-RECEIVE"],
                "on_hand": ["INV-001"],
                "quarantined": [],
                "sold": [],
                "returned": [],
                "disposed": [],
                "unaccounted": ["EV-RECEIVE", "INV-001"],
            },
        )

    with pytest.raises(ValidationError):
        Reconciliation(
            lot_id="LOT-001",
            received=10,
            on_hand=11,
            quarantined=0,
            sold=0,
            returned=0,
            disposed=0,
            unaccounted=-1,
        )


def test_predicate_normalizes_plant_and_julian_range() -> None:
    predicate = RecallPredicate(
        product_terms=["egg"],
        upcs=["0 11110-60903 8"],
        plant_codes=["p-1950"],
        julian_start=157,
        julian_end=184,
        geography=["Texas"],
        hazard="Possible Salmonella Enteritidis",
    )

    assert predicate.plant_codes == ["P-1950"]
    assert predicate.upcs == ["011110609038"]
