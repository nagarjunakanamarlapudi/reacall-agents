from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from recallops.agents.runtime import RecallOpsRuntime
from recallops.ui.adapter import DurableRuntimeAdapter, normalize_runtime_result
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


@pytest.mark.asyncio
async def test_durable_adapter_projects_compact_checkpoint_history(tmp_path: Path) -> None:
    """Break caught: the product loses durable history when raw graph access is removed."""

    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    reviewed = await adapter.run_investigation(await adapter.open_case("H-1230-2026"))

    assert reviewed["checkpoint_history"]
    assert reviewed["checkpoint_history"][0] == {
        "checkpoint_id": reviewed["checkpoint_id"],
        "status": "review_required",
        "case_version": 0,
        "pending_kind": "action_review",
        "next_nodes": ["action_review"],
    }
    assert json.loads(json.dumps(reviewed["checkpoint_history"])) == reviewed[
        "checkpoint_history"
    ]


@pytest.mark.asyncio
async def test_immutable_runtime_result_normalizes_to_detached_ui_json(tmp_path: Path) -> None:
    """Break caught: a UI projection mutates or leaks the runtime's frozen audit result."""

    checkpoint = tmp_path / "checkpoints.sqlite3"
    operations = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint,
        operations_path=operations,
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Project immutable runtime results into detached product state.",
            case_id="CASE-IMMUTABLE-UI",
            thread_id="THREAD-IMMUTABLE-UI",
        )
        projected = normalize_runtime_result(result)
        projected["status"] = "caller-only"
        projected["pending_interrupt"]["kind"] = "caller-only"
        fresh = await runtime.get_case(thread_id="THREAD-IMMUTABLE-UI")

    with pytest.raises(TypeError):
        result.case["status"] = "forged"
    assert fresh.case["status"] == "review_required"
    assert fresh.pending_interrupt["kind"] == "action_review"
    assert json.loads(json.dumps(projected))["status"] == "caller-only"


