"""Shared MCP serialization and approval construction."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from recallops.models import ActionDecision, ApprovalDecision


def approval_from_input(
    decision: ActionDecision,
    actor: str,
    justification: str,
    approved_at: datetime,
    approved_case_version: int,
    action_ids: list[str],
) -> ApprovalDecision:
    return ApprovalDecision(
        decision=decision,
        actor=actor,
        justification=justification,
        approved_at=approved_at,
        approved_case_version=approved_case_version,
        action_ids=action_ids,
    )


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value
