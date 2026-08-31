from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from recallops.ui.adapter import DurableRuntimeAdapter
from recallops.ui.presenters import (
    APPROVAL_JUSTIFICATION,
    build_match_rows,
    build_retrieval_rows,
    can_simulate,
    reduce_case_snapshot,
)


def _receipt_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
    finally:
        connection.close()


def test_durable_adapter_exposes_only_real_runtime_transports(tmp_path: Path) -> None:
    direct = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "direct-checkpoints.sqlite3",
        operations_path=tmp_path / "direct-operations.sqlite3",
    )
    assert direct.transport == "direct"
    assert direct.transport_label == "direct MCP gateway"

    stdio = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "stdio-checkpoints.sqlite3",
        operations_path=tmp_path / "stdio-operations.sqlite3",
        transport="stdio",
    )
    assert stdio.transport == "stdio"
    assert stdio.transport_label == "stdio MCP subprocesses"

    with pytest.raises(ValueError, match="direct.*stdio"):
        DurableRuntimeAdapter(
            checkpoint_path=tmp_path / "bad-checkpoints.sqlite3",
            operations_path=tmp_path / "bad-operations.sqlite3",
            transport="pretend",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_durable_adapter_projects_runtime_and_survives_reopen(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoints.sqlite3"
    operations = tmp_path / "operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=checkpoint,
        operations_path=operations,
    )
    opened = await adapter.open_case("H-1230-2026")
    assert opened["runtime_mode"] == "Durable LangGraph + SQLite"
    assert opened["matches"] == []

    reviewed = await adapter.run_investigation(opened)
    assert reviewed["pending_interrupt"]["kind"] == "action_review"
    assert reviewed["pending_interrupt"]["scope"] == "create_case"
    assert reviewed["status"] == "review_required"
    assert len(build_match_rows(reduce_case_snapshot(reviewed))) == 144
    retrieval = build_retrieval_rows(reduce_case_snapshot(reviewed))
    assert retrieval and retrieval[0].query
    assert "BM25" in retrieval[0].sparse
    assert _receipt_count(operations) == 0

    approved = await adapter.resume_review(
        reviewed,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    assert approved["pending_interrupt"]["kind"] == "execution_confirmation"
    assert can_simulate(reduce_case_snapshot(approved))[0]
    assert _receipt_count(operations) == 0

    restored_adapter = DurableRuntimeAdapter(
        checkpoint_path=checkpoint,
        operations_path=operations,
    )
    restored = await restored_adapter.load_case(approved["thread_id"])
    assert restored["checkpoint_id"] == approved["checkpoint_id"]
    assert restored["pending_interrupt"] == approved["pending_interrupt"]

    created = await restored_adapter.simulate_approved_actions(restored)
    assert created["case_version"] == 1
    assert created["receipts"][0]["action_type"] == "create_case"
    assert created["pending_interrupt"]["scope"] == "apply_inventory_hold"
    assert _receipt_count(operations) == 1


@pytest.mark.asyncio
async def test_durable_failure_selector_arms_only_supported_runtime_failure(tmp_path: Path) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    opened = await adapter.open_case("H-1230-2026")
    armed = await adapter.inject_failure(opened, "openFDA unavailable → labelled frozen snapshot")
    assert armed["failure_result"]["status"] == "armed"
    assert armed["failure_result"]["next_step"] == "Run investigation"
    investigated = await adapter.run_investigation(armed)
    assert any(
        "pinned OFFICIAL_OPENFDA_SNAPSHOT fallback" in warning
        for warning in investigated["warnings"]
    )

    with pytest.raises(ValueError, match="fresh case"):
        await adapter.inject_failure(
            investigated, "read timeout/429 → bounded retry then circuit-open/fallback"
        )


@pytest.mark.asyncio
async def test_durable_adapter_rejects_coercive_case_and_thread_bindings(tmp_path: Path) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    with pytest.raises(ValueError, match="nonblank strings"):
        await adapter.run_investigation(
            {
                "recall_number": "H-1230-2026",
                "question": "Investigate safely.",
                "case_id": 123,
                "thread_id": ["THREAD"],
                "case_version": 0,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"question": 123},
        {"scope_lot_ids": [True]},
        {"scope_lot_ids": "LOT-EXACT-170"},
    ],
)
async def test_durable_adapter_rejects_coercive_investigation_inputs(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    opened = await adapter.open_case("H-1230-2026")
    opened.update(overrides)
    with pytest.raises(ValueError, match="question|scope_lot_ids"):
        await adapter.run_investigation(opened)


@pytest.mark.asyncio
async def test_durable_lost_response_is_observed_then_recovers_with_same_key(
    tmp_path: Path,
) -> None:
    operations = tmp_path / "operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations,
    )
    reviewed = await adapter.run_investigation(await adapter.open_case("H-1230-2026"))
    approved = await adapter.resume_review(
        reviewed,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    original_key = approved["pending_interrupt"]["idempotency_key"]
    armed = await adapter.inject_failure(approved, "lost write response → same-key replay")
    unknown = await adapter.simulate_approved_actions(armed)
    assert unknown["status"] == "write_outcome_unknown"
    assert unknown["pending_interrupt"]["kind"] == "write_outcome_recovery"
    assert unknown["pending_interrupt"]["idempotency_key"] == original_key
    assert unknown["failure_result"]["status"] == "observed"
    assert _receipt_count(operations) == 1

    recovered = await adapter.simulate_approved_actions(unknown)
    assert recovered["case_version"] == 1
    assert recovered["receipts"][0]["idempotency_key"] == original_key
    assert _receipt_count(operations) == 1


@pytest.mark.asyncio
async def test_durable_repeated_progress_surfaces_fail_closed_state(tmp_path: Path) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    opened = await adapter.open_case("H-1230-2026")
    armed = await adapter.inject_failure(opened, "repeated graph progress → watchdog escalation")
    escalated = await adapter.run_investigation(armed)
    assert escalated["status"] == "escalated"
    assert escalated["pending_interrupt"] is None
    assert escalated["failure_result"]["status"] == "observed"
    assert escalated["watchdog"]["repeat_count"] == 2


@pytest.mark.asyncio
async def test_closure_presentation_preserves_review_and_closed_lifecycle_states(
    tmp_path: Path,
) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    ready = await adapter.open_case("H-1230-2026")
    ready.update(
        status="closure_review_required",
        current_node="closure_review",
        pending_interrupt={
            "kind": "closure_review",
            "case_id": ready["case_id"],
            "thread_id": ready["thread_id"],
            "expected_version": 8,
            "action_digest": "closure-digest",
        },
        case_version=8,
        evidence_gaps=[],
        ambiguous_lot_ids=[],
        required_facilities=["DC-NORTH"],
        acknowledgements={"DC-NORTH": True},
        verification={"violations": []},
    )
    review = await adapter.request_closure(ready)
    assert review["status"] == "closure_review_required"
    assert review["closure"]["status"] == "closure_review_required"
    assert any(gate["state"] == "review" for gate in review["closure"]["gates"])
    assert review["closure"]["blockers"] == []

    closed = {**review, "status": "closed", "pending_interrupt": None}
    completed = await adapter.request_closure(closed)
    assert completed["status"] == "closed"
    assert completed["closure"]["status"] == "Closed — simulated"
