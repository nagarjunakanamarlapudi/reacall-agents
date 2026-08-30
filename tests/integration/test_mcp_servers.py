import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from recallops.mcp.gateway import DirectGateway, StdioMCPGateway
from recallops.models import ApprovalDecision, RecallPredicate, Reconciliation
from recallops.services.operations import (
    ApprovalRequiredError,
    IdempotencyConflictError,
    OperationsService,
)
from recallops.services.traceability import TraceabilityService

EXPECTED_TOOLS = {
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


def _connections(database: Path) -> dict[str, dict[str, object]]:
    environment = {
        **os.environ,
        "RECALLOPS_DATA_DIR": str(Path("data").resolve()),
        "RECALLOPS_SOURCE_MODE": "snapshot",
        "RECALLOPS_OPERATIONS_DB": str(database),
    }

    def connection(module: str) -> dict[str, object]:
        return {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", module],
            "env": environment,
        }

    return {
        "registry": connection("recallops.mcp.recall_registry_server"),
        "traceability": connection("recallops.mcp.traceability_server"),
        "operations": connection("recallops.mcp.operations_server"),
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
        actor="integration-reviewer",
        justification="reviewed exact integration evidence",
        approved_at=datetime(2026, 8, 30, 15, version, tzinfo=UTC),
        approved_case_version=version,
        action_ids=[f"integration-action-{version}"],
    )


def _reconciliation() -> Reconciliation:
    return TraceabilityService().reconcile_units("LOT-PROBABLE-160")


def _case_kwargs(case_id: str = "CASE-MCP-INTEGRATION") -> dict:
    events = TraceabilityService().trace_forward("LOT-PROBABLE-160")
    return {
        "case_id": case_id,
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": ["LOT-PROBABLE-160"],
        "trace_event_ids": [event["event_id"] for event in events],
        "required_facilities": ["DC-SOUTH", "STORE-03"],
        "reconciliation": [_reconciliation()],
        "evidence_gaps": [],
        "approval": _approval(0),
        "expected_case_version": 0,
        "idempotency_key": "mcp-create",
    }


def _tool_json(result):
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return json.loads(result[0]["text"])
    if isinstance(result, str):
        return json.loads(result)
    return result


def _without_created_at(value):
    if isinstance(value, dict):
        return {
            key: _without_created_at(item) for key, item in value.items() if key != "created_at"
        }
    if isinstance(value, list):
        return [_without_created_at(item) for item in value]
    return value


@pytest.mark.asyncio
async def test_all_servers_discovery_schemas_resources_trace_and_restart_replay(
    tmp_path: Path,
) -> None:
    connections = _connections(tmp_path / "operations.sqlite3")
    client = MultiServerMCPClient(connections)
    tools = await client.get_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS

    resources: dict[str, set[str]] = {}
    output_schemas: dict[str, dict] = {}
    for server in ("registry", "traceability", "operations"):
        async with client.session(server) as session:
            resources[server] = {
                str(resource.uri) for resource in (await session.list_resources()).resources
            }
            output_schemas.update(
                {
                    tool.name: tool.outputSchema
                    for tool in (await session.list_tools()).tools
                    if tool.outputSchema is not None
                }
            )
    assert resources == {
        "registry": {"recallops://policy/provenance"},
        "traceability": {"recallops://policy/synthetic-boundary"},
        "operations": set(),
    }
    assert "properties" in output_schemas["reconcile_units"]
    assert "properties" in output_schemas["create_case"]

    by_name = {tool.name: tool for tool in tools}
    predicate_schema = json.dumps(by_name["match_lots"].args_schema)
    assert '"minimum": 1' in predicate_schema
    assert '"maximum": 366' in predicate_schema
    assert '"minItems": 1' in predicate_schema
    decision_schema = by_name["create_case"].args_schema["properties"]["decision"]
    assert decision_schema["enum"] == ["approve", "edit", "reject", "escalate"]
    for operation_name in {
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    }:
        assert "approved_case_version" in by_name[operation_name].args_schema["required"]
    disposition_schema = by_name["record_disposition"].args_schema["properties"]["disposition"]
    assert disposition_schema["enum"] == [
        "dispose_unaccounted",
        "quarantined",
        "returned",
    ]

    trace = await by_name["trace_forward"].ainvoke({"lot_id": "LOT-EXACT-170"})
    assert "EV-001" in str(trace)

    create_payload = {
        **_case_kwargs(),
        "reconciliation": [_reconciliation().model_dump(mode="json")],
        "decision": "approve",
        "actor": _approval(0).actor,
        "justification": _approval(0).justification,
        "approved_at": _approval(0).approved_at.isoformat(),
        "approved_case_version": _approval(0).approved_case_version,
        "action_ids": _approval(0).action_ids,
    }
    create_payload.pop("approval")
    created = _tool_json(await by_name["create_case"].ainvoke(create_payload))
    await by_name["create_facility_tasks"].ainvoke(
        {
            "case_id": "CASE-MCP-INTEGRATION",
            "facility_ids": ["DC-SOUTH", "STORE-03"],
            "decision": "approve",
            "actor": _approval(1).actor,
            "justification": _approval(1).justification,
            "approved_at": _approval(1).approved_at.isoformat(),
            "approved_case_version": _approval(1).approved_case_version,
            "action_ids": _approval(1).action_ids,
            "expected_case_version": 1,
            "idempotency_key": "mcp-tasks",
        }
    )

    restarted = MultiServerMCPClient(connections)
    restarted_tools = {tool.name: tool for tool in await restarted.get_tools()}
    replay = _tool_json(await restarted_tools["create_case"].ainvoke(create_payload))
    assert replay == created


@pytest.mark.asyncio
async def test_direct_and_stdio_gateways_have_identical_method_matrix_shapes(
    tmp_path: Path,
) -> None:
    direct = DirectGateway(operations=OperationsService(storage_path=tmp_path / "direct.sqlite3"))
    stdio = StdioMCPGateway(_connections(tmp_path / "stdio.sqlite3"))

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
    write_calls = [
        ("create_case", _case_kwargs()),
        (
            "apply_inventory_hold",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                "lot_ids": ["LOT-PROBABLE-160"],
                "approval": _approval(1),
                "expected_case_version": 1,
                "idempotency_key": "matrix-hold",
            },
        ),
        (
            "create_facility_tasks",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                "facility_ids": ["DC-SOUTH", "STORE-03"],
                "approval": _approval(2),
                "expected_case_version": 2,
                "idempotency_key": "matrix-tasks",
            },
        ),
        (
            "record_acknowledgment",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                "facility_id": "DC-SOUTH",
                "approval": _approval(3),
                "expected_case_version": 3,
                "idempotency_key": "matrix-ack",
            },
        ),
        (
            "record_disposition",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                "lot_id": "LOT-PROBABLE-160",
                "disposition": "quarantined",
                "evidence_id": "EV-Q-LOT-PROBABLE-160",
                "approval": _approval(5),
                "expected_case_version": 5,
                "idempotency_key": "matrix-disposition",
            },
        ),
        (
            "close_case",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                "approval": _approval(6),
                "expected_case_version": 6,
                "idempotency_key": "matrix-close",
            },
        ),
    ]
    for name, kwargs in [*read_calls, *write_calls[:4]]:
        direct_result = await getattr(direct, name)(**kwargs)
        stdio_result = await getattr(stdio, name)(**kwargs)
        json.dumps(direct_result)
        json.dumps(stdio_result)
        assert _without_created_at(direct_result) == _without_created_at(stdio_result)

    second_ack = {
        "case_id": "CASE-MCP-INTEGRATION",
        "facility_id": "STORE-03",
        "approval": _approval(4),
        "expected_case_version": 4,
        "idempotency_key": "matrix-ack-store",
    }
    assert _without_created_at(
        await direct.record_acknowledgment(**second_ack)
    ) == _without_created_at(await stdio.record_acknowledgment(**second_ack))

    for name, kwargs in write_calls[4:]:
        direct_result = await getattr(direct, name)(**kwargs)
        stdio_result = await getattr(stdio, name)(**kwargs)
        json.dumps(direct_result)
        json.dumps(stdio_result)
        assert _without_created_at(direct_result) == _without_created_at(stdio_result)


