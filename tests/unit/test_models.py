from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from recallops.models import (
    ApprovalBinding,
    ApprovalDecision,
    AuditReceipt,
    Lot,
    Product,
    ProposedAction,
    RecallCaseState,
    RecallPredicate,
    RecallRecord,
    Reconciliation,
    proposed_action_digest,
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


@pytest.mark.parametrize(
    "invalid_version",
    [True, 1.0, "1", float("nan"), float("inf"), -1],
)
def test_every_case_version_contract_requires_a_strict_nonnegative_integer(
    invalid_version: object,
) -> None:
    action = ProposedAction(
        action_id="action-1",
        action_type="apply_inventory_hold",
        case_id="CASE-1",
        target_ids=["LOT-1"],
        rationale="Reviewed containment action.",
        evidence_by_target={"LOT-1": ["EV-1"]},
        evidence_ids=["EV-1"],
        expected_case_version=1,
    )
    approval = ApprovalDecision(
        decision="approve",
        actor="reviewer",
        justification="Reviewed exact action.",
        approved_at=datetime(2026, 8, 30, tzinfo=UTC),
        approved_case_version=1,
        approved_case_id=action.case_id,
        action_ids=[action.action_id],
        action_bindings=[
            ApprovalBinding(
                action_id=action.action_id,
                action_digest=proposed_action_digest(action),
            )
        ],
    )
    contracts = [
        (
            ProposedAction,
            action.model_dump(mode="python"),
            "expected_case_version",
        ),
        (
            ApprovalDecision,
            approval.model_dump(mode="python"),
            "approved_case_version",
        ),
        (
            AuditReceipt,
            {
                "receipt_id": "receipt-1",
                "case_id": action.case_id,
                "action_type": action.action_type,
                "actor": approval.actor,
                "justification": approval.justification,
                "idempotency_key": "key-1",
                "case_version": 2,
                "status": "simulated",
            },
            "case_version",
        ),
        (
            RecallCaseState,
            {
                "case_id": action.case_id,
                "thread_id": "thread-1",
                "recall_number": "H-1230-2026",
                "case_version": 1,
            },
            "case_version",
        ),
    ]

    for model, payload, field in contracts:
        payload[field] = invalid_version
        with pytest.raises(ValidationError, match=field):
            model.model_validate(payload)


def test_proposed_action_canonicalizes_and_freezes_target_evidence_in_approval_digest() -> None:
    first = ProposedAction(
        action_id="action-evidence",
        action_type="apply_inventory_hold",
        case_id="CASE-1",
        target_ids=["LOT-B", "LOT-A"],
        rationale="Reviewed exact lot evidence.",
        evidence_by_target={"LOT-B": ["EV-2"], "LOT-A": ["EV-1"]},
        evidence_ids=["EV-2", "EV-1"],
        expected_case_version=1,
    )
    second = ProposedAction(
        action_id="action-evidence",
        action_type="apply_inventory_hold",
        case_id="CASE-1",
        target_ids=["LOT-B", "LOT-A"],
        rationale="Reviewed exact lot evidence.",
        evidence_by_target={"LOT-A": ["EV-1"], "LOT-B": ["EV-2"]},
        evidence_ids=["EV-2", "EV-1"],
        expected_case_version=1,
    )

    assert list(first.evidence_by_target) == ["LOT-A", "LOT-B"]
    assert first.evidence_by_target["LOT-A"] == ("EV-1",)
    assert first.model_dump(mode="json")["evidence_by_target"] == {
        "LOT-A": ["EV-1"],
        "LOT-B": ["EV-2"],
    }
    assert proposed_action_digest(first) == proposed_action_digest(second)
    with pytest.raises(TypeError):
        first.evidence_by_target["LOT-A"] = ("EV-MUTATED",)
    with pytest.raises(TypeError):
        first.evidence_by_target["LOT-A"][0] = "EV-MUTATED"


@pytest.mark.parametrize(
    ("evidence_by_target", "evidence_ids", "message"),
    [
        ({"LOT-A": ["EV-1"]}, ["EV-1"], "cover every target"),
        (
            {"LOT-A": ["EV-1"], "LOT-B": ["EV-2"], "LOT-X": ["EV-X"]},
            ["EV-1", "EV-2", "EV-X"],
            "cover every target",
        ),
        ({"LOT-A": ["EV-1"], "LOT-B": []}, ["EV-1"], "unsupported target"),
        (
            {"LOT-A": ["EV-1"], "LOT-B": ["EV-2"]},
            ["EV-1"],
            "equal the target evidence union",
        ),
        (
            {"LOT-A": ["EV-1", "EV-1"], "LOT-B": ["EV-2"]},
            ["EV-1", "EV-2"],
            "unique nonblank",
        ),
    ],
)
def test_proposed_action_rejects_detached_or_noncanonical_target_evidence(
    evidence_by_target: dict[str, list[str]],
    evidence_ids: list[str],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ProposedAction(
            action_id="action-evidence",
            action_type="apply_inventory_hold",
            case_id="CASE-1",
            target_ids=["LOT-A", "LOT-B"],
            rationale="Reviewed exact lot evidence.",
            evidence_by_target=evidence_by_target,
            evidence_ids=evidence_ids,
            expected_case_version=1,
        )


def test_proposed_action_requires_explicit_target_evidence_even_for_no_targets() -> None:
    with pytest.raises(ValidationError, match="evidence_by_target"):
        ProposedAction(
            action_id="action-close",
            action_type="close_case",
            case_id="CASE-1",
            target_ids=[],
            rationale="Reviewed closure.",
            evidence_ids=[],
            expected_case_version=1,
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
