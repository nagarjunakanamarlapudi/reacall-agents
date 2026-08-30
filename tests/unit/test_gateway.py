import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recallops.mcp.gateway import DirectGateway, Gateway
from recallops.models import ApprovalDecision, RecallPredicate, Reconciliation
from recallops.services.operations import OperationsService

EXPECTED_GATEWAY_METHODS = {
    "search_recalls",
    "get_recall",
    "get_product_metadata",
    "find_candidate_products",
    "match_lots",
    "trace_forward",
    "trace_backward",
    "get_inventory",
    "get_sales",
    "reconcile_units",
    "create_case",
    "apply_inventory_hold",
    "create_facility_tasks",
    "record_acknowledgment",
    "record_disposition",
    "close_case",
}


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


def _approval(version: int) -> ApprovalDecision:
    return ApprovalDecision(
        decision="approve",
        actor="reviewer",
        justification="matrix evidence",
        approved_at=datetime(2026, 8, 30, 12, version, tzinfo=UTC),
        approved_case_version=version,
        action_ids=[f"action-v{version}"],
    )


def _case_input() -> dict:
    reconciliation = Reconciliation(
        lot_id="LOT-EXACT-170",
        received=10,
        on_hand=9,
        quarantined=0,
        sold=0,
        returned=0,
        disposed=0,
        unaccounted=1,
        verified=True,
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
    )
    return {
        "case_id": "CASE-GATEWAY",
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": ["LOT-EXACT-170"],
        "trace_event_ids": ["EV-RECEIVE"],
        "required_facilities": ["DC-NORTH"],
        "reconciliation": [reconciliation],
        "evidence_gaps": [],
        "approval": _approval(0),
        "expected_case_version": 0,
        "idempotency_key": "gateway-create",
    }


def test_gateway_protocol_declares_every_service_method() -> None:
    methods = {
        name
        for name, member in Gateway.__dict__.items()
        if not name.startswith("_") and callable(member)
    }
    assert methods == EXPECTED_GATEWAY_METHODS
    assert EXPECTED_GATEWAY_METHODS <= {
        name for name, member in DirectGateway.__dict__.items() if callable(member)
    }


@pytest.mark.asyncio
async def test_every_direct_gateway_result_is_json_serializable(tmp_path: Path) -> None:
    gateway = DirectGateway(
        operations=OperationsService(storage_path=tmp_path / "operations.sqlite3")
    )
    read_calls = [
        ("search_recalls", {"query": "Salmonella"}),
        ("get_recall", {"recall_number": "H-1230-2026"}),
        ("get_product_metadata", {"upc": "011110609038"}),
        ("find_candidate_products", {"predicate": _predicate()}),
        ("match_lots", {"predicate": _predicate()}),
        ("trace_forward", {"lot_id": "LOT-EXACT-170"}),
        ("trace_backward", {"lot_id": "LOT-EXACT-170"}),
        ("get_inventory", {"lot_id": "LOT-EXACT-170"}),
        ("get_sales", {"lot_id": "LOT-EXACT-170"}),
        ("reconcile_units", {"lot_id": "LOT-EXACT-170"}),
    ]
    for name, kwargs in read_calls:
        json.dumps(await getattr(gateway, name)(**kwargs))

    json.dumps(await gateway.create_case(**_case_input()))
    json.dumps(
        await gateway.apply_inventory_hold(
            case_id="CASE-GATEWAY",
            lot_ids=["LOT-EXACT-170"],
            approval=_approval(1),
            expected_case_version=1,
            idempotency_key="gateway-hold",
        )
    )
    json.dumps(
        await gateway.create_facility_tasks(
            case_id="CASE-GATEWAY",
            facility_ids=["DC-NORTH"],
            approval=_approval(2),
            expected_case_version=2,
            idempotency_key="gateway-tasks",
        )
    )
    json.dumps(
        await gateway.record_acknowledgment(
            case_id="CASE-GATEWAY",
            facility_id="DC-NORTH",
            approval=_approval(3),
            expected_case_version=3,
            idempotency_key="gateway-ack",
        )
    )
    json.dumps(
        await gateway.record_disposition(
            case_id="CASE-GATEWAY",
            lot_id="LOT-EXACT-170",
            disposition="dispose_unaccounted",
            evidence_id="EV-DISPOSE",
            approval=_approval(4),
            expected_case_version=4,
            idempotency_key="gateway-disposition",
        )
    )
    json.dumps(
        await gateway.close_case(
            case_id="CASE-GATEWAY",
            approval=_approval(5),
            expected_case_version=5,
            idempotency_key="gateway-close",
        )
    )