@pytest.mark.asyncio
async def test_direct_and_stdio_preserve_reviewed_version_for_rejection_conflict_and_replay(
    tmp_path: Path,
) -> None:
    direct = DirectGateway(operations=OperationsService(storage_path=tmp_path / "approval-direct"))
    stdio = StdioMCPGateway(_connections(tmp_path / "approval-stdio"))

    for gateway in (direct, stdio):
        case_id = "CASE-APPROVAL-PARITY"
        await gateway.create_case(**_case_kwargs(case_id))
        stale_review = {
            "case_id": case_id,
            "lot_ids": ["LOT-PROBABLE-160"],
            "approval": _approval(0),
            "expected_case_version": 1,
            "idempotency_key": "stale-reviewed-version",
        }
        expected_error = ApprovalRequiredError if gateway is direct else Exception
        with pytest.raises(expected_error, match="approval must be bound"):
            await gateway.apply_inventory_hold(**stale_review)

        accepted = {
            **stale_review,
            "approval": _approval(1),
            "idempotency_key": "approval-replay",
        }
        first = await gateway.apply_inventory_hold(**accepted)
        assert await gateway.apply_inventory_hold(**accepted) == first

        changed_review = {**accepted, "approval": _approval(0)}
        expected_error = IdempotencyConflictError if gateway is direct else Exception
        with pytest.raises(expected_error, match="idempotency key is bound"):
            await gateway.apply_inventory_hold(**changed_review)
