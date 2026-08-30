"""End-to-end contracts for the durable RecallOps LangGraph runtime."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest


def _operation_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
    finally:
        connection.close()


def _bound_response(
    pending: dict,
    *,
    decision: str,
    actor: str = "food-safety-lead",
    justification: str = "Reviewed against the cited evidence and reconciliation.",
) -> dict:
    response = {
        key: pending[key]
        for key in ("kind", "case_id", "thread_id", "case_version", "action_digest")
    }
    response["action_id"] = (
        pending["action_id"] if "action_id" in pending else pending["action"]["action_id"]
    )
    for key in ("execution_id", "idempotency_key"):
        if key in pending:
            response[key] = pending[key]
    response["decision"] = decision
    if decision == "approve":
        response.update(actor=actor, justification=justification)
    return response


@pytest.mark.asyncio
async def test_initial_investigation_stops_at_bound_review_with_zero_writes(
    tmp_path: Path,
) -> None:
    """Break caught: investigation skips a specialist or writes before dual consent."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question=(
                "Which lots and facilities are affected by H-1230-2026, and what containment "
                "is safe?"
            ),
            case_id="CASE-FLAGSHIP",
            thread_id="THREAD-FLAGSHIP",
        )

    assert result.next_nodes == ("action_review",)
    assert result.pending_interrupt is not None
    assert result.pending_interrupt["kind"] == "action_review"
    assert result.pending_interrupt["case_id"] == "CASE-FLAGSHIP"
    assert result.pending_interrupt["thread_id"] == "THREAD-FLAGSHIP"
    assert result.pending_interrupt["case_version"] == 0
    assert result.pending_interrupt["action"]["action_type"] == "create_case"
    assert result.pending_interrupt["action_digest"]
    assert result.pending_interrupt["official_evidence"]["provenance"] == (
        "OFFICIAL_OPENFDA_SNAPSHOT"
    )
    assert result.pending_interrupt["synthetic_evidence"]["origin"] == (
        "SYNTHETIC_RETAILER_DIGITAL_TWIN"
    )
    assert result.pending_interrupt["rag_citations"]
    assert "rag_gaps" in result.pending_interrupt
    assert result.case["status"] == "review_required"
    assert result.case["node_trace"] == [
        "intake",
        "retrieve_context",
        "plan",
        "regulatory_intake",
        "product_lot_match",
        "trace_forward_backward",
        "reconcile",
        "containment_draft",
        "verify",
        "prepare_action_review",
        "action_review",
    ]
    assert json.loads(json.dumps(result.case)) == result.case
    assert _operation_count(operations_path) == 0


@pytest.mark.asyncio
async def test_restart_restores_review_and_confirmation_without_rerunning_reasoning(
    tmp_path: Path,
) -> None:
    """Break caught: reopening SQLite restarts investigation or loses the pending consent."""
    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Trace the affected recall lots before proposing containment.",
            case_id="CASE-RESTART",
            thread_id="THREAD-RESTART",
        )
        original_trace = review.case["tool_trace"]

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        restored_review = await runtime.get_case(thread_id="THREAD-RESTART")
        assert restored_review == review
        confirmation = await runtime.resume_case(
            thread_id="THREAD-RESTART",
            response=_bound_response(restored_review.pending_interrupt, decision="approve"),
        )
        assert confirmation.case["tool_trace"] == original_trace

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        restored_confirmation = await runtime.get_case(thread_id="THREAD-RESTART")
        assert restored_confirmation == confirmation
        created = await runtime.resume_case(
            thread_id="THREAD-RESTART",
            response=_bound_response(
                restored_confirmation.pending_interrupt,
                decision="confirm",
            ),
        )

    assert created.case["case_version"] == 1
    assert created.case["tool_trace"] == original_trace
    assert _operation_count(operations_path) == 1


@pytest.mark.asyncio
async def test_lost_write_response_recovers_with_same_key_and_one_logical_receipt(
    tmp_path: Path,
) -> None:
    """Break caught: unknown write outcome gets a new key or duplicates a logical write."""
    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Investigate and contain the affected lots.",
            case_id="CASE-UNKNOWN",
            thread_id="THREAD-UNKNOWN",
        )
        confirmation = await runtime.resume_case(
            thread_id="THREAD-UNKNOWN",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )
        original_key = confirmation.pending_interrupt["idempotency_key"]
        runtime.inject_failure("lost_write_response")
        unknown = await runtime.resume_case(
            thread_id="THREAD-UNKNOWN",
            response=_bound_response(confirmation.pending_interrupt, decision="confirm"),
        )

    assert unknown.case["status"] == "write_outcome_unknown"
    assert unknown.pending_interrupt["kind"] == "write_outcome_recovery"
    assert unknown.pending_interrupt["idempotency_key"] == original_key
    assert _operation_count(operations_path) == 1

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        restored = await runtime.get_case(thread_id="THREAD-UNKNOWN")
        assert restored == unknown
        recovered = await runtime.resume_case(
            thread_id="THREAD-UNKNOWN",
            response=_bound_response(restored.pending_interrupt, decision="retry"),
        )

    assert recovered.case["case_version"] == 1
    assert recovered.case["write_receipts"][0]["idempotency_key"] == original_key
    assert _operation_count(operations_path) == 1


