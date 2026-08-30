"""Recall Operations MCP stdio server (approval-gated simulated writes)."""

from fastmcp import FastMCP

from recallops.mcp.common import approval_from_input, jsonable
from recallops.paths import OPERATIONS_STATE_PATH
from recallops.services.operations import OperationsService

mcp = FastMCP("Recall Operations MCP", instructions="Approval-gated simulated operations only.")
service = OperationsService(storage_path=OPERATIONS_STATE_PATH)


def _approval(decision: str, actor: str, justification: str):
    return approval_from_input(decision, actor, justification)


@mcp.tool()
def create_case(
    case_id: str,
    recall_number: str,
    decision: str,
    actor: str,
    justification: str,
    expected_case_version: int,
    idempotency_key: str,
    question: str = "",
) -> dict:
    return jsonable(
        service.create_case(
            case_id=case_id,
            recall_number=recall_number,
            approval=_approval(decision, actor, justification),
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
            question=question,
        )
    )


@mcp.tool()
def apply_inventory_hold(
    case_id: str,
    lot_ids: list[str],
    decision: str,
    actor: str,
    justification: str,
    expected_case_version: int,
    idempotency_key: str,
) -> dict:
    return jsonable(
        service.apply_inventory_hold(
            case_id=case_id,
            lot_ids=lot_ids,
            approval=_approval(decision, actor, justification),
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
        )
    )


@mcp.tool()
def create_facility_tasks(
    case_id: str,
    facility_ids: list[str],
    decision: str,
    actor: str,
    justification: str,
    expected_case_version: int,
    idempotency_key: str,
) -> dict:
    return jsonable(
        service.create_facility_tasks(
            case_id=case_id,
            facility_ids=facility_ids,
            approval=_approval(decision, actor, justification),
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
        )
    )


@mcp.tool()
def record_acknowledgment(
    case_id: str,
    facility_id: str,
    decision: str,
    actor: str,
    justification: str,
    expected_case_version: int,
    idempotency_key: str,
) -> dict:
    return jsonable(
        service.record_acknowledgment(
            case_id=case_id,
            facility_id=facility_id,
            approval=_approval(decision, actor, justification),
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
        )
    )


@mcp.tool()
def record_disposition(
    case_id: str,
    lot_id: str,
    disposition: str,
    decision: str,
    actor: str,
    justification: str,
    expected_case_version: int,
    idempotency_key: str,
) -> dict:
    return jsonable(
        service.record_disposition(
            case_id=case_id,
            lot_id=lot_id,
            disposition=disposition,
            approval=_approval(decision, actor, justification),
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
        )
    )


@mcp.tool()
def close_case(
    case_id: str,
    decision: str,
    actor: str,
    justification: str,
    expected_case_version: int,
    idempotency_key: str,
) -> dict:
    return jsonable(
        service.close_case(
            case_id=case_id,
            approval=_approval(decision, actor, justification),
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
        )
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
