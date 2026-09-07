import json
from inspect import Parameter, signature
from pathlib import Path

import pytest

from recallops.mcp.gateway import DirectGateway, Gateway
from recallops.models import RecallPredicate
from recallops.services.operations import OperationsService

EXPECTED_GATEWAY_METHODS = {
    "search_recalls",
    "search_regulatory_evidence",
    "get_recall",
    "get_product_metadata",
    "find_candidate_products",
    "match_lots",
    "trace_forward",
    "trace_backward",
    "get_inventory",
    "get_sales",
    "reconcile_units",
    "search_operational_evidence",
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
async def test_every_direct_read_gateway_result_is_json_serializable(tmp_path: Path) -> None:
    gateway = DirectGateway(
        operations=OperationsService(storage_path=tmp_path / "operations.sqlite3")
    )
    read_calls = [
        ("search_recalls", {"query": "Salmonella"}),
        (
            "search_regulatory_evidence",
            {"query": "Class I recall", "top_k": 4, "record_types": ()},
        ),
        ("get_recall", {"recall_number": "H-1230-2026"}),
        ("get_product_metadata", {"upc": "011110609038"}),
        ("find_candidate_products", {"predicate": _predicate()}),
        ("match_lots", {"predicate": _predicate()}),
        ("trace_forward", {"lot_id": "LOT-EXACT-170"}),
        ("trace_backward", {"lot_id": "LOT-EXACT-170"}),
        ("get_inventory", {"lot_id": "LOT-EXACT-170"}),
        ("get_sales", {"lot_id": "LOT-EXACT-170"}),
        ("reconcile_units", {"lot_id": "LOT-EXACT-170"}),
        (
            "search_operational_evidence",
            {"query": "LOT-BG-042-03", "top_k": 4, "record_types": ()},
        ),
    ]
    for name, kwargs in read_calls:
        json.dumps(await getattr(gateway, name)(**kwargs))