@pytest.mark.asyncio
async def test_copied_store_adapters_surface_one_fenced_review_winner(tmp_path: Path) -> None:
    """Break caught: two product adapters fork one copied durable checkpoint head."""

    original_checkpoint = tmp_path / "original-checkpoints.sqlite3"
    copied_checkpoint = tmp_path / "copied-checkpoints.sqlite3"
    operations = tmp_path / "operations.sqlite3"
    original = DurableRuntimeAdapter(
        checkpoint_path=original_checkpoint,
        operations_path=operations,
    )
    reviewed = await original.run_investigation(await original.open_case("H-1230-2026"))
    shutil.copy2(original_checkpoint, copied_checkpoint)
    copied = DurableRuntimeAdapter(
        checkpoint_path=copied_checkpoint,
        operations_path=operations,
    )

    outcomes = await asyncio.gather(
        original.resume_review(
            reviewed,
            decision="approve",
            actor="Food-safety manager",
            justification=APPROVAL_JUSTIFICATION,
            edited_action="",
        ),
        copied.resume_review(
            reviewed,
            decision="reject",
            actor="Food-safety manager",
            justification="Reject the proposed action while evidence is rechecked.",
            edited_action="",
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum(isinstance(item, ValueError) for item in outcomes) == 1
    stale = copied if isinstance(outcomes[0], dict) else original
    with pytest.raises(ValueError, match="checkpoint|head|stale|mutation"):
        await stale.resume_review(
            reviewed,
            decision="approve",
            actor="Food-safety manager",
            justification=APPROVAL_JUSTIFICATION,
            edited_action="",
        )


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


@pytest.mark.asyncio
async def test_product_adapter_runs_every_probable_lot_action_through_closure(
    tmp_path: Path,
) -> None:
    operations = tmp_path / "operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations,
    )
    opened = await adapter.open_case("H-1230-2026")
    opened["scope_lot_ids"] = ["LOT-PROBABLE-160"]
    case = await adapter.run_investigation(opened)
    reviewed_actions: list[str] = []
    receipt_counts: list[int] = []
    edited_closure = False

    while case.get("pending_interrupt") is not None:
        pending = case["pending_interrupt"]
        if pending["kind"] in {"action_review", "closure_review"}:
            action_type = pending["action"]["action_type"]
            reviewed_actions.append(action_type)
            if pending["kind"] == "closure_review" and not edited_closure:
                case = await adapter.resume_review(
                    case,
                    decision="edit",
                    actor="Food-safety manager",
                    justification="Clarify closure rationale without changing action or scope.",
                    edited_action="Close only after every disposition and acknowledgement.",
                )
                assert case["status"] == "closure_review_required"
                assert case["pending_interrupt"]["kind"] == "closure_review"
                edited_closure = True
            else:
                case = await adapter.resume_review(
                    case,
                    decision="approve",
                    actor="Food-safety manager",
                    justification=APPROVAL_JUSTIFICATION,
                    edited_action="",
                )
        elif pending["kind"] == "execution_confirmation":
            before = len(case["receipts"])
            case = await adapter.simulate_approved_actions(case)
            assert len(case["receipts"]) == before + 1
            receipt_counts.append(len(case["receipts"]))
        else:  # pragma: no cover - explicit contract assertion is clearer
            raise AssertionError(f"unexpected interrupt: {pending['kind']}")

    assert reviewed_actions == [
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_acknowledgment",
        "close_case",
        "close_case",
    ]
    assert receipt_counts == list(range(1, 7))
    assert [item["action_type"] for item in case["receipts"]].count("record_acknowledgment") == 2
    assert all(item["action_type"] != "record_disposition" for item in case["receipts"])
    assert case["status"] == "closed"
    assert _receipt_count(operations) == 6


@pytest.mark.asyncio
async def test_product_adapter_routes_exact_lot_through_required_disposition(
    tmp_path: Path,
) -> None:
    operations = tmp_path / "operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations,
    )
    opened = await adapter.open_case("H-1230-2026")
    opened["scope_lot_ids"] = ["LOT-EXACT-170"]
    case = await adapter.run_investigation(opened)
    reviewed_actions: list[str] = []
    receipt_versions: list[int] = []

    while case.get("pending_interrupt") is not None:
        pending = case["pending_interrupt"]
        if pending["kind"] in {"action_review", "closure_review"}:
            reviewed_actions.append(pending["action"]["action_type"])
            case = await adapter.resume_review(
                case,
                decision="approve",
                actor="Food-safety manager",
                justification=APPROVAL_JUSTIFICATION,
                edited_action="",
            )
        elif pending["kind"] == "execution_confirmation":
            before = len(case["receipts"])
            action_type = pending["action"]["action_type"]
            case = await adapter.simulate_approved_actions(case)
            if action_type == "close_case" and case["status"] == "open_closure_blocked":
                assert len(case["receipts"]) == before
            else:
                assert len(case["receipts"]) == before + 1
                receipt_versions.append(case["receipts"][-1]["case_version"])
        else:  # pragma: no cover - explicit contract assertion is clearer
            raise AssertionError(f"unexpected interrupt: {pending['kind']}")

    assert reviewed_actions[:3] == [
        "create_case",
        "apply_inventory_hold",
        "record_disposition",
    ]
    assert reviewed_actions.index("record_disposition") < reviewed_actions.index(
        "create_facility_tasks"
    )
    assert reviewed_actions[-1] == "close_case"
    assert receipt_versions == list(range(1, len(receipt_versions) + 1))
    assert [item["action_type"] for item in case["receipts"]] == reviewed_actions[:-1]
    assert case["status"] == "open_closure_blocked"
    assert case["closure_outcome"]["eligible"] is False
    assert case["closure_outcome"].get("closed") is not True
    assert _receipt_count(operations) == len(reviewed_actions) - 1
