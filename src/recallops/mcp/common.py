"""Shared MCP serialization and approval construction."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from recallops.models import ActionDecision, ApprovalBinding, ApprovalDecision


def approval_from_input(
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    approved_case_id: str,
    action_ids: list[str],
    action_bindings: list[ApprovalBinding],
) -> ApprovalDecision:
    return ApprovalDecision(
        decision=decision,
        actor=actor,
        justification=justification,
        approved_at=approved_at,
        approved_case_version=approved_case_version,
        approved_case_id=approved_case_id,
        action_ids=action_ids,
        action_bindings=action_bindings,
    )


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value