@pytest.mark.asyncio
async def test_dual_consent_executes_one_create_then_one_hold_version(
    tmp_path: Path,
) -> None:
    """Break caught: approval writes immediately or batches create and hold together."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Identify affected lots, trace facilities, and contain H-1230-2026.",
            case_id="CASE-CONSENT",
            thread_id="THREAD-CONSENT",
        )
        approved = await runtime.resume_case(
            thread_id="THREAD-CONSENT",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )

        assert approved.pending_interrupt["kind"] == "execution_confirmation"
        assert approved.case["status"] == "approved_pending_execution"
        assert _operation_count(operations_path) == 0

        created = await runtime.resume_case(
            thread_id="THREAD-CONSENT",
            response=_bound_response(approved.pending_interrupt, decision="confirm"),
        )
        assert created.pending_interrupt["kind"] == "action_review"
        assert created.pending_interrupt["case_version"] == 1
        assert created.pending_interrupt["action"]["action_type"] == "apply_inventory_hold"
        assert [item["action_type"] for item in created.case["write_receipts"]] == ["create_case"]
        assert _operation_count(operations_path) == 1

        hold_approved = await runtime.resume_case(
            thread_id="THREAD-CONSENT",
            response=_bound_response(created.pending_interrupt, decision="approve"),
        )
        held = await runtime.resume_case(
            thread_id="THREAD-CONSENT",
            response=_bound_response(hold_approved.pending_interrupt, decision="confirm"),
        )

    assert held.pending_interrupt is None
    assert held.case["case_version"] == 2
    assert held.case["status"] == "open_closure_blocked"
    assert [item["action_type"] for item in held.case["write_receipts"]] == [
        "create_case",
        "apply_inventory_hold",
    ]
    assert _operation_count(operations_path) == 2


@pytest.mark.asyncio
async def test_wrong_resume_binding_leaves_checkpoint_and_operations_unchanged(
    tmp_path: Path,
) -> None:
    """Break caught: a stale or cross-thread decision advances the durable graph."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Investigate and contain the affected lots.",
            case_id="CASE-BINDING",
            thread_id="THREAD-BINDING",
        )
        before = await runtime.get_case(thread_id="THREAD-BINDING")
        assert before is not None

        for field, wrong in (
            ("case_id", "CASE-OTHER"),
            ("thread_id", "THREAD-OTHER"),
            ("case_version", 7),
            ("action_id", "ACTION-OTHER"),
            ("action_digest", "0" * 64),
        ):
            response = _bound_response(review.pending_interrupt, decision="approve")
            response[field] = wrong
            with pytest.raises(ValueError, match=field):
                await runtime.resume_case(thread_id="THREAD-BINDING", response=response)
            after = await runtime.get_case(thread_id="THREAD-BINDING")
            assert after == before
            assert _operation_count(operations_path) == 0

        execution = await runtime.resume_case(
            thread_id="THREAD-BINDING",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )
        execution_before = await runtime.get_case(thread_id="THREAD-BINDING")
        assert execution_before is not None
        for field, wrong in (
            ("execution_id", "EXECUTION-OTHER"),
            ("idempotency_key", "KEY-OTHER"),
            ("action_digest", "f" * 64),
        ):
            response = _bound_response(execution.pending_interrupt, decision="confirm")
            response[field] = wrong
            with pytest.raises(ValueError, match=field):
                await runtime.resume_case(thread_id="THREAD-BINDING", response=response)
            assert await runtime.get_case(thread_id="THREAD-BINDING") == execution_before
            assert _operation_count(operations_path) == 0


@pytest.mark.asyncio
async def test_edit_reverifies_new_digest_and_reject_never_exposes_execution(
    tmp_path: Path,
) -> None:
    """Break caught: edited bytes retain stale approval or rejection reaches a write gate."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        original = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Investigate the scoped recall.",
            case_id="CASE-EDIT",
            thread_id="THREAD-EDIT",
        )
        edited_action = deepcopy(original.pending_interrupt["action"])
        edited_action["rationale"] = (
            "Create the simulated case only after reviewing the cited authoritative trace."
        )
        edit_response = _bound_response(original.pending_interrupt, decision="edit")
        edit_response["edited_action"] = edited_action
        edited = await runtime.resume_case(
            thread_id="THREAD-EDIT",
            response=edit_response,
        )

        assert edited.pending_interrupt["kind"] == "action_review"
        assert edited.pending_interrupt["action"]["rationale"] == edited_action["rationale"]
        assert (
            edited.pending_interrupt["action_digest"] != original.pending_interrupt["action_digest"]
        )
        assert edited.case["verification"]["edited_action_reverified"] is True

        rejected = await runtime.resume_case(
            thread_id="THREAD-EDIT",
            response=_bound_response(edited.pending_interrupt, decision="reject"),
        )

    assert rejected.pending_interrupt is None
    assert rejected.case["status"] == "open"
    assert "execution_confirmation" not in rejected.next_nodes
    assert _operation_count(operations_path) == 0
