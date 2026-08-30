"""Shared MCP serialization and approval construction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from recallops.models import ApprovalDecision


def approval_from_input(decision: str, actor: str, justification: str) -> ApprovalDecision:
    return ApprovalDecision(
        decision=decision, actor=actor, justification=justification, approved_at=datetime.now(UTC)
    )


def jsonable(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value
