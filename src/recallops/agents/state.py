"""Durable, JSON-only state contracts for the RecallOps LangGraph."""

from __future__ import annotations

import json
from typing import Annotated, Any, TypedDict


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_dedupe(left: list[Any], right: list[Any]) -> list[Any]:
    """Append new JSON values while retaining first-seen order across replays."""
    merged: list[Any] = []
    seen: set[str] = set()
    for value in [*left, *right]:
        key = _canonical(value)
        if key not in seen:
            merged.append(value)
            seen.add(key)
    return merged


def merge_specialists(
    left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Merge fixed specialist outputs by name."""
    return {**left, **right}


class RecallOpsGraphState(TypedDict, total=False):
    """Checkpointed workflow state; every value must survive strict JSON encoding."""

    case_id: str
    thread_id: str
    recall_number: str
    question: str
    scope_lot_ids: list[str]
    status: str
    case_version: int
    source_mode: str
    plan: dict[str, Any]
    specialist_outputs: Annotated[dict[str, dict[str, Any]], merge_specialists]
    rag_result: dict[str, Any]
    rag_state: dict[str, Any]
    official_evidence: dict[str, Any]
    synthetic_evidence: dict[str, Any]
    recall: dict[str, Any]
    recall_predicate: dict[str, Any]
    candidate_products: list[dict[str, Any]]
    candidate_lots: list[dict[str, Any]]
    match_decisions: list[dict[str, Any]]
    confirmed_lot_ids: list[str]
    ambiguous_lot_ids: list[str]
    trace_events: list[dict[str, Any]]
    inventory_positions: list[dict[str, Any]]
    forward_traces: dict[str, list[str]]
    backward_traces: dict[str, list[str]]
    reconciliations: list[dict[str, Any]]
    required_facilities: list[str]
    evidence_by_lot: dict[str, list[str]]
    evidence_by_facility: dict[str, list[str]]
    evidence_gaps: Annotated[list[str], stable_dedupe]
    warnings: Annotated[list[str], stable_dedupe]
    verification: dict[str, Any]
    current_action: dict[str, Any]
    action_queue: list[dict[str, Any]]
    remaining_action_types: list[str]
    action_digest: str
    review_packet: dict[str, Any]
    review_history: Annotated[list[dict[str, Any]], stable_dedupe]
    approval: dict[str, Any]
    execution_request: dict[str, Any]
    execution_id: str
    idempotency_key: str
    write_receipts: Annotated[list[dict[str, Any]], stable_dedupe]
    acknowledgements: dict[str, bool]
    closure_outcome: dict[str, Any]
    failure_state: dict[str, Any]
    retry_state: dict[str, Any]
    watchdog: dict[str, Any]
    node_trace: Annotated[list[str], stable_dedupe]
    tool_trace: Annotated[list[dict[str, Any]], stable_dedupe]
    model_trace: Annotated[list[dict[str, Any]], stable_dedupe]
