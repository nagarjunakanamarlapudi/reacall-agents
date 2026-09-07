import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

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
    _workflow_authorization_broker,
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
TRACEABILITY = TraceabilityService()


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
    if evidence_ids is not None:
        evidence_by_target = {target_id: evidence_ids for target_id in target_ids}
    else:
        evidence_by_target = {}
        for target_id in target_ids:
            if target_id.startswith("LOT-"):
                identifiers = sorted(
                    {event["event_id"] for event in TRACEABILITY.trace_forward(target_id)}
                    | {
                        position["position_id"]
                        for position in TRACEABILITY.get_inventory(target_id)
                    }
                )
            else:
                identifiers = [
                    event["event_id"]
                    for event in TRACEABILITY.dataset["events"]
                    if target_id in {event.get("from_facility"), event.get("to_facility")}
                ][:1]
            evidence_by_target[target_id] = identifiers or [f"EVIDENCE-{target_id}"]
    action_evidence = list(
        dict.fromkeys(
            identifier for target_id in target_ids for identifier in evidence_by_target[target_id]
        )
    )
    action = ProposedAction(
        action_id=f"{case_id}-{action_type}-{version}",
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids,
        rationale=f"Reviewed {action_type} through MCP.",
        evidence_ids=action_evidence,
        evidence_by_target=evidence_by_target,
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
    return TRACEABILITY.reconcile_units("LOT-PROBABLE-160")


def _case_kwargs_for_lot(
    case_id: str,
    lot_id: str,
    *,
    recall_number: str = "H-1230-2026",
    idempotency_key: str = "mcp-create",
) -> dict:
    events = TRACEABILITY.trace_forward(lot_id)
    reconciliation = TRACEABILITY.reconcile_units(lot_id)
    evidence_gaps = (
        [f"{lot_id}: {reconciliation.unaccounted} unaccounted units"]
        if reconciliation.unaccounted
        else []
    )
    payload = {
        "case_id": case_id,
        "recall_number": recall_number,
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
        "reconciliation": [reconciliation],
        "evidence_gaps": evidence_gaps,
        "expected_case_version": 0,
        "idempotency_key": idempotency_key,
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


def _case_kwargs(case_id: str = "CASE-MCP-INTEGRATION") -> dict:
    return _case_kwargs_for_lot(case_id, "LOT-PROBABLE-160")


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


class WorkflowGrantedGateway:
    """Drive gateway writes through a real active checkpoint fence and one-use grant."""

    def __init__(self, gateway, operations: OperationsService) -> None:
        self.gateway = gateway
        self.operations = operations

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    async def _write(self, name: str, kwargs: dict) -> dict:
        case_id = kwargs["case_id"]
        expected = kwargs["expected_case_version"]
        key = kwargs["idempotency_key"]
        if self.operations.get_receipt(key) is not None:
            return await getattr(self.gateway, name)(**kwargs, execution_grant="completed-replay")
        state = self.operations.get_case(case_id)
        thread_id = kwargs.get("thread_id") or (state.thread_id if state else case_id)
        with sqlite3.connect(self.operations.storage_path) as connection:
            owner_row = connection.execute(
                "SELECT owner_token FROM workflow_identities WHERE case_id=?", (case_id,)
            ).fetchone()
        owner = owner_row[0] if owner_row and owner_row[0] else str(uuid4())
        broker = _workflow_authorization_broker(self.operations, owner)
        if self.operations.get_thread_for_case(case_id) is None:
            broker.reserve_workflow_identity(case_id, thread_id)
        with sqlite3.connect(self.operations.storage_path) as connection:
            head = connection.execute(
                "SELECT checkpoint_head FROM workflow_identities WHERE case_id=?", (case_id,)
            ).fetchone()[0]
        request_digest = hashlib.sha256(
            f"{case_id}:{thread_id}:{head}:{name}:{expected}:{key}".encode()
        ).hexdigest()
        attempt = str(uuid4())
        execution_id = f"MCP-EXECUTION:{name}:{expected}:{key}"
        execution_request_digest = hashlib.sha256(
            f"mcp-execution:{case_id}:{name}:{expected}:{key}".encode()
        ).hexdigest()
        broker.claim_workflow_mutation(
            case_id,
            thread_id,
            head,
            attempt,
            request_digest,
            execution_id=execution_id,
            execution_request_digest=execution_request_digest,
        )
        fields = {
            "create_case": (
                "recall_number",
                "question",
                "thread_id",
                "confirmed_lot_ids",
                "trace_event_ids",
                "required_facilities",
                "reconciliation",
                "evidence_gaps",
            ),
            "apply_inventory_hold": ("lot_ids",),
            "create_facility_tasks": ("facility_ids",),
            "record_acknowledgment": ("facility_id",),
            "record_disposition": ("lot_id", "disposition", "evidence_id"),
            "close_case": (),
        }[name]
        details = {}
        for field in fields:
            if field == "question":
                details[field] = kwargs.get(field, "")
            elif field == "thread_id":
                details[field] = thread_id
            elif field == "reconciliation":
                details[field] = [
                    Reconciliation.model_validate(item).model_dump(mode="json")
                    for item in kwargs[field]
                ]
            else:
                details[field] = kwargs[field]
        action = kwargs["proposed_action"]
        approval = kwargs["approval"]
        try:
            grant = broker.issue_workflow_execution_grant(
                case_id=case_id,
                thread_id=thread_id,
                proposed_action=action,
                approval=approval,
                expected_case_version=expected,
                idempotency_key=key,
                execution_id=execution_id,
                execution_request_digest=execution_request_digest,
                details=details,
                target_ids=list(action.target_ids),
                evidence_ids=(
                    list(kwargs["trace_event_ids"])
                    if name == "create_case"
                    else [kwargs["evidence_id"]]
                    if name == "record_disposition"
                    else None
                ),
            )
            receipt = await getattr(self.gateway, name)(**kwargs, execution_grant=grant)
        except BaseException:
            broker.release_workflow_mutation(case_id, thread_id, head, attempt, request_digest)
            raise
        broker.advance_workflow_mutation(
            case_id,
            thread_id,
            head,
            f"MCP-CHECKPOINT:{case_id}:{receipt['case_version']}:{receipt['receipt_id']}",
            attempt,
            request_digest,
        )
        return receipt

    async def create_case(self, **kwargs):
        return await self._write("create_case", kwargs)

    async def apply_inventory_hold(self, **kwargs):
        return await self._write("apply_inventory_hold", kwargs)

    async def create_facility_tasks(self, **kwargs):
        return await self._write("create_facility_tasks", kwargs)

    async def record_acknowledgment(self, **kwargs):
        return await self._write("record_acknowledgment", kwargs)

    async def record_disposition(self, **kwargs):
        return await self._write("record_disposition", kwargs)

    async def close_case(self, **kwargs):
        return await self._write("close_case", kwargs)


def _granted_gateway(kind: str, database: Path) -> WorkflowGrantedGateway:
    operations = OperationsService(storage_path=database)
    gateway = (
        DirectGateway(operations=operations)
        if kind == "direct"
        else StdioMCPGateway(_connections(database))
    )
    return WorkflowGrantedGateway(gateway, operations)


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
            "execution_grant",
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
    gateway = _granted_gateway("stdio", tmp_path / "operations.sqlite3")
    created = await gateway.create_case(**case_kwargs)

    restarted = MultiServerMCPClient(connections)
    restarted_tools = {tool.name: tool for tool in await restarted.get_tools()}
    replay = _tool_json(
        await restarted_tools["create_case"].ainvoke(
            {**create_payload, "execution_grant": "completed-replay"}
        )
    )
    assert replay == created


@pytest.mark.asyncio
async def test_direct_and_stdio_gateways_have_identical_method_matrix_shapes(
    tmp_path: Path,
) -> None:
    direct = _granted_gateway("direct", tmp_path / "direct.sqlite3")
    stdio = _granted_gateway("stdio", tmp_path / "stdio.sqlite3")

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
            "close_case",
            {
                "case_id": "CASE-MCP-INTEGRATION",
                **_review("CASE-MCP-INTEGRATION", "close_case", 5, []),
                "expected_case_version": 5,
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
    direct = _granted_gateway("direct", tmp_path / "approval-direct")
    stdio = _granted_gateway("stdio", tmp_path / "approval-stdio")

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
                "evidence_ids": ["EVIDENCE-LOT-PROBABLE-160"],
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
    gateways = [_granted_gateway("direct", direct_db), _granted_gateway("stdio", stdio_db)]
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
    gateway = _granted_gateway(gateway_kind, database)
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
    evidence = [event["event_id"] for event in TRACEABILITY.trace_forward("LOT-PROBABLE-160")][:2]
    action = ProposedAction(
        action_id=f"{case_id}-target-evidence",
        action_type="apply_inventory_hold",
        case_id=case_id,
        target_ids=["LOT-PROBABLE-160"],
        rationale="Hold each lot only for its reviewed evidence.",
        evidence_by_target={"LOT-PROBABLE-160": [evidence[0]]},
        evidence_ids=[evidence[0]],
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
            "evidence_by_target": {"LOT-PROBABLE-160": [evidence[1]]},
            "evidence_ids": [evidence[1]],
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
    gateway = _granted_gateway(gateway_kind, database)
    await gateway.create_case(**_case_kwargs(case_id))
    review = _target_evidence_review(case_id)

    expected_error = ApprovalRequiredError if gateway_kind == "direct" else Exception
    with pytest.raises(expected_error, match="approval binding"):
        await gateway.apply_inventory_hold(
            case_id=case_id,
            lot_ids=["LOT-PROBABLE-160"],
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
    gateway = _granted_gateway(gateway_kind, database)
    await gateway.create_case(**_case_kwargs(case_id))
    review = _target_evidence_review(case_id)
    request = {
        "case_id": case_id,
        "lot_ids": ["LOT-PROBABLE-160"],
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


@pytest.mark.parametrize(
    ("recall_number", "lot_id"),
    [
        ("NOT-A-REAL-RECALL", "LOT-PROBABLE-160"),
        ("H-1230-2026", "LOT-AMBIG-175"),
        ("H-1230-2026", "LOT-REJECT-190"),
    ],
)
@pytest.mark.asyncio
async def test_stdio_recomputes_recall_scope_before_case_creation(
    tmp_path: Path,
    recall_number: str,
    lot_id: str,
) -> None:
    database = tmp_path / f"authority-{lot_id}.sqlite3"
    gateway = _granted_gateway("stdio", database)
    request = _case_kwargs_for_lot(
        f"CASE-STDIO-AUTHORITY-{lot_id}",
        lot_id,
        recall_number=recall_number,
        idempotency_key=f"stdio-authority-{lot_id}",
    )

    with pytest.raises(Exception, match="recall|predicate|eligible|classification"):
        await gateway.create_case(**request)

    assert OperationsService(storage_path=database).get_case(request["case_id"]) is None


@pytest.mark.asyncio
async def test_stdio_rejects_out_of_scope_hold_targets_and_evidence(tmp_path: Path) -> None:
    database = tmp_path / "stdio-hold-authority.sqlite3"
    case_id = "CASE-STDIO-HOLD-AUTHORITY"
    gateway = _granted_gateway("stdio", database)
    await gateway.create_case(**_case_kwargs(case_id))

    with pytest.raises(Exception, match="case|eligible|scope"):
        await gateway.apply_inventory_hold(
            case_id=case_id,
            lot_ids=["LOT-EXACT-170"],
            **_review(case_id, "apply_inventory_hold", 1, ["LOT-EXACT-170"]),
            expected_case_version=1,
            idempotency_key="stdio-unrelated-hold",
        )
    with pytest.raises(Exception, match="evidence"):
        await gateway.apply_inventory_hold(
            case_id=case_id,
            lot_ids=["LOT-PROBABLE-160"],
            **_review(
                case_id,
                "apply_inventory_hold",
                1,
                ["LOT-PROBABLE-160"],
                evidence_ids=["FAKE-EVIDENCE"],
            ),
            expected_case_version=1,
            idempotency_key="stdio-fake-evidence-hold",
        )

    assert OperationsService(storage_path=database).get_case(case_id).case_version == 1


@pytest.mark.asyncio
async def test_stdio_closure_requires_persisted_holds_before_completion(tmp_path: Path) -> None:
    database = tmp_path / "stdio-no-hold-close.sqlite3"
    case_id = "CASE-STDIO-NO-HOLD"
    gateway = _granted_gateway("stdio", database)
    await gateway.create_case(**_case_kwargs(case_id))
    with sqlite3.connect(database) as connection:
        for facility_id in ("DC-SOUTH", "STORE-03"):
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, 'acknowledged')", (case_id, facility_id)
            )
            connection.execute(
                "INSERT INTO acknowledgements VALUES (?, ?, 1)", (case_id, facility_id)
            )

    with pytest.raises(Exception, match="hold"):
        await gateway.close_case(
            case_id=case_id,
            **_review(case_id, "close_case", 1, []),
            expected_case_version=1,
            idempotency_key="stdio-no-hold-close",
        )


@pytest.mark.asyncio
async def test_stdio_execution_grant_is_single_request_scoped(tmp_path: Path) -> None:
    database = tmp_path / "stdio-grant-scope.sqlite3"
    case_id = "CASE-STDIO-GRANT-SCOPE"
    workflow_gateway = _granted_gateway("stdio", database)
    create_request = _case_kwargs(case_id)
    created = await workflow_gateway.create_case(**create_request)
    with sqlite3.connect(database) as connection:
        create_grant = connection.execute(
            "SELECT grant_token FROM execution_grants WHERE action_type='create_case'"
        ).fetchone()[0]
    raw_gateway = StdioMCPGateway(_connections(database))

    assert await raw_gateway.create_case(**create_request, execution_grant=create_grant) == created
    hold_request = {
        "case_id": case_id,
        "lot_ids": ["LOT-PROBABLE-160"],
        **_review(case_id, "apply_inventory_hold", 1, ["LOT-PROBABLE-160"]),
        "expected_case_version": 1,
        "idempotency_key": "stdio-valid-hold",
    }
    with pytest.raises(Exception, match="execution grant"):
        await raw_gateway.apply_inventory_hold(
            **{**hold_request, "idempotency_key": "stdio-cross-action"},
            execution_grant=create_grant,
        )

    await workflow_gateway.apply_inventory_hold(**hold_request)
    with sqlite3.connect(database) as connection:
        hold_grant = connection.execute(
            "SELECT grant_token FROM execution_grants WHERE action_type='apply_inventory_hold'"
        ).fetchone()[0]
    with pytest.raises(Exception, match="execution grant"):
        await raw_gateway.apply_inventory_hold(
            case_id=case_id,
            lot_ids=["LOT-PROBABLE-160"],
            **_review(case_id, "apply_inventory_hold", 2, ["LOT-PROBABLE-160"]),
            expected_case_version=2,
            idempotency_key="stdio-cross-version",
            execution_grant=hold_grant,
        )
    assert OperationsService(storage_path=database).get_case(case_id).case_version == 2


@pytest.mark.asyncio
async def test_stdio_disposition_is_append_only_and_enables_valid_closure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stdio-disposition.sqlite3"
    case_id = "CASE-STDIO-DISPOSITION"
    lot_id = "LOT-EXACT-170"
    gateway = _granted_gateway("stdio", database)
    create_request = _case_kwargs_for_lot(case_id, lot_id)
    await gateway.create_case(**create_request)
    base_trace_ids = list(create_request["trace_event_ids"])
    await gateway.apply_inventory_hold(
        case_id=case_id,
        lot_ids=[lot_id],
        **_review(case_id, "apply_inventory_hold", 1, [lot_id]),
        expected_case_version=1,
        idempotency_key="stdio-disposition-hold",
    )
    source_evidence_id = create_request["reconciliation"][0].component_evidence["unaccounted"][0]
    await gateway.record_disposition(
        case_id=case_id,
        lot_id=lot_id,
        disposition="dispose_unaccounted",
        evidence_id=source_evidence_id,
        **_review(
            case_id,
            "record_disposition",
            2,
            [lot_id],
            evidence_ids=[source_evidence_id],
        ),
        expected_case_version=2,
        idempotency_key="stdio-disposition-event",
    )
    facilities = create_request["required_facilities"]
    await gateway.create_facility_tasks(
        case_id=case_id,
        facility_ids=facilities,
        **_review(case_id, "create_facility_tasks", 3, facilities),
        expected_case_version=3,
        idempotency_key="stdio-disposition-tasks",
    )
    version = 4
    for facility_id in facilities:
        await gateway.record_acknowledgment(
            case_id=case_id,
            facility_id=facility_id,
            **_review(case_id, "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"stdio-disposition-ack-{facility_id}",
        )
        version += 1
    await gateway.close_case(
        case_id=case_id,
        **_review(case_id, "close_case", version, []),
        expected_case_version=version,
        idempotency_key="stdio-disposition-close",
    )

    state = OperationsService(storage_path=database).get_case(case_id)
    assert state.status == "closed"
    assert state.trace_event_ids == base_trace_ids
    assert state.reconciliation[0].unaccounted == 0
    assert len(state.disposition_events) == 1
