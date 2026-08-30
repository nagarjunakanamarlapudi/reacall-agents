import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from recallops.mcp.gateway import DirectGateway, StdioMCPGateway
from recallops.models import (
    ApprovalBinding,
    ApprovalDecision,
    ProposedAction,
    RecallPredicate,
    Reconciliation,
    proposed_action_digest,
)
from recallops.services.operations import (
    ApprovalRequiredError,
    IdempotencyConflictError,
    OperationsService,
)
from recallops.services.traceability import TraceabilityService

EXPECTED_TOOLS = {
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


def _review(
    case_id: str,
    action_type: str,
    version: int,
    target_ids: list[str],
    *,
    evidence_ids: list[str] | None = None,
    approved_version: int | None = None,
) -> dict:
    action_evidence = evidence_ids or [f"EVIDENCE-{target_id}" for target_id in target_ids]
    action = ProposedAction(
        action_id=f"{case_id}-{action_type}-{version}",
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids,
        rationale=f"Reviewed {action_type} through MCP.",
        evidence_ids=action_evidence,
        evidence_by_target={target_id: action_evidence for target_id in target_ids},
        expected_case_version=version,
    )
    approval = ApprovalDecision(
        decision="approve",
        actor="integration-reviewer",
        justification="reviewed exact integration evidence",
        approved_at=datetime(2026, 8, 30, 15, version, tzinfo=UTC),
        approved_case_version=version if approved_version is None else approved_version,
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


def _reconciliation() -> Reconciliation:
    return TraceabilityService().reconcile_units("LOT-PROBABLE-160")


def _case_kwargs(case_id: str = "CASE-MCP-INTEGRATION") -> dict:
    events = TraceabilityService().trace_forward("LOT-PROBABLE-160")
    payload = {
        "case_id": case_id,
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": ["LOT-PROBABLE-160"],
        "trace_event_ids": [event["event_id"] for event in events],
        "required_facilities": ["DC-SOUTH", "STORE-03"],
        "reconciliation": [_reconciliation()],
        "evidence_gaps": [],
        "expected_case_version": 0,
        "idempotency_key": "mcp-create",
    }
    return {
        **payload,
        **_review(
            case_id,
            "create_case",
            0,
            payload["confirmed_lot_ids"],
            evidence_ids=payload["trace_event_ids"],
        ),
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
    proposed_action_schema = by_name["create_case"].args_schema["properties"]["proposed_action"]
    assert "evidence_by_target" in proposed_action_schema["properties"]
    assert "evidence_by_target" in proposed_action_schema["required"]
    for operation_name in {
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    }:
        required = by_name[operation_name].args_schema["required"]
        assert {
            "approved_case_version",
            "approved_case_id",
            "action_bindings",
            "proposed_action",
        } <= set(required)
    disposition_schema = by_name["record_disposition"].args_schema["properties"]["disposition"]
    assert disposition_schema["enum"] == [
        "dispose_unaccounted",
        "quarantined",
        "returned",
    ]

    trace = await by_name["trace_forward"].ainvoke({"lot_id": "LOT-EXACT-170"})
    assert "EV-001" in str(trace)

    case_kwargs = _case_kwargs()
    create_approval = case_kwargs["approval"]
    create_payload = {
        **case_kwargs,
        "reconciliation": [_reconciliation().model_dump(mode="json")],
        "proposed_action": case_kwargs["proposed_action"].model_dump(mode="json"),
        **create_approval.model_dump(mode="json"),
    }
    create_payload.pop("approval")
    created = _tool_json(await by_name["create_case"].ainvoke(create_payload))
    await by_name["create_facility_tasks"].ainvoke(
        {
            "case_id": "CASE-MCP-INTEGRATION",
            "facility_ids": ["DC-SOUTH", "STORE-03"],
            "expected_case_version": 1,
            "idempotency_key": "mcp-tasks",
            "proposed_action": _review(
                "CASE-MCP-INTEGRATION",
                "create_facility_tasks",
                1,
                ["DC-SOUTH", "STORE-03"],
            )["proposed_action"].model_dump(mode="json"),
            **_review(
                "CASE-MCP-INTEGRATION",
                "create_facility_tasks",
                1,
                ["DC-SOUTH", "STORE-03"],
            )["approval"].model_dump(mode="json"),
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
    write_calls = [
        ("create_case", _case_kwargs()),
        (
            "apply_inventory_hold",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                "lot_ids": ["LOT-PROBABLE-160"],
                **_review(
                    "CASE-MCP-INTEGRATION",
                    "apply_inventory_hold",
                    1,
                    ["LOT-PROBABLE-160"],
                ),
                "expected_case_version": 1,
                "idempotency_key": "matrix-hold",
            },
        ),
        (
            "create_facility_tasks",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                "facility_ids": ["DC-SOUTH", "STORE-03"],
                **_review(
                    "CASE-MCP-INTEGRATION",
                    "create_facility_tasks",
                    2,
                    ["DC-SOUTH", "STORE-03"],
                ),
                "expected_case_version": 2,
                "idempotency_key": "matrix-tasks",
            },
        ),
        (
            "record_acknowledgment",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                "facility_id": "DC-SOUTH",
                **_review(
                    "CASE-MCP-INTEGRATION",
                    "record_acknowledgment",
                    3,
                    ["DC-SOUTH"],
                ),
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
                **_review(
                    "CASE-MCP-INTEGRATION",
                    "record_disposition",
                    5,
                    ["LOT-PROBABLE-160"],
                    evidence_ids=["EV-Q-LOT-PROBABLE-160"],
                ),
                "expected_case_version": 5,
                "idempotency_key": "matrix-disposition",
            },
        ),
        (
            "close_case",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                **_review("CASE-MCP-INTEGRATION", "close_case", 6, []),
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
        **_review(
            "CASE-MCP-INTEGRATION",
            "record_acknowledgment",
            4,
            ["STORE-03"],
        ),
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
            **_review(
                case_id,
                "apply_inventory_hold",
                1,
                ["LOT-PROBABLE-160"],
                approved_version=0,
            ),
            "expected_case_version": 1,
            "idempotency_key": "stale-reviewed-version",
        }
        expected_error = ApprovalRequiredError if gateway is direct else Exception
        with pytest.raises(expected_error, match="approval must be bound"):
            await gateway.apply_inventory_hold(**stale_review)

        accepted = {
            **stale_review,
            **_review(case_id, "apply_inventory_hold", 1, ["LOT-PROBABLE-160"]),
            "idempotency_key": "approval-replay",
        }
        first = await gateway.apply_inventory_hold(**accepted)
        assert await gateway.apply_inventory_hold(**accepted) == first

        changed_approval = accepted["approval"].model_copy(update={"approved_case_version": 0})
        changed_review = {**accepted, "approval": changed_approval}
        expected_error = IdempotencyConflictError if gateway is direct else Exception
        with pytest.raises(expected_error, match="idempotency key is bound"):
            await gateway.apply_inventory_hold(**changed_review)


@pytest.mark.parametrize(
    ("mutation_name", "change"),
    [
        ("case_id", {"case_id": "CASE-CROSS-REPLAY"}),
        ("action_id", {"action_id": "post-review-action-id"}),
        ("action_type", {"action_type": "create_facility_tasks"}),
        (
            "target_ids",
            {
                "target_ids": ["LOT-EXACT-170"],
                "evidence_by_target": {"LOT-EXACT-170": ["EVIDENCE-LOT-PROBABLE-160"]},
            },
        ),
        (
            "evidence_ids",
            {
                "evidence_ids": ["EV-POST-REVIEW"],
                "evidence_by_target": {"LOT-PROBABLE-160": ["EV-POST-REVIEW"]},
            },
        ),
        ("rationale", {"rationale": "Changed after the human reviewed it."}),
        ("expected_case_version", {"expected_case_version": 2}),
    ],
)
@pytest.mark.asyncio
async def test_direct_and_stdio_reject_every_post_approval_action_mutation_identically(
    tmp_path: Path,
    mutation_name: str,
    change: dict,
) -> None:
    case_id = f"CASE-MUTATION-{mutation_name.upper()}"
    direct_db = tmp_path / f"direct-{mutation_name}.sqlite3"
    stdio_db = tmp_path / f"stdio-{mutation_name}.sqlite3"
    gateways = [
        DirectGateway(operations=OperationsService(storage_path=direct_db)),
        StdioMCPGateway(_connections(stdio_db)),
    ]
    messages: list[str] = []

    for gateway in gateways:
        await gateway.create_case(**_case_kwargs(case_id))
        reviewed = _review(case_id, "apply_inventory_hold", 1, ["LOT-PROBABLE-160"])
        changed_action = ProposedAction.model_validate(
            {**reviewed["proposed_action"].model_dump(mode="python"), **change}
        )
        with pytest.raises(Exception) as rejected:
            await gateway.apply_inventory_hold(
                case_id=case_id,
                lot_ids=["LOT-PROBABLE-160"],
                proposed_action=changed_action,
                approval=reviewed["approval"],
                expected_case_version=1,
                idempotency_key=f"rejected-{mutation_name}",
            )
        messages.append(str(rejected.value))

    assert messages[0] in messages[1]


@pytest.mark.parametrize("gateway_kind", ["direct", "stdio"])
@pytest.mark.asyncio
async def test_exact_proposal_replay_is_one_receipt_but_changed_proposal_conflicts(
    tmp_path: Path,
    gateway_kind: str,
) -> None:
    case_id = f"CASE-PROPOSAL-REPLAY-{gateway_kind.upper()}"
    database = tmp_path / f"proposal-replay-{gateway_kind}.sqlite3"
    gateway = (
        DirectGateway(operations=OperationsService(storage_path=database))
        if gateway_kind == "direct"
        else StdioMCPGateway(_connections(database))
    )
    await gateway.create_case(**_case_kwargs(case_id))
    accepted = {
        "case_id": case_id,
        "lot_ids": ["LOT-PROBABLE-160"],
        **_review(case_id, "apply_inventory_hold", 1, ["LOT-PROBABLE-160"]),
        "expected_case_version": 1,
        "idempotency_key": "proposal-replay",
    }

    first = await gateway.apply_inventory_hold(**accepted)
    assert await gateway.apply_inventory_hold(**accepted) == first

    changed_action = accepted["proposed_action"].model_copy(
        update={"rationale": "A separately reviewed alternative rationale."}
    )
    changed_approval = accepted["approval"].model_copy(
        update={
            "action_bindings": (
                ApprovalBinding(
                    action_id=changed_action.action_id,
                    action_digest=proposed_action_digest(changed_action),
                ),
            )
        }
    )
    expected_error = IdempotencyConflictError if gateway_kind == "direct" else Exception
    with pytest.raises(expected_error, match="idempotency key is bound"):
        await gateway.apply_inventory_hold(
            **{
                **accepted,
                "proposed_action": changed_action,
                "approval": changed_approval,
            }
        )

    stored = OperationsService(storage_path=database).get_case(case_id)
    assert stored is not None
    assert stored.case_version == 2
    assert [receipt.action_type for receipt in stored.write_receipts].count(
        "apply_inventory_hold"
    ) == 1


def _target_evidence_review(case_id: str) -> dict:
    action = ProposedAction(
        action_id=f"{case_id}-target-evidence",
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
    approval = ApprovalDecision(
        decision="approve",
        actor="integration-reviewer",
        justification="reviewed the exact target-to-evidence allocation",
        approved_at=datetime(2026, 8, 30, 16, 0, tzinfo=UTC),
        approved_case_version=1,
        approved_case_id=case_id,
        action_ids=[action.action_id],
        action_bindings=[
            ApprovalBinding(
                action_id=action.action_id,
                action_digest=proposed_action_digest(action),
            )
        ],
    )
    changed = ProposedAction.model_validate(
        {
            **action.model_dump(mode="python"),
            "evidence_by_target": {
                "LOT-PROBABLE-160": ["EV-B"],
                "LOT-EXACT-170": ["EV-A"],
            },
        }
    )
    return {"action": action, "approval": approval, "changed": changed}


@pytest.mark.parametrize("gateway_kind", ["direct", "stdio"])
@pytest.mark.asyncio
async def test_target_evidence_mutation_after_approval_is_rejected_direct_and_stdio(
    tmp_path: Path,
    gateway_kind: str,
) -> None:
    case_id = f"CASE-TARGET-EVIDENCE-REJECT-{gateway_kind.upper()}"
    database = tmp_path / f"target-evidence-reject-{gateway_kind}.sqlite3"
    gateway = (
        DirectGateway(operations=OperationsService(storage_path=database))
        if gateway_kind == "direct"
        else StdioMCPGateway(_connections(database))
    )
    await gateway.create_case(**_case_kwargs(case_id))
    review = _target_evidence_review(case_id)

    expected_error = ApprovalRequiredError if gateway_kind == "direct" else Exception
    with pytest.raises(expected_error, match="approval binding"):
        await gateway.apply_inventory_hold(
            case_id=case_id,
            lot_ids=["LOT-PROBABLE-160", "LOT-EXACT-170"],
            proposed_action=review["changed"],
            approval=review["approval"],
            expected_case_version=1,
            idempotency_key="target-evidence-rejected",
        )


@pytest.mark.parametrize("gateway_kind", ["direct", "stdio"])
@pytest.mark.asyncio
async def test_target_evidence_change_conflicts_with_exact_replay_key_direct_and_stdio(
    tmp_path: Path,
    gateway_kind: str,
) -> None:
    case_id = f"CASE-TARGET-EVIDENCE-CONFLICT-{gateway_kind.upper()}"
    database = tmp_path / f"target-evidence-conflict-{gateway_kind}.sqlite3"
    gateway = (
        DirectGateway(operations=OperationsService(storage_path=database))
        if gateway_kind == "direct"
        else StdioMCPGateway(_connections(database))
    )
    await gateway.create_case(**_case_kwargs(case_id))
    review = _target_evidence_review(case_id)
    request = {
        "case_id": case_id,
        "lot_ids": ["LOT-PROBABLE-160", "LOT-EXACT-170"],
        "proposed_action": review["action"],
        "approval": review["approval"],
        "expected_case_version": 1,
        "idempotency_key": "target-evidence-replay",
    }

    first = await gateway.apply_inventory_hold(**request)
    assert await gateway.apply_inventory_hold(**request) == first
    changed_approval = review["approval"].model_copy(
        update={
            "action_bindings": (
                ApprovalBinding(
                    action_id=review["changed"].action_id,
                    action_digest=proposed_action_digest(review["changed"]),
                ),
            )
        }
    )
    expected_error = IdempotencyConflictError if gateway_kind == "direct" else Exception
    with pytest.raises(expected_error, match="idempotency key is bound"):
        await gateway.apply_inventory_hold(
            **{
                **request,
                "proposed_action": review["changed"],
                "approval": changed_approval,
            }
        )
