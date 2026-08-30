import json
from datetime import UTC, datetime
from inspect import Parameter, signature
from pathlib import Path

import pytest

from recallops.mcp.gateway import DirectGateway, Gateway
from recallops.models import (
    ApprovalBinding,
    ApprovalDecision,
    ProposedAction,
    RecallPredicate,
    proposed_action_digest,
)
from recallops.services.operations import OperationsService
from recallops.services.traceability import TraceabilityService

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


def _review(
    case_id: str,
    action_type: str,
    version: int,
    target_ids: list[str],
    *,
    evidence_ids: list[str] | None = None,
) -> dict:
    action = ProposedAction(
        action_id=f"{case_id}-{action_type}-{version}",
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids,
        rationale=f"Reviewed {action_type}.",
        evidence_ids=evidence_ids or [],
        expected_case_version=version,
    )
    approval = ApprovalDecision(
        decision="approve",
        actor="reviewer",
        justification="matrix evidence",
        approved_at=datetime(2026, 8, 30, 12, version, tzinfo=UTC),
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
    return {"proposed_action": action, "approval": approval}


def _case_input() -> dict:
    traceability = TraceabilityService()
    lot_id = "LOT-PROBABLE-160"
    events = traceability.trace_forward(lot_id)
    payload = {
        "case_id": "CASE-GATEWAY",
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": [lot_id],
        "trace_event_ids": [event["event_id"] for event in events],
        "required_facilities": ["DC-SOUTH", "STORE-03"],
        "reconciliation": [traceability.reconcile_units(lot_id)],
        "evidence_gaps": [],
        "expected_case_version": 0,
        "idempotency_key": "gateway-create",
    }
    return {
        **payload,
        **_review(
            "CASE-GATEWAY",
            "create_case",
            0,
            payload["confirmed_lot_ids"],
            evidence_ids=payload["trace_event_ids"],
        ),
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
    for name in {
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    }:
        assert all(
            parameter.kind is not Parameter.VAR_KEYWORD
            for parameter in signature(getattr(Gateway, name)).parameters.values()
        )


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
            lot_ids=["LOT-PROBABLE-160"],
            **_review("CASE-GATEWAY", "apply_inventory_hold", 1, ["LOT-PROBABLE-160"]),
            expected_case_version=1,
            idempotency_key="gateway-hold",
        )
    )
    json.dumps(
        await gateway.create_facility_tasks(
            case_id="CASE-GATEWAY",
            facility_ids=["DC-SOUTH", "STORE-03"],
            **_review("CASE-GATEWAY", "create_facility_tasks", 2, ["DC-SOUTH", "STORE-03"]),
            expected_case_version=2,
            idempotency_key="gateway-tasks",
        )
    )
    json.dumps(
        await gateway.record_acknowledgment(
            case_id="CASE-GATEWAY",
            facility_id="DC-SOUTH",
            **_review("CASE-GATEWAY", "record_acknowledgment", 3, ["DC-SOUTH"]),
            expected_case_version=3,
            idempotency_key="gateway-ack",
        )
    )
    json.dumps(
        await gateway.record_acknowledgment(
            case_id="CASE-GATEWAY",
            facility_id="STORE-03",
            **_review("CASE-GATEWAY", "record_acknowledgment", 4, ["STORE-03"]),
            expected_case_version=4,
            idempotency_key="gateway-ack-store",
        )
    )
    json.dumps(
        await gateway.record_disposition(
            case_id="CASE-GATEWAY",
            lot_id="LOT-PROBABLE-160",
            disposition="quarantined",
            evidence_id="EV-Q-LOT-PROBABLE-160",
            **_review(
                "CASE-GATEWAY",
                "record_disposition",
                5,
                ["LOT-PROBABLE-160"],
                evidence_ids=["EV-Q-LOT-PROBABLE-160"],
            ),
            expected_case_version=5,
            idempotency_key="gateway-disposition",
        )
    )
    json.dumps(
        await gateway.close_case(
            case_id="CASE-GATEWAY",
            **_review("CASE-GATEWAY", "close_case", 6, []),
            expected_case_version=6,
            idempotency_key="gateway-close",
        )
    )
