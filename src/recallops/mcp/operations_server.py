"""Recall Operations MCP stdio server (approval-gated simulated writes)."""

from datetime import datetime

from fastmcp import FastMCP

from recallops.mcp.common import approval_from_input
from recallops.models import (
    ActionDecision,
    ApprovalBinding,
    AuditReceipt,
    Disposition,
    ProposedAction,
    Reconciliation,
)
from recallops.services.operations import OperationsService

mcp = FastMCP("Recall Operations MCP", instructions="Approval-gated simulated operations only.")
service = OperationsService()


def _approval(
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    approved_case_id: str,
    action_ids: list[str],
    action_bindings: list[ApprovalBinding],
):
    return approval_from_input(
        decision,
        actor,
        justification,
        approved_at,
        approved_case_version,
        approved_case_id,
        action_ids,
        action_bindings,
    )


@mcp.tool()
def create_case(
    case_id: str,
    recall_number: str,
    confirmed_lot_ids: list[str],
    trace_event_ids: list[str],
    required_facilities: list[str],
    reconciliation: list[Reconciliation],
    evidence_gaps: list[str],
    proposed_action: ProposedAction,
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    approved_case_id: str,
    action_ids: list[str],
    action_bindings: list[ApprovalBinding],
    expected_case_version: int,
    idempotency_key: str,
    question: str = "",
) -> AuditReceipt:
    return service.create_case(
        case_id=case_id,
        recall_number=recall_number,
        confirmed_lot_ids=confirmed_lot_ids,
        trace_event_ids=trace_event_ids,
        required_facilities=required_facilities,
        reconciliation=reconciliation,
        evidence_gaps=evidence_gaps,
        proposed_action=proposed_action,
        approval=_approval(
            decision,
            actor,
            justification,
            approved_at,
            approved_case_version,
            approved_case_id,
            action_ids,
            action_bindings,
        ),
        expected_case_version=expected_case_version,
        idempotency_key=idempotency_key,
        question=question,
    )


@mcp.tool()
def apply_inventory_hold(
    case_id: str,
    lot_ids: list[str],
    proposed_action: ProposedAction,
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    approved_case_id: str,
    action_ids: list[str],
    action_bindings: list[ApprovalBinding],
    expected_case_version: int,
    idempotency_key: str,
) -> AuditReceipt:
    return service.apply_inventory_hold(
        case_id=case_id,
        lot_ids=lot_ids,
        proposed_action=proposed_action,
        approval=_approval(
            decision,
            actor,
            justification,
            approved_at,
            approved_case_version,
            approved_case_id,
            action_ids,
            action_bindings,
        ),
        expected_case_version=expected_case_version,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def create_facility_tasks(
    case_id: str,
    facility_ids: list[str],
    proposed_action: ProposedAction,
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    approved_case_id: str,
    action_ids: list[str],
    action_bindings: list[ApprovalBinding],
    expected_case_version: int,
    idempotency_key: str,
) -> AuditReceipt:
    return service.create_facility_tasks(
        case_id=case_id,
        facility_ids=facility_ids,
        proposed_action=proposed_action,
        approval=_approval(
            decision,
            actor,
            justification,
            approved_at,
            approved_case_version,
            approved_case_id,
            action_ids,
            action_bindings,
        ),
        expected_case_version=expected_case_version,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def record_acknowledgment(
    case_id: str,
    facility_id: str,
    proposed_action: ProposedAction,
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    approved_case_id: str,
    action_ids: list[str],
    action_bindings: list[ApprovalBinding],
    expected_case_version: int,
    idempotency_key: str,
) -> AuditReceipt:
    return service.record_acknowledgment(
        case_id=case_id,
        facility_id=facility_id,
        proposed_action=proposed_action,
        approval=_approval(
            decision,
            actor,
            justification,
            approved_at,
            approved_case_version,
            approved_case_id,
            action_ids,
            action_bindings,
        ),
        expected_case_version=expected_case_version,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def record_disposition(
    case_id: str,
    lot_id: str,
    disposition: Disposition,
    evidence_id: str,
    proposed_action: ProposedAction,
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    approved_case_id: str,
    action_ids: list[str],
    action_bindings: list[ApprovalBinding],
    expected_case_version: int,
    idempotency_key: str,
) -> AuditReceipt:
    return service.record_disposition(
        case_id=case_id,
        lot_id=lot_id,
        disposition=disposition,
        evidence_id=evidence_id,
        proposed_action=proposed_action,
        approval=_approval(
            decision,
            actor,
            justification,
            approved_at,
            approved_case_version,
            approved_case_id,
            action_ids,
            action_bindings,
        ),
        expected_case_version=expected_case_version,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def close_case(
    case_id: str,
    proposed_action: ProposedAction,
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    approved_case_id: str,
    action_ids: list[str],
    action_bindings: list[ApprovalBinding],
    expected_case_version: int,
    idempotency_key: str,
) -> AuditReceipt:
    return service.close_case(
        case_id=case_id,
        proposed_action=proposed_action,
        approval=_approval(
            decision,
            actor,
            justification,
            approved_at,
            approved_case_version,
            approved_case_id,
            action_ids,
            action_bindings,
        ),
        expected_case_version=expected_case_version,
        idempotency_key=idempotency_key,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
