"""End-to-end contracts for the durable RecallOps LangGraph runtime."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest


async def _execute_workflow(workflow, input, config):
    from recallops.agents.workflow import _execute_workflow as execute_workflow

    return await execute_workflow(workflow, input, config)


def _workflow_active_lock_count(workflow) -> int:
    from recallops.agents.workflow import _workflow_active_lock_count as active_lock_count

    return active_lock_count(workflow)


def _operation_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
    finally:
        connection.close()


def _case_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0])
    finally:
        connection.close()


def _durable_confirmation_records(history) -> tuple[dict, ...]:
    """Extract only adjacent pending-confirmation -> execute-ready audit transitions."""
    records = []
    for confirmed, pending in zip(history, history[1:], strict=False):
        request = pending.pending_interrupt
        if (
            confirmed.case.get("status") != "approved_pending_execution"
            or confirmed.pending_interrupt is not None
            or confirmed.next_nodes != ("execute_one_operation",)
            or not confirmed.checkpoint_id
            or request is None
            or request.get("kind") != "execution_confirmation"
            or pending.next_nodes != ("execution_confirmation",)
            or not pending.checkpoint_id
        ):
            continue
        binding = {
            "case_id": request["case_id"],
            "thread_id": request["thread_id"],
            "case_version": request["case_version"],
            "action_id": request["action_id"],
            "action_digest": request["action_digest"],
            "execution_id": request["execution_id"],
            "idempotency_key": request["idempotency_key"],
            "action": request["action"],
            "approval": confirmed.case["approval"],
        }
        confirmed_binding = {
            "case_id": confirmed.case["case_id"],
            "thread_id": confirmed.case["thread_id"],
            "case_version": confirmed.case["case_version"],
            "action_id": confirmed.case["current_action"]["action_id"],
            "action_digest": confirmed.case["action_digest"],
            "execution_id": confirmed.case["execution_id"],
            "idempotency_key": confirmed.case["idempotency_key"],
            "action": confirmed.case["current_action"],
            "approval": confirmed.case["approval"],
        }
        if binding == confirmed_binding:
            assert pending.case["execution_request"] == request
            assert confirmed.case["execution_request"] == request
            assert len(confirmed.case["write_receipts"]) == len(pending.case["write_receipts"])
            assert confirmed.checkpoint_id != pending.checkpoint_id
            records.append(binding)
    return tuple(records)


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
            case_id="THREAD-FLAGSHIP",
            thread_id="THREAD-FLAGSHIP",
        )

    assert result.next_nodes == ("action_review",)
    assert result.pending_interrupt is not None
    assert result.pending_interrupt["kind"] == "action_review"
    assert result.pending_interrupt["case_id"] == "THREAD-FLAGSHIP"
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
    assert result.case["action_queue"] == [result.case["current_action"]]
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
    assert json.loads(json.dumps(result.model_dump(mode="json")["case"])) == result.case
    assert _operation_count(operations_path) == 0
    assert _case_count(operations_path) == 0


@pytest.mark.asyncio
async def test_existing_thread_start_fails_without_altering_checkpoint(tmp_path: Path) -> None:
    """Break caught: start_case overwrites a durable thread or invents an unknown case."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        assert await runtime.get_case(thread_id="THREAD-NOT-FOUND") is None
        first = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Investigate the affected recall lots.",
            case_id="THREAD-ONCE",
            thread_id="THREAD-ONCE",
        )
        with pytest.raises(ValueError, match="already has a durable checkpoint"):
            await runtime.start_case(
                recall_number="H-1230-2026",
                question="Try to replace the durable workflow.",
                case_id="THREAD-ONCE",
                thread_id="THREAD-ONCE",
            )
        assert await runtime.get_case(thread_id="THREAD-ONCE") == first
    assert _case_count(operations_path) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"recall_number": None}, "recall_number"),
        ({"recall_number": False}, "recall_number"),
        ({"recall_number": 123}, "recall_number"),
        ({"question": None}, "question"),
        ({"question": False}, "question"),
        ({"question": 123}, "question"),
        ({"case_id": False}, "case_id"),
        ({"case_id": 123}, "case_id"),
        ({"thread_id": False}, "thread_id"),
        ({"thread_id": 123}, "thread_id"),
        ({"scope_lot_ids": "LOT-PROBABLE-160"}, "scope_lot_ids"),
        ({"scope_lot_ids": False}, "scope_lot_ids"),
        ({"scope_lot_ids": {"LOT-PROBABLE-160"}}, "scope_lot_ids"),
        ({"scope_lot_ids": [False]}, "scope_lot_ids"),
        ({"scope_lot_ids": [123]}, "scope_lot_ids"),
    ],
)
async def test_public_start_rejects_coercive_or_container_shaped_inputs(
    tmp_path: Path,
    overrides: dict,
    message: str,
) -> None:
    """Break caught: public start accepts bools, strings-as-lists, or raises AttributeError."""
    from recallops.agents.runtime import RecallOpsRuntime

    kwargs = {
        "recall_number": "H-1230-2026",
        "question": "Validate the exact public start contract.",
        "case_id": "CASE-STRICT-START",
        "thread_id": "THREAD-STRICT-START",
        "scope_lot_ids": [],
        **overrides,
    }
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    ) as runtime:
        with pytest.raises((TypeError, ValueError), match=message):
            await runtime.start_case(**kwargs)
        assert await runtime.get_case(thread_id="THREAD-STRICT-START") is None


@pytest.mark.asyncio
async def test_public_start_persists_the_distinct_case_thread_mapping(tmp_path: Path) -> None:
    """Break caught: Operations silently replaces the durable graph thread with case_id."""
    from recallops.agents.runtime import RecallOpsRuntime
    from recallops.services.operations import OperationsService

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Persist the explicit case-to-thread mapping.",
            case_id="CASE-MAPPED",
            thread_id="THREAD-MAPPED",
        )
        assert review.case["case_id"] == "CASE-MAPPED"
        assert review.case["thread_id"] == "THREAD-MAPPED"
        confirmation = await runtime.resume_case(
            thread_id="THREAD-MAPPED",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )
        created = await runtime.resume_case(
            thread_id="THREAD-MAPPED",
            response=_bound_response(confirmation.pending_interrupt, decision="confirm"),
        )
        assert created.case["case_version"] == 1

    operation_case = OperationsService(storage_path=operations_path).get_case("CASE-MAPPED")
    assert operation_case is not None
    assert operation_case.thread_id == "THREAD-MAPPED"


@pytest.mark.asyncio
async def test_case_and_thread_identity_is_bidirectional_across_restarts_and_stores(
    tmp_path: Path,
) -> None:
    """Break caught: one case is initialized on two graph threads or vice versa."""
    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        original = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Reserve a durable one-to-one case and thread identity.",
            case_id="CASE-SAME",
            thread_id="THREAD-ONE",
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        with pytest.raises(ValueError, match="case_id.*THREAD-ONE|already bound"):
            await runtime.start_case(
                recall_number="H-1230-2026",
                question="Attempt to bind the same case to a second thread.",
                case_id="CASE-SAME",
                thread_id="THREAD-TWO",
            )
        assert await runtime.get_case(thread_id="THREAD-ONE") == original
        assert await runtime.get_case(thread_id="THREAD-TWO") is None

        confirmation = await runtime.resume_case(
            thread_id="THREAD-ONE",
            response=_bound_response(original.pending_interrupt, decision="approve"),
        )
        await runtime.resume_case(
            thread_id="THREAD-ONE",
            response=_bound_response(confirmation.pending_interrupt, decision="confirm"),
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "other-checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        with pytest.raises(ValueError, match="already bound"):
            await runtime.start_case(
                recall_number="H-1230-2026",
                question="Operations must reject a second thread for the same case.",
                case_id="CASE-SAME",
                thread_id="THREAD-TWO",
            )
        with pytest.raises(ValueError, match="already bound"):
            await runtime.start_case(
                recall_number="H-1230-2026",
                question="Operations must reject a second case for the same thread.",
                case_id="CASE-TWO",
                thread_id="THREAD-ONE",
            )


@pytest.mark.asyncio
async def test_exact_identity_cannot_start_in_a_second_checkpoint_store(tmp_path: Path) -> None:
    """Break caught: an exact durable reservation is treated as reusable by another store."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    first_checkpoint = tmp_path / "first-checkpoints.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=first_checkpoint,
        operations_path=operations_path,
    ) as runtime:
        original = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Reserve this case and thread for one checkpoint store.",
            case_id="CASE-OWNED",
            thread_id="THREAD-OWNED",
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "second-checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        with pytest.raises(ValueError, match="reserved|checkpoint store|already bound"):
            await runtime.start_case(
                recall_number="H-1230-2026",
                question="A second checkpoint store must not own this workflow.",
                case_id="CASE-OWNED",
                thread_id="THREAD-OWNED",
            )

    async with RecallOpsRuntime.open(
        checkpoint_path=first_checkpoint,
        operations_path=operations_path,
    ) as runtime:
        assert await runtime.get_case(thread_id="THREAD-OWNED") == original


@pytest.mark.asyncio
async def test_checkpoint_store_owner_survives_database_move(tmp_path: Path) -> None:
    """Break caught: ownership is derived from a filename instead of checkpoint identity."""
    from recallops.agents.runtime import RecallOpsRuntime

    original_path = tmp_path / "original-checkpoints.sqlite3"
    moved_path = tmp_path / "moved-checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=original_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="The durable store identity follows the SQLite database.",
            case_id="CASE-MOVED-STORE",
            thread_id="THREAD-MOVED-STORE",
        )

    original_path.replace(moved_path)
    async with RecallOpsRuntime.open(
        checkpoint_path=moved_path,
        operations_path=operations_path,
    ) as runtime:
        restored = await runtime.get_case(thread_id="THREAD-MOVED-STORE")
        assert restored == review
        confirmation = await runtime.resume_case(
            thread_id="THREAD-MOVED-STORE",
            response=_bound_response(restored.pending_interrupt, decision="approve"),
        )

    assert confirmation.pending_interrupt["kind"] == "execution_confirmation"


@pytest.mark.asyncio
async def test_new_database_at_old_path_cannot_reuse_checkpoint_owner(tmp_path: Path) -> None:
    """Break caught: deleting/replacing a checkpoint file preserves path-derived ownership."""
    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        await runtime.start_case(
            recall_number="H-1230-2026",
            question="Only the original durable checkpoint store may own this identity.",
            case_id="CASE-REPLACED-STORE",
            thread_id="THREAD-REPLACED-STORE",
        )

    checkpoint_path.replace(tmp_path / "archived-checkpoints.sqlite3")
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        with pytest.raises(ValueError, match="reserved|checkpoint store|owner"):
            await runtime.start_case(
                recall_number="H-1230-2026",
                question="A replacement database is a different owner.",
                case_id="CASE-REPLACED-STORE",
                thread_id="THREAD-REPLACED-STORE",
            )


@pytest.mark.asyncio
async def test_path_owned_checkpoint_migrates_to_embedded_random_owner(tmp_path: Path) -> None:
    """Break caught: a pre-metadata checkpoint cannot reopen after ownership hardening."""
    from uuid import NAMESPACE_URL, UUID, uuid5

    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = (tmp_path / "checkpoints.sqlite3").resolve()
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Migrate a durable checkpoint created by the path-owned runtime.",
            case_id="CASE-PATH-OWNER-MIGRATION",
            thread_id="THREAD-PATH-OWNER-MIGRATION",
        )

    legacy_owner = str(uuid5(NAMESPACE_URL, f"recallops-checkpoint-owner:{checkpoint_path}"))
    with sqlite3.connect(checkpoint_path) as connection:
        connection.execute("DROP TABLE recallops_runtime_metadata")
    with sqlite3.connect(operations_path) as connection:
        connection.execute(
            "UPDATE workflow_identities SET owner_token=? WHERE case_id=?",
            (legacy_owner, "CASE-PATH-OWNER-MIGRATION"),
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        restored = await runtime.get_case(thread_id="THREAD-PATH-OWNER-MIGRATION")
        assert restored == review
        confirmation = await runtime.resume_case(
            thread_id="THREAD-PATH-OWNER-MIGRATION",
            response=_bound_response(restored.pending_interrupt, decision="approve"),
        )

    with sqlite3.connect(operations_path) as connection:
        migrated_owner = connection.execute(
            "SELECT owner_token FROM workflow_identities WHERE case_id=?",
            ("CASE-PATH-OWNER-MIGRATION",),
        ).fetchone()[0]
    assert confirmation.pending_interrupt["kind"] == "execution_confirmation"
    assert UUID(migrated_owner).version == 4
    assert migrated_owner != legacy_owner


@pytest.mark.asyncio
async def test_unrelated_version_five_owner_is_not_treated_as_a_path_legacy(
    tmp_path: Path,
) -> None:
    """Break caught: any UUIDv5 token can impersonate the one legacy path owner."""
    from uuid import NAMESPACE_URL, uuid5

    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Only the exact former path token is eligible for migration.",
            case_id="CASE-HOSTILE-V5",
            thread_id="THREAD-HOSTILE-V5",
        )

    hostile_owner = str(uuid5(NAMESPACE_URL, "unrelated-checkpoint-store"))
    with sqlite3.connect(operations_path) as connection:
        connection.execute(
            "UPDATE workflow_identities SET owner_token=? WHERE case_id=?",
            (hostile_owner, "CASE-HOSTILE-V5"),
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        before = await runtime.get_case_history(thread_id="THREAD-HOSTILE-V5")
        with pytest.raises(ValueError, match="reserved|checkpoint store|owner"):
            await runtime.resume_case(
                thread_id="THREAD-HOSTILE-V5",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            )
        assert await runtime.get_case_history(thread_id="THREAD-HOSTILE-V5") == before


@pytest.mark.asyncio
async def test_unreserved_checkpoint_can_be_read_but_cannot_resume_into_operations(
    tmp_path: Path,
) -> None:
    """Break caught: a checkpoint copied onto an unrelated Operations DB can mutate it."""
    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    owner_operations = tmp_path / "owner-operations.sqlite3"
    unrelated_operations = tmp_path / "unrelated-operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=owner_operations,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Reads survive, but resumes require the checkpoint's Operations owner.",
            case_id="CASE-UNRESERVED-RESUME",
            thread_id="THREAD-UNRESERVED-RESUME",
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=unrelated_operations,
    ) as runtime:
        assert await runtime.get_case(thread_id="THREAD-UNRESERVED-RESUME") == review
        history_before = await runtime.get_case_history(thread_id="THREAD-UNRESERVED-RESUME")
        with pytest.raises(ValueError, match="reserved|owner|identity"):
            await runtime.resume_case(
                thread_id="THREAD-UNRESERVED-RESUME",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            )
        assert (
            await runtime.get_case_history(thread_id="THREAD-UNRESERVED-RESUME") == history_before
        )

    assert _case_count(unrelated_operations) == 0
    assert _operation_count(unrelated_operations) == 0


@pytest.mark.asyncio
async def test_legacy_null_owner_is_claimed_atomically_from_matching_checkpoint(
    tmp_path: Path,
) -> None:
    """Break caught: an authentic pre-owner reservation stays nullable after resume."""
    from uuid import UUID

    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Claim the legacy identity only from its matching checkpoint.",
            case_id="CASE-LEGACY-NULL",
            thread_id="THREAD-LEGACY-NULL",
        )

    with sqlite3.connect(operations_path) as connection:
        connection.execute(
            "UPDATE workflow_identities SET owner_token=NULL WHERE case_id=?",
            ("CASE-LEGACY-NULL",),
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        confirmation = await runtime.resume_case(
            thread_id="THREAD-LEGACY-NULL",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )

    with sqlite3.connect(operations_path) as connection:
        owner = connection.execute(
            "SELECT owner_token FROM workflow_identities WHERE case_id=?",
            ("CASE-LEGACY-NULL",),
        ).fetchone()[0]
    assert confirmation.pending_interrupt["kind"] == "execution_confirmation"
    assert UUID(owner).version == 4


@pytest.mark.asyncio
async def test_hostile_null_identity_cannot_claim_a_different_checkpoint(
    tmp_path: Path,
) -> None:
    """Break caught: any NULL identity row is accepted without exact checkpoint proof."""
    from recallops.agents.runtime import RecallOpsRuntime
    from recallops.services.operations import OperationsService

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    owner_operations = tmp_path / "owner-operations.sqlite3"
    hostile_operations = tmp_path / "hostile-operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=owner_operations,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Reject an unrelated nullable Operations identity.",
            case_id="CASE-NULL-PROOF",
            thread_id="THREAD-NULL-PROOF",
        )

    OperationsService(storage_path=hostile_operations)
    with sqlite3.connect(hostile_operations) as connection:
        connection.execute(
            "INSERT INTO workflow_identities (case_id, thread_id, owner_token) VALUES (?, ?, NULL)",
            ("CASE-HOSTILE", "THREAD-NULL-PROOF"),
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=hostile_operations,
    ) as runtime:
        before = await runtime.get_case_history(thread_id="THREAD-NULL-PROOF")
        with pytest.raises(ValueError, match="identity|reserved|bound"):
            await runtime.resume_case(
                thread_id="THREAD-NULL-PROOF",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            )
        assert await runtime.get_case_history(thread_id="THREAD-NULL-PROOF") == before


@pytest.mark.asyncio
async def test_legacy_resume_wins_against_concurrent_replacement_store_start(
    tmp_path: Path,
) -> None:
    """Break caught: a replacement store races a NULL legacy claim into dual ownership."""
    from recallops.agents.runtime import RecallOpsRuntime, RuntimeResult

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Atomically claim one legacy owner during a competing start.",
            case_id="CASE-CONCURRENT-LEGACY",
            thread_id="THREAD-CONCURRENT-LEGACY",
        )

    with sqlite3.connect(operations_path) as connection:
        connection.execute(
            "UPDATE workflow_identities SET owner_token=NULL WHERE case_id=?",
            ("CASE-CONCURRENT-LEGACY",),
        )

    async with (
        RecallOpsRuntime.open(
            checkpoint_path=checkpoint_path,
            operations_path=operations_path,
        ) as legitimate,
        RecallOpsRuntime.open(
            checkpoint_path=tmp_path / "replacement-checkpoints.sqlite3",
            operations_path=operations_path,
        ) as replacement,
    ):
        outcomes = await asyncio.gather(
            legitimate.resume_case(
                thread_id="THREAD-CONCURRENT-LEGACY",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            ),
            replacement.start_case(
                recall_number="H-1230-2026",
                question="A replacement store must not take the legacy identity.",
                case_id="CASE-CONCURRENT-LEGACY",
                thread_id="THREAD-CONCURRENT-LEGACY",
            ),
            return_exceptions=True,
        )

    assert sum(isinstance(item, RuntimeResult) for item in outcomes) == 1
    assert sum(isinstance(item, ValueError) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_copied_checkpoint_heads_have_one_fenced_mutation_winner(tmp_path: Path) -> None:
    """Break caught: copied stores sharing one UUID can both fork the same paused head."""
    from recallops.agents.runtime import RecallOpsRuntime, RuntimeResult

    first_checkpoint = tmp_path / "first-checkpoints.sqlite3"
    copied_checkpoint = tmp_path / "copied-checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=first_checkpoint,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Fence every mutation from this copied checkpoint head.",
            case_id="CASE-COPIED-HEAD",
            thread_id="THREAD-COPIED-HEAD",
        )

    shutil.copy2(first_checkpoint, copied_checkpoint)
    async with (
        RecallOpsRuntime.open(
            checkpoint_path=first_checkpoint,
            operations_path=operations_path,
        ) as first,
        RecallOpsRuntime.open(
            checkpoint_path=copied_checkpoint,
            operations_path=operations_path,
        ) as copied,
    ):
        outcomes = await asyncio.gather(
            first.resume_case(
                thread_id="THREAD-COPIED-HEAD",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            ),
            copied.resume_case(
                thread_id="THREAD-COPIED-HEAD",
                response=_bound_response(review.pending_interrupt, decision="reject"),
            ),
            return_exceptions=True,
        )

    assert sum(isinstance(item, RuntimeResult) for item in outcomes) == 1
    assert sum(isinstance(item, ValueError) for item in outcomes) == 1
    winner_index = next(
        index for index, item in enumerate(outcomes) if isinstance(item, RuntimeResult)
    )
    winner_path = (first_checkpoint, copied_checkpoint)[winner_index]
    stale_path = (copied_checkpoint, first_checkpoint)[winner_index]
    winning_result = outcomes[winner_index]

    async with RecallOpsRuntime.open(
        checkpoint_path=stale_path,
        operations_path=operations_path,
    ) as stale:
        stale_before = await stale.get_case_history(thread_id="THREAD-COPIED-HEAD")
        with pytest.raises(ValueError, match="checkpoint|head|stale|mutation"):
            await stale.resume_case(
                thread_id="THREAD-COPIED-HEAD",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            )
        assert await stale.get_case_history(thread_id="THREAD-COPIED-HEAD") == stale_before

    async with RecallOpsRuntime.open(
        checkpoint_path=winner_path,
        operations_path=operations_path,
    ) as winner:
        assert await winner.get_case(thread_id="THREAD-COPIED-HEAD") == winning_result


@pytest.mark.asyncio
async def test_winning_store_recovers_crash_between_checkpoint_and_head_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a crash after checkpoint commit strands the winning lease forever."""
    from recallops.agents.runtime import RecallOpsRuntime
    from recallops.services.operations import OperationsService

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Recover only the store carrying the durable attempt token.",
            case_id="CASE-CRASH-FENCE",
            thread_id="THREAD-CRASH-FENCE",
        )

        with monkeypatch.context() as crash:
            crash.setattr(
                OperationsService,
                "advance_workflow_mutation",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    SystemExit("simulated process death after checkpoint commit")
                ),
            )
            with pytest.raises(SystemExit, match="simulated process death"):
                await runtime.resume_case(
                    thread_id="THREAD-CRASH-FENCE",
                    response=_bound_response(review.pending_interrupt, decision="approve"),
                )

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        confirmation = await runtime.get_case(thread_id="THREAD-CRASH-FENCE")
        assert confirmation.pending_interrupt["kind"] == "execution_confirmation"
        created = await runtime.resume_case(
            thread_id="THREAD-CRASH-FENCE",
            response=_bound_response(confirmation.pending_interrupt, decision="confirm"),
        )

    assert created.case["case_version"] == 1
    assert _operation_count(operations_path) == 1


@pytest.mark.asyncio
async def test_cancelled_unchanged_resume_releases_fence_for_immediate_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: cancellation before checkpoint mutation leaves an immortal lease."""
    import recallops.agents.runtime as runtime_module
    from recallops.agents.runtime import RecallOpsRuntime

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Release an unchanged mutation fence after cancellation.",
            case_id="CASE-CANCELLED-FENCE",
            thread_id="THREAD-CANCELLED-FENCE",
        )

        async def cancel_before_mutation(*args, **kwargs):
            raise asyncio.CancelledError

        with monkeypatch.context() as cancellation:
            cancellation.setattr(runtime_module, "_execute_workflow", cancel_before_mutation)
            with pytest.raises(asyncio.CancelledError):
                await runtime.resume_case(
                    thread_id="THREAD-CANCELLED-FENCE",
                    response=_bound_response(review.pending_interrupt, decision="approve"),
                )

        confirmation = await runtime.resume_case(
            thread_id="THREAD-CANCELLED-FENCE",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )

    assert confirmation.pending_interrupt["kind"] == "execution_confirmation"


@pytest.mark.asyncio
async def test_expired_uncertain_lease_rejects_copy_but_original_token_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: lease expiry lets a copied store fork an uncertain attempt."""
    import recallops.agents.runtime as runtime_module
    from recallops.agents.runtime import RecallOpsRuntime
    from recallops.services.operations import OperationsService

    original_path = tmp_path / "original-checkpoints.sqlite3"
    copied_path = tmp_path / "copied-checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=original_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Only the prepared store may recover an expired uncertain lease.",
            case_id="CASE-EXPIRED-FENCE",
            thread_id="THREAD-EXPIRED-FENCE",
        )

    shutil.copy2(original_path, copied_path)

    async def die_before_mutation(*args, **kwargs):
        raise SystemExit("simulated death before checkpoint mutation")

    def die_before_release(*args, **kwargs):
        raise SystemExit("simulated death before fence release")

    async with RecallOpsRuntime.open(
        checkpoint_path=original_path,
        operations_path=operations_path,
    ) as original:
        with monkeypatch.context() as crash:
            crash.setattr(runtime_module, "_execute_workflow", die_before_mutation)
            crash.setattr(OperationsService, "release_workflow_mutation", die_before_release)
            with pytest.raises(SystemExit, match="fence release"):
                await original.resume_case(
                    thread_id="THREAD-EXPIRED-FENCE",
                    response=_bound_response(review.pending_interrupt, decision="approve"),
                )

    with sqlite3.connect(operations_path) as connection:
        connection.execute(
            "UPDATE workflow_identities SET attempt_expires_at=0 WHERE case_id=?",
            ("CASE-EXPIRED-FENCE",),
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=copied_path,
        operations_path=operations_path,
    ) as copied:
        with pytest.raises(ValueError, match="active|uncertain|mutation"):
            await copied.resume_case(
                thread_id="THREAD-EXPIRED-FENCE",
                response=_bound_response(review.pending_interrupt, decision="reject"),
            )

    with sqlite3.connect(operations_path) as connection:
        state = connection.execute(
            "SELECT attempt_state FROM workflow_identities WHERE case_id=?",
            ("CASE-EXPIRED-FENCE",),
        ).fetchone()[0]
    assert state == "uncertain"

    async with RecallOpsRuntime.open(
        checkpoint_path=original_path,
        operations_path=operations_path,
    ) as original:
        confirmation = await original.resume_case(
            thread_id="THREAD-EXPIRED-FENCE",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )

    assert confirmation.pending_interrupt["kind"] == "execution_confirmation"


@pytest.mark.asyncio
async def test_rejected_live_claim_cannot_delete_the_winners_recovery_marker(
    tmp_path: Path,
) -> None:
    """Break caught: a losing runtime deletes the active winner's crash-recovery proof."""
    from recallops.agents.runtime import (
        RecallOpsRuntime,
        _clear_checkpoint_attempt,
        _load_checkpoint_attempt,
        _prepare_checkpoint_attempt,
    )
    from recallops.services.operations import OperationsService

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Preserve the live winner's recovery proof.",
            case_id="CASE-LIVE-MARKER",
            thread_id="THREAD-LIVE-MARKER",
        )
        token = _prepare_checkpoint_attempt(
            checkpoint_path,
            "CASE-LIVE-MARKER",
            "THREAD-LIVE-MARKER",
            review.checkpoint_id,
        )
        operations = OperationsService(storage_path=operations_path)
        operations.claim_workflow_mutation(
            "CASE-LIVE-MARKER",
            "THREAD-LIVE-MARKER",
            runtime._checkpoint_owner_token,
            review.checkpoint_id,
            token,
        )

        with pytest.raises(ValueError, match="active|uncertain"):
            await runtime.resume_case(
                thread_id="THREAD-LIVE-MARKER",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            )

        assert _load_checkpoint_attempt(
            checkpoint_path,
            "CASE-LIVE-MARKER",
            "THREAD-LIVE-MARKER",
        ) == (token, review.checkpoint_id)
        operations.release_workflow_mutation(
            "CASE-LIVE-MARKER",
            "THREAD-LIVE-MARKER",
            runtime._checkpoint_owner_token,
            review.checkpoint_id,
            token,
        )
        _clear_checkpoint_attempt(
            checkpoint_path,
            "CASE-LIVE-MARKER",
            "THREAD-LIVE-MARKER",
            token,
        )


@pytest.mark.asyncio
async def test_concurrent_checkpoint_stores_have_one_exact_identity_winner(tmp_path: Path) -> None:
    """Break caught: two stores concurrently create independent graphs for one identity."""
    from recallops.agents.runtime import RecallOpsRuntime, RuntimeResult

    operations_path = tmp_path / "operations.sqlite3"
    async with (
        RecallOpsRuntime.open(
            checkpoint_path=tmp_path / "first-checkpoints.sqlite3",
            operations_path=operations_path,
        ) as first,
        RecallOpsRuntime.open(
            checkpoint_path=tmp_path / "second-checkpoints.sqlite3",
            operations_path=operations_path,
        ) as second,
    ):
        starts = await asyncio.gather(
            first.start_case(
                recall_number="H-1230-2026",
                question="Exactly one checkpoint store may own this workflow.",
                case_id="CASE-CONCURRENT-OWNER",
                thread_id="THREAD-CONCURRENT-OWNER",
            ),
            second.start_case(
                recall_number="H-1230-2026",
                question="Exactly one checkpoint store may own this workflow.",
                case_id="CASE-CONCURRENT-OWNER",
                thread_id="THREAD-CONCURRENT-OWNER",
            ),
            return_exceptions=True,
        )

    assert sum(isinstance(item, RuntimeResult) for item in starts) == 1
    assert sum(isinstance(item, ValueError) for item in starts) == 1


@pytest.mark.asyncio
async def test_real_stdio_runtime_reaches_one_approved_mcp_write(tmp_path: Path) -> None:
    """Break caught: runtime claims MCP orchestration but hardcodes in-process gateways."""
    from recallops.agents.runtime import RecallOpsRuntime
    from recallops.services.operations import OperationsService

    operations_path = tmp_path / "stdio-operations.sqlite3"
    checkpoint_path = tmp_path / "stdio-checkpoints.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
        transport="stdio",
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Use real MCP stdio to investigate and create the reviewed case.",
            case_id="CASE-STDIO-RUNTIME",
            thread_id="THREAD-STDIO-RUNTIME",
        )
        assert review.pending_interrupt["kind"] == "action_review"
        confirmation = await runtime.resume_case(
            thread_id="THREAD-STDIO-RUNTIME",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )
        original_key = confirmation.pending_interrupt["idempotency_key"]
        runtime.inject_failure("lost_write_response")
        unknown = await runtime.resume_case(
            thread_id="THREAD-STDIO-RUNTIME",
            response=_bound_response(confirmation.pending_interrupt, decision="confirm"),
        )

    assert unknown.case["status"] == "write_outcome_unknown"
    assert unknown.pending_interrupt["idempotency_key"] == original_key
    assert _operation_count(operations_path) == 1

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
        transport="stdio",
    ) as runtime:
        restored = await runtime.get_case(thread_id="THREAD-STDIO-RUNTIME")
        assert restored == unknown
        created = await runtime.resume_case(
            thread_id="THREAD-STDIO-RUNTIME",
            response=_bound_response(restored.pending_interrupt, decision="retry"),
        )
        stdio_confirmation_records = _durable_confirmation_records(
            await runtime.get_case_history(thread_id="THREAD-STDIO-RUNTIME")
        )

    assert created.case["case_version"] == 1
    assert created.pending_interrupt["action"]["action_type"] == "apply_inventory_hold"
    assert _operation_count(operations_path) == 1
    assert created.case["write_receipts"][0]["idempotency_key"] == original_key
    assert len(stdio_confirmation_records) == 1
    assert stdio_confirmation_records[0]["idempotency_key"] == original_key
    persisted = OperationsService(storage_path=operations_path).get_case("CASE-STDIO-RUNTIME")
    assert persisted is not None
    assert persisted.thread_id == "THREAD-STDIO-RUNTIME"


@pytest.mark.asyncio
async def test_runtime_rejects_unknown_transport_before_opening_stores(tmp_path: Path) -> None:
    from recallops.agents.runtime import RecallOpsRuntime

    with pytest.raises(ValueError, match="transport"):
        async with RecallOpsRuntime.open(
            checkpoint_path=tmp_path / "checkpoints.sqlite3",
            operations_path=tmp_path / "operations.sqlite3",
            transport="socket",
        ):
            pytest.fail("invalid transport unexpectedly opened")


@pytest.mark.asyncio
async def test_concurrent_same_thread_commands_have_one_checkpoint_winner(tmp_path: Path) -> None:
    """Break caught: check-then-invoke races admit two starts, reviews, or confirmations."""
    from recallops.agents.runtime import RecallOpsRuntime, RuntimeResult

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        starts = await asyncio.gather(
            *(
                runtime.start_case(
                    recall_number="H-1230-2026",
                    question="One durable investigation despite concurrent starts.",
                    case_id="THREAD-CONCURRENT-START",
                    thread_id="THREAD-CONCURRENT-START",
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, RuntimeResult) for item in starts) == 1
        assert sum(isinstance(item, ValueError) for item in starts) == 1

        review = next(item for item in starts if isinstance(item, RuntimeResult))
        decisions = await asyncio.gather(
            runtime.resume_case(
                thread_id="THREAD-CONCURRENT-START",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            ),
            runtime.resume_case(
                thread_id="THREAD-CONCURRENT-START",
                response=_bound_response(review.pending_interrupt, decision="reject"),
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, RuntimeResult) for item in decisions) == 1
        assert sum(isinstance(item, (ValueError, KeyError)) for item in decisions) == 1
        persisted = await runtime.get_case(thread_id="THREAD-CONCURRENT-START")
        assert persisted == next(item for item in decisions if isinstance(item, RuntimeResult))
        assert _operation_count(operations_path) == 0

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "confirm-checkpoints.sqlite3",
        operations_path=tmp_path / "confirm-operations.sqlite3",
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Confirm one write despite duplicate confirmation commands.",
            case_id="THREAD-CONCURRENT-CONFIRM",
            thread_id="THREAD-CONCURRENT-CONFIRM",
        )
        confirmation = await runtime.resume_case(
            thread_id="THREAD-CONCURRENT-CONFIRM",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )
        response = _bound_response(confirmation.pending_interrupt, decision="confirm")
        confirmations = await asyncio.gather(
            runtime.resume_case(thread_id="THREAD-CONCURRENT-CONFIRM", response=response),
            runtime.resume_case(thread_id="THREAD-CONCURRENT-CONFIRM", response=response),
            return_exceptions=True,
        )
        assert sum(isinstance(item, RuntimeResult) for item in confirmations) == 1
        assert sum(isinstance(item, ValueError) for item in confirmations) == 1
    assert _operation_count(tmp_path / "confirm-operations.sqlite3") == 1


@pytest.mark.asyncio
async def test_runtime_lock_registry_releases_normal_error_and_concurrent_entries(
    tmp_path: Path,
) -> None:
    """Break caught: every completed thread leaves an immortal class-level lock."""
    from recallops.agents.runtime import RecallOpsRuntime

    baseline = RecallOpsRuntime._active_lock_count()
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Release identity and thread locks after every command path.",
            case_id="CASE-LOCK-LIFECYCLE",
            thread_id="THREAD-LOCK-LIFECYCLE",
        )
        assert RecallOpsRuntime._active_lock_count() == baseline

        wrong = _bound_response(review.pending_interrupt, decision="approve")
        wrong["case_id"] = "CASE-WRONG"
        with pytest.raises(ValueError, match="case_id"):
            await runtime.resume_case(thread_id="THREAD-LOCK-LIFECYCLE", response=wrong)
        assert RecallOpsRuntime._active_lock_count() == baseline

        decisions = await asyncio.gather(
            runtime.resume_case(
                thread_id="THREAD-LOCK-LIFECYCLE",
                response=_bound_response(review.pending_interrupt, decision="approve"),
            ),
            runtime.resume_case(
                thread_id="THREAD-LOCK-LIFECYCLE",
                response=_bound_response(review.pending_interrupt, decision="reject"),
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in decisions) == 1
        assert RecallOpsRuntime._active_lock_count() == baseline

    assert RecallOpsRuntime._active_lock_count() == baseline


@pytest.mark.asyncio
async def test_user_thread_id_cannot_collide_with_identity_lock_namespace(tmp_path: Path) -> None:
    """Break caught: a valid thread ID equal to an internal lock token deadlocks start."""
    from recallops.agents.runtime import RecallOpsRuntime

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    ) as runtime:
        result = await asyncio.wait_for(
            runtime.start_case(
                recall_number="H-1230-2026",
                question="Internal lock namespaces must not collide with user identifiers.",
                case_id="CASE-LOCK-NAMESPACE",
                thread_id="__identity_start__",
            ),
            timeout=1,
        )
    assert result.pending_interrupt["thread_id"] == "__identity_start__"


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
            case_id="THREAD-RESTART",
            thread_id="THREAD-RESTART",
        )
        original_trace = review.case["tool_trace"]
        original_rag_state = review.case["rag_state"]
        assert original_rag_state["read_count"] == review.case["rag_result"]["read_count"]
        assert original_rag_state["query_count"] == review.case["rag_result"]["query_count"]

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
        assert confirmation.case["rag_state"] == original_rag_state

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
    assert created.case["rag_state"] == original_rag_state
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
            case_id="THREAD-UNKNOWN",
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
        confirmation_records = _durable_confirmation_records(
            await runtime.get_case_history(thread_id="THREAD-UNKNOWN")
        )

    assert recovered.case["case_version"] == 1
    assert recovered.case["write_receipts"][0]["idempotency_key"] == original_key
    assert len(confirmation_records) == 1
    assert confirmation_records[0]["idempotency_key"] == original_key
    assert _operation_count(operations_path) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("case_id", "CASE-FORGED"),
        ("action_type", "apply_inventory_hold"),
        ("idempotency_key", "forged-idempotency-key"),
        ("case_version", 99),
        ("status", "rejected"),
        ("actor", "forged-actor"),
        ("justification", "Forged justification."),
        ("reviewed_action", {"action_id": "forged-action"}),
    ],
)
async def test_receipt_field_mismatch_enters_same_key_authoritative_recovery(
    tmp_path: Path,
    field: str,
    forged_value: object,
) -> None:
    """Break caught: a typed but forged receipt advances the durable case."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    operations_path = tmp_path / f"{field}.sqlite3"
    operations = OperationsService(storage_path=operations_path)

    class CorruptingGateway(DirectGateway):
        corrupt_next = True

        async def create_case(self, **kwargs):
            receipt = await super().create_case(**kwargs)
            if not self.corrupt_next:
                return receipt
            self.corrupt_next = False
            forged = deepcopy(receipt)
            if field == "reviewed_action":
                forged["details"]["reviewed_action"] = forged_value
            else:
                forged[field] = forged_value
            return forged

    gateway = CorruptingGateway(operations=operations)
    graph = build_workflow(
        gateway=gateway,
        operations_service=operations,
        checkpointer=InMemorySaver(),
    )
    thread_id = f"THREAD-RECEIPT-{field}"
    config = {"configurable": {"thread_id": thread_id}}
    await _execute_workflow(
        graph,
        {
            "case_id": f"CASE-RECEIPT-{field}",
            "thread_id": thread_id,
            "recall_number": "H-1230-2026",
            "question": "Reject every forged receipt binding before advancing state.",
            "scope_lot_ids": [],
        },
        config,
    )
    review = (await graph.aget_state(config)).interrupts[0].value
    await _execute_workflow(
        graph,
        Command(resume=_bound_response(review, decision="approve")),
        config,
    )
    confirmation = (await graph.aget_state(config)).interrupts[0].value
    original_key = confirmation["idempotency_key"]
    await _execute_workflow(
        graph,
        Command(resume=_bound_response(confirmation, decision="confirm")),
        config,
    )

    unknown = await graph.aget_state(config)
    assert unknown.values["status"] == "write_outcome_unknown"
    assert unknown.values["case_version"] == 0
    assert unknown.values["write_receipts"] == []
    recovery = unknown.interrupts[0].value
    assert recovery["kind"] == "write_outcome_recovery"
    assert recovery["idempotency_key"] == original_key
    assert _operation_count(operations_path) == 1

    await _execute_workflow(
        graph,
        Command(resume=_bound_response(recovery, decision="retry")),
        config,
    )
    recovered = await graph.aget_state(config)
    assert recovered.values["case_version"] == 1
    assert recovered.values["write_receipts"][0]["idempotency_key"] == original_key
    assert _operation_count(operations_path) == 1


@pytest.mark.asyncio
async def test_exact_looking_receipt_without_operations_commit_is_not_trusted(
    tmp_path: Path,
) -> None:
    """Break caught: graph receipt shape is trusted without authoritative store parity."""
    from datetime import UTC, datetime

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    operations_path = tmp_path / "operations.sqlite3"
    operations = OperationsService(storage_path=operations_path)

    class NoCommitGateway(DirectGateway):
        async def create_case(self, **kwargs):
            approval = kwargs["approval"]
            action = kwargs["proposed_action"]
            return {
                "receipt_id": "forged-but-well-shaped",
                "case_id": kwargs["case_id"],
                "action_type": "create_case",
                "actor": approval.actor,
                "justification": approval.justification,
                "idempotency_key": kwargs["idempotency_key"],
                "case_version": kwargs["expected_case_version"] + 1,
                "status": "simulated",
                "created_at": datetime.now(UTC).isoformat(),
                "details": {"reviewed_action": action.model_dump(mode="json")},
            }

    graph = build_workflow(
        gateway=NoCommitGateway(operations=operations),
        operations_service=operations,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "THREAD-NO-COMMIT"}}
    await _execute_workflow(
        graph,
        {
            "case_id": "CASE-NO-COMMIT",
            "thread_id": "THREAD-NO-COMMIT",
            "recall_number": "H-1230-2026",
            "question": "Require Operations parity for every receipt.",
            "scope_lot_ids": [],
        },
        config,
    )
    review = (await graph.aget_state(config)).interrupts[0].value
    await _execute_workflow(
        graph,
        Command(resume=_bound_response(review, decision="approve")),
        config,
    )
    confirmation = (await graph.aget_state(config)).interrupts[0].value
    await _execute_workflow(
        graph,
        Command(resume=_bound_response(confirmation, decision="confirm")),
        config,
    )

    unknown = await graph.aget_state(config)
    assert unknown.values["status"] == "write_outcome_unknown"
    assert unknown.values["case_version"] == 0
    assert unknown.values["write_receipts"] == []
    assert unknown.interrupts[0].value["kind"] == "write_outcome_recovery"
    assert _operation_count(operations_path) == 0


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
            case_id="THREAD-CONSENT",
            thread_id="THREAD-CONSENT",
        )
        approved = await runtime.resume_case(
            thread_id="THREAD-CONSENT",
            response=_bound_response(review.pending_interrupt, decision="approve"),
        )

        assert approved.pending_interrupt["kind"] == "execution_confirmation"
        assert approved.case["status"] == "approved_pending_execution"
        assert _operation_count(operations_path) == 0
        assert (
            _durable_confirmation_records(
                await runtime.get_case_history(thread_id="THREAD-CONSENT")
            )
            == ()
        )

        created = await runtime.resume_case(
            thread_id="THREAD-CONSENT",
            response=_bound_response(approved.pending_interrupt, decision="confirm"),
        )
        assert created.pending_interrupt["kind"] == "action_review"
        assert created.pending_interrupt["case_version"] == 1
        assert created.pending_interrupt["action"]["action_type"] == "apply_inventory_hold"
        assert [item["action_type"] for item in created.case["write_receipts"]] == ["create_case"]
        assert _operation_count(operations_path) == 1
        create_records = _durable_confirmation_records(
            await runtime.get_case_history(thread_id="THREAD-CONSENT")
        )
        assert create_records == (
            {
                key: approved.pending_interrupt[key]
                for key in (
                    "case_id",
                    "thread_id",
                    "case_version",
                    "action_id",
                    "action_digest",
                    "execution_id",
                    "idempotency_key",
                )
            }
            | {
                "action": approved.pending_interrupt["action"],
                "approval": approved.case["approval"],
            },
        )
        assert (
            created.case["write_receipts"][0]["idempotency_key"]
            == create_records[0]["idempotency_key"]
        )
        assert created.case["write_receipts"][0]["case_version"] == (
            create_records[0]["case_version"] + 1
        )
        assert (
            created.case["write_receipts"][0]["details"]["reviewed_action"]["action_id"]
            == create_records[0]["action_id"]
        )
        assert (
            created.case["write_receipts"][0]["details"]["reviewed_action"]
            == (create_records[0]["action"])
        )
        assert created.case["write_receipts"][0]["actor"] == create_records[0]["approval"]["actor"]
        assert (
            created.case["write_receipts"][0]["justification"]
            == create_records[0]["approval"]["justification"]
        )

        hold_approved = await runtime.resume_case(
            thread_id="THREAD-CONSENT",
            response=_bound_response(created.pending_interrupt, decision="approve"),
        )
        held = await runtime.resume_case(
            thread_id="THREAD-CONSENT",
            response=_bound_response(hold_approved.pending_interrupt, decision="confirm"),
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as restarted:
        durable_records = _durable_confirmation_records(
            await restarted.get_case_history(thread_id="THREAD-CONSENT")
        )

    assert held.pending_interrupt is None
    assert held.case["case_version"] == 2
    assert held.case["status"] == "open_closure_blocked"
    assert [item["action_type"] for item in held.case["write_receipts"]] == [
        "create_case",
        "apply_inventory_hold",
    ]
    assert _operation_count(operations_path) == 2
    assert len(durable_records) == 2
    assert {record["idempotency_key"] for record in durable_records} == {
        receipt["idempotency_key"] for receipt in held.case["write_receipts"]
    }
    for receipt in held.case["write_receipts"]:
        record = next(
            item
            for item in durable_records
            if item["idempotency_key"] == receipt["idempotency_key"]
        )
        assert record["case_version"] + 1 == receipt["case_version"]
        assert record["action"] == receipt["details"]["reviewed_action"]
        assert record["action"]["action_type"] == receipt["action_type"]


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
            case_id="THREAD-BINDING",
            thread_id="THREAD-BINDING",
        )
        before = await runtime.get_case(thread_id="THREAD-BINDING")
        assert before is not None

        for field, wrong in (
            ("case_id", "CASE-OTHER"),
            ("thread_id", "THREAD-OTHER"),
            ("case_version", False),
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
async def test_injected_stale_decision_and_changed_digest_fail_before_resume(
    tmp_path: Path,
) -> None:
    """Break caught: an injected approval race mutates a pending checkpoint."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Investigate the recall and protect the approval boundary.",
            case_id="THREAD-INJECTED-BINDING",
            thread_id="THREAD-INJECTED-BINDING",
        )
        before = await runtime.get_case(thread_id="THREAD-INJECTED-BINDING")
        for scenario, message in (
            ("stale_decision_version", "stale decision/version"),
            ("changed_action_digest", "changed action digest"),
        ):
            runtime.inject_failure(scenario)
            with pytest.raises(ValueError, match=message):
                await runtime.resume_case(
                    thread_id="THREAD-INJECTED-BINDING",
                    response=_bound_response(review.pending_interrupt, decision="approve"),
                )
            assert await runtime.get_case(thread_id="THREAD-INJECTED-BINDING") == before
            assert _operation_count(operations_path) == 0


@pytest.mark.asyncio
async def test_compiled_graph_also_rejects_a_direct_unbound_command(tmp_path: Path) -> None:
    """Break caught: bypassing Runtime can feed an unbound Command into a write path."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    gateway = DirectGateway(
        operations=OperationsService(storage_path=tmp_path / "operations.sqlite3")
    )
    graph = build_workflow(gateway=gateway, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "THREAD-DIRECT"}}
    await _execute_workflow(
        graph,
        {
            "case_id": "THREAD-DIRECT",
            "thread_id": "THREAD-DIRECT",
            "recall_number": "H-1230-2026",
            "question": "Investigate the affected recall lots.",
            "scope_lot_ids": [],
        },
        config,
    )
    pending = (await graph.aget_state(config)).interrupts[0].value
    response = _bound_response(pending, decision="approve")
    response["case_id"] = "CASE-ATTACKER"

    with pytest.raises(ValueError, match="case_id"):
        await _execute_workflow(
            graph,
            Command(resume=response),
            config,
        )
    assert _operation_count(tmp_path / "operations.sqlite3") == 0


def test_build_workflow_requires_a_real_checkpointer(tmp_path: Path) -> None:
    """Break caught: a compiled HITL graph advertises durability but cannot checkpoint."""
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    gateway = DirectGateway(
        operations=OperationsService(storage_path=tmp_path / "operations.sqlite3")
    )
    with pytest.raises(TypeError, match="checkpointer"):
        build_workflow(gateway=gateway)
    with pytest.raises(TypeError, match="checkpointer"):
        build_workflow(gateway=gateway, checkpointer=object())

    from langgraph.checkpoint.sqlite import SqliteSaver

    connection = sqlite3.connect(tmp_path / "sync-checkpoints.sqlite3")
    try:
        with pytest.raises(TypeError, match="async-compatible"):
            build_workflow(gateway=gateway, checkpointer=SqliteSaver(connection))
    finally:
        connection.close()

    class SyncOnlySaver(BaseCheckpointSaver):
        def get_tuple(self, config):
            return None

        def list(self, config, **kwargs):
            return iter(())

        def put(self, config, checkpoint, metadata, new_versions):
            return config

        def put_writes(self, config, writes, task_id, task_path=""):
            return None

    with pytest.raises(TypeError, match="async-compatible"):
        build_workflow(gateway=gateway, checkpointer=SyncOnlySaver())


def test_compiled_graph_public_surface_is_read_only(tmp_path: Path) -> None:
    """Break caught: callers obtain the mutable Runnable executor from ``runtime.graph``."""
    from langgraph.checkpoint.memory import InMemorySaver

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    graph = build_workflow(
        gateway=DirectGateway(
            operations=OperationsService(storage_path=tmp_path / "operations.sqlite3")
        ),
        checkpointer=InMemorySaver(),
    )

    assert callable(graph.aget_state)
    assert callable(graph.aget_state_history)
    for surface in ("ainvoke", "_execute", "_graph", "compiled_graph", "runner"):
        with pytest.raises(AttributeError, match="not exposed|disabled"):
            getattr(graph, surface)


@pytest.mark.asyncio
async def test_runtime_object_exposes_no_graph_or_executor_capability(tmp_path: Path) -> None:
    """Break caught: normal Runtime attribute access reveals any executable graph object."""
    from recallops.agents.runtime import RecallOpsRuntime

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    ) as runtime:
        for surface in ("graph", "_graph", "_execute", "compiled_graph", "runner"):
            with pytest.raises(AttributeError):
                getattr(runtime, surface)


@pytest.mark.asyncio
async def test_runtime_exposes_detached_checkpoint_history_without_a_runner(tmp_path: Path) -> None:
    """Break caught: removing graph access also removes legitimate historical inspection."""
    from recallops.agents.runtime import RecallOpsRuntime, RuntimeResult

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        review = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Keep checkpoint history available as detached read models.",
            case_id="CASE-READ-HISTORY",
            thread_id="THREAD-READ-HISTORY",
        )
        history = await runtime.get_case_history(thread_id="THREAD-READ-HISTORY")

    assert history
    assert history[0] == review
    assert all(isinstance(item, RuntimeResult) for item in history)
    assert all(item.model_config["frozen"] for item in history)


@pytest.mark.asyncio
async def test_runtime_results_are_recursively_immutable_and_json_presentable(
    tmp_path: Path,
) -> None:
    """Break caught: frozen result models still expose mutable nested dicts and lists."""
    from recallops.agents.runtime import RecallOpsRuntime

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Return an immutable audit view without losing JSON ergonomics.",
            case_id="CASE-IMMUTABLE-RESULT",
            thread_id="THREAD-IMMUTABLE-RESULT",
        )
        history = await runtime.get_case_history(thread_id="THREAD-IMMUTABLE-RESULT")

        with pytest.raises(TypeError):
            result.case["status"] = "forged"
        with pytest.raises(TypeError):
            dict.__setitem__(result.case, "status", "base-class-forged")
        with pytest.raises(TypeError):
            result.case["rag_state"]["read_count"] = 999
        with pytest.raises((AttributeError, TypeError)):
            result.case["node_trace"].append("forged")
        with pytest.raises(TypeError):
            result.pending_interrupt["action"]["rationale"] = "forged"
        with pytest.raises(TypeError):
            history[0].case["status"] = "forged-history"

        presented = result.model_dump(mode="json")
        json_case = json.loads(json.dumps(presented["case"]))
        assert json_case == presented["case"]
        assert json_case["status"] == "review_required"
        presented["case"]["status"] = "presentation-only"
        fresh = await runtime.get_case(thread_id="THREAD-IMMUTABLE-RESULT")

    assert isinstance(result.case["node_trace"], tuple)
    assert fresh == result
    assert fresh.case["status"] == "review_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_key", "extra_value"),
    [
        ("checkpoint_id", "CURRENT"),
        ("checkpoint_ns", "attacker-branch"),
        ("unexpected", "attacker-value"),
    ],
)
async def test_private_executor_rejects_branching_or_extra_config_without_state_change(
    tmp_path: Path,
    extra_key: str,
    extra_value: str,
) -> None:
    """Break caught: a historical/alternate config becomes the latest durable branch."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    thread_id = f"THREAD-CONFIG-{extra_key}"
    graph = build_workflow(
        gateway=DirectGateway(
            operations=OperationsService(storage_path=tmp_path / f"{extra_key}.sqlite3")
        ),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": thread_id}}
    await _execute_workflow(
        graph,
        {
            "case_id": f"CASE-CONFIG-{extra_key}",
            "thread_id": thread_id,
            "recall_number": "H-1230-2026",
            "question": "Historical reads must remain read-only.",
            "scope_lot_ids": [],
        },
        config,
    )
    before = await graph.aget_state(config)
    history_before = [snapshot async for snapshot in graph.aget_state_history(config)]
    pending = before.interrupts[0].value
    value = (
        before.config["configurable"]["checkpoint_id"] if extra_value == "CURRENT" else extra_value
    )
    attack_config = {"configurable": {"thread_id": thread_id, extra_key: value}}

    with pytest.raises(ValueError, match="configurable|execution config"):
        await _execute_workflow(
            graph,
            Command(resume=_bound_response(pending, decision="reject")),
            attack_config,
        )

    after = await graph.aget_state(config)
    history_after = [snapshot async for snapshot in graph.aget_state_history(config)]
    assert after.values == before.values
    assert after.interrupts == before.interrupts
    assert after.config == before.config
    assert history_after == history_before
    if extra_key == "checkpoint_id":
        historical = await graph.aget_state(attack_config)
        assert historical.values == before.values


@pytest.mark.asyncio
async def test_private_executor_rejects_caller_runtime_setting_overrides(tmp_path: Path) -> None:
    """Break caught: callers weaken checkpoint durability through Runnable kwargs."""
    from langgraph.checkpoint.memory import InMemorySaver

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    graph = build_workflow(
        gateway=DirectGateway(
            operations=OperationsService(storage_path=tmp_path / "operations.sqlite3")
        ),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "THREAD-FIXED-SETTINGS"}}

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await _execute_workflow(
            graph,
            {
                "case_id": "CASE-FIXED-SETTINGS",
                "thread_id": "THREAD-FIXED-SETTINGS",
                "recall_number": "H-1230-2026",
                "question": "Runtime execution settings cannot be overridden.",
                "scope_lot_ids": [],
            },
            config,
            durability="exit",
        )

    untouched = await graph.aget_state(config)
    assert untouched.values == {}
    assert "checkpoint_id" not in untouched.config["configurable"]


@pytest.mark.asyncio
async def test_compiled_graph_rejects_reinitializing_a_paused_thread(tmp_path: Path) -> None:
    """Break caught: a second initial dict overwrites the paused case before its review."""
    from langgraph.checkpoint.memory import InMemorySaver

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    gateway = DirectGateway(
        operations=OperationsService(storage_path=tmp_path / "operations.sqlite3")
    )
    graph = build_workflow(gateway=gateway, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "THREAD-DIRECT-REINIT"}}
    initial = {
        "case_id": "THREAD-DIRECT-REINIT",
        "thread_id": "THREAD-DIRECT-REINIT",
        "recall_number": "H-1230-2026",
        "question": "Original investigation question.",
        "scope_lot_ids": [],
    }
    await _execute_workflow(graph, initial, config)
    before = await graph.aget_state(config)

    with pytest.raises(ValueError, match="already has a durable checkpoint"):
        await _execute_workflow(
            graph,
            {**initial, "question": "Overwrite the paused investigation."},
            config,
        )

    after = await graph.aget_state(config)
    assert after.values == before.values
    assert after.interrupts == before.interrupts
    assert after.config == before.config
    assert _workflow_active_lock_count(graph) == 0

    with pytest.raises(AttributeError, match="disabled"):
        await graph.aupdate_state(config, {"case_id": "CASE-OVERWRITE"})
    final = await graph.aget_state(config)
    assert final.values == before.values
    assert final.config == before.config


@pytest.mark.asyncio
async def test_compiled_graph_exposes_no_alternate_execution_or_mutation_runner(
    tmp_path: Path,
) -> None:
    """Break caught: stream/batch/config wrappers bypass guarded ``ainvoke``."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from recallops.agents.workflow import build_workflow
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    operations = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    graph = build_workflow(
        gateway=DirectGateway(operations=operations),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "THREAD-SEALED-RUNNERS"}}
    await _execute_workflow(
        graph,
        {
            "case_id": "CASE-SEALED-RUNNERS",
            "thread_id": "THREAD-SEALED-RUNNERS",
            "recall_number": "H-1230-2026",
            "question": "Keep every alternate runner away from a paused checkpoint.",
            "scope_lot_ids": [],
        },
        config,
    )
    before = await graph.aget_state(config)

    runner_and_mutator_surfaces = (
        "invoke",
        "stream",
        "astream",
        "stream_events",
        "astream_events",
        "astream_log",
        "batch",
        "abatch",
        "batch_as_completed",
        "abatch_as_completed",
        "transform",
        "atransform",
        "update_state",
        "aupdate_state",
        "bulk_update_state",
        "abulk_update_state",
        "with_config",
        "with_retry",
        "with_fallbacks",
        "with_listeners",
        "with_alisteners",
        "with_types",
        "bind",
        "assign",
        "map",
        "pick",
        "pipe",
        "as_tool",
        "copy",
        "clear_cache",
        "aclear_cache",
    )
    for surface in runner_and_mutator_surfaces:
        with pytest.raises(AttributeError, match="not exposed|disabled"):
            getattr(graph, surface)

    pending = before.interrupts[0].value
    with pytest.raises(ValueError, match="resume-only"):
        await _execute_workflow(
            graph,
            Command(
                update={"case_id": "CASE-COMMAND-OVERWRITE"},
                resume=_bound_response(pending, decision="reject"),
            ),
            config,
        )

    after = await graph.aget_state(config)
    assert after.values == before.values
    assert after.interrupts == before.interrupts
    assert after.config == before.config


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
            case_id="THREAD-EDIT",
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


@pytest.mark.asyncio
async def test_edit_cannot_change_required_transition_scope_or_evidence(tmp_path: Path) -> None:
    """Break caught: edit bypasses create→hold order or invents/removes reviewed scope."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Preserve the required action transition during review edits.",
            case_id="THREAD-EDIT-SAFETY",
            thread_id="THREAD-EDIT-SAFETY",
        )
        original_action = deepcopy(result.pending_interrupt["action"])
        original_digest = result.pending_interrupt["action_digest"]

        changed_type = {**deepcopy(original_action), "action_type": "create_facility_tasks"}
        subset = deepcopy(original_action)
        kept_target = subset["target_ids"][0]
        subset["target_ids"] = [kept_target]
        subset["evidence_by_target"] = {kept_target: subset["evidence_by_target"][kept_target]}
        subset["evidence_ids"] = subset["evidence_by_target"][kept_target]
        invented = deepcopy(original_action)
        first_target = invented["target_ids"][0]
        invented["evidence_by_target"][first_target] = [
            *invented["evidence_by_target"][first_target],
            "INVENTED-EVIDENCE",
        ]
        invented["evidence_ids"] = [*invented["evidence_ids"], "INVENTED-EVIDENCE"]
        premature_close = {
            **deepcopy(original_action),
            "action_type": "close_case",
            "target_ids": [],
            "evidence_ids": [],
            "evidence_by_target": {},
        }
        disposition = {
            **deepcopy(original_action),
            "action_type": "record_disposition",
            "target_ids": [original_action["target_ids"][0]],
            "evidence_ids": original_action["evidence_by_target"][original_action["target_ids"][0]],
            "evidence_by_target": {
                original_action["target_ids"][0]: original_action["evidence_by_target"][
                    original_action["target_ids"][0]
                ]
            },
        }

        for edited_action in (
            changed_type,
            subset,
            invented,
            premature_close,
            disposition,
        ):
            response = _bound_response(result.pending_interrupt, decision="edit")
            response["edited_action"] = edited_action
            result = await runtime.resume_case(
                thread_id="THREAD-EDIT-SAFETY",
                response=response,
            )
            assert result.case["status"] == "review_required"
            assert result.pending_interrupt["action"] == original_action
            assert result.pending_interrupt["action_digest"] == original_digest
            assert "execution_confirmation" not in result.next_nodes

    assert _operation_count(operations_path) == 0


@pytest.mark.asyncio
async def test_probable_only_case_runs_one_operation_per_version_through_closure(
    tmp_path: Path,
) -> None:
    """Break caught: a closure bypasses tasks/acks or combines multiple versioned writes."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Trace and contain LOT-PROBABLE-160, then close only if every gate passes.",
            case_id="THREAD-PROBABLE",
            thread_id="THREAD-PROBABLE",
            scope_lot_ids=["LOT-PROBABLE-160"],
        )
        reviewed_actions: list[str] = []
        versions: list[int] = []
        while result.pending_interrupt is not None:
            pending = result.pending_interrupt
            if pending["kind"] in {"action_review", "closure_review"}:
                reviewed_actions.append(pending["action"]["action_type"])
                if pending["action"]["action_type"] == "create_case":
                    assert pending["remaining_action_types"] == [
                        "apply_inventory_hold",
                        "create_facility_tasks",
                        "record_acknowledgment",
                        "record_acknowledgment",
                        "close_case",
                    ]
                if (
                    pending["action"]["action_type"] == "record_acknowledgment"
                    and reviewed_actions.count("record_acknowledgment") == 1
                ):
                    assert pending["remaining_action_types"] == [
                        "record_acknowledgment",
                        "close_case",
                    ]
                result = await runtime.resume_case(
                    thread_id="THREAD-PROBABLE",
                    response=_bound_response(pending, decision="approve"),
                )
            elif pending["kind"] == "execution_confirmation":
                previous_count = _operation_count(operations_path)
                result = await runtime.resume_case(
                    thread_id="THREAD-PROBABLE",
                    response=_bound_response(pending, decision="confirm"),
                )
                assert _operation_count(operations_path) == previous_count + 1
                versions.append(result.case["case_version"])
            else:  # pragma: no cover - the assertion gives a clearer failure than KeyError
                raise AssertionError(f"unexpected interrupt: {pending['kind']}")

    assert reviewed_actions == [
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_acknowledgment",
        "close_case",
    ]
    assert versions == [1, 2, 3, 4, 5, 6]
    assert result.case["status"] == "closed"
    assert result.case["closure_outcome"] == {"eligible": True, "closed": True}
    assert [receipt["action_type"] for receipt in result.case["write_receipts"]] == reviewed_actions


@pytest.mark.asyncio
async def test_unaccounted_lot_disposition_is_reviewed_confirmed_and_restart_safe(
    tmp_path: Path,
) -> None:
    """Break caught: required disposition is absent from the durable action lifecycle."""
    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Resolve LOT-EXACT-170 unaccounted units before facility follow-up.",
            case_id="CASE-DISPOSITION",
            thread_id="THREAD-DISPOSITION",
            scope_lot_ids=["LOT-EXACT-170"],
        )
        assert result.pending_interrupt["remaining_action_types"] == [
            "apply_inventory_hold",
            "record_disposition",
            "create_facility_tasks",
            *(["record_acknowledgment"] * len(result.case["required_facilities"])),
            "close_case",
        ]
        while result.pending_interrupt["action"]["action_type"] != "record_disposition":
            review = result.pending_interrupt
            confirmation = await runtime.resume_case(
                thread_id="THREAD-DISPOSITION",
                response=_bound_response(review, decision="approve"),
            )
            result = await runtime.resume_case(
                thread_id="THREAD-DISPOSITION",
                response=_bound_response(confirmation.pending_interrupt, decision="confirm"),
            )
        disposition_review = result

    assert disposition_review.case["case_version"] == 2
    assert disposition_review.pending_interrupt["action"]["target_ids"] == ["LOT-EXACT-170"]
    assert "dispose_unaccounted" in disposition_review.pending_interrupt["action"]["rationale"]
    assert _operation_count(operations_path) == 2

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        restored = await runtime.get_case(thread_id="THREAD-DISPOSITION")
        assert restored == disposition_review
        confirmation = await runtime.resume_case(
            thread_id="THREAD-DISPOSITION",
            response=_bound_response(restored.pending_interrupt, decision="approve"),
        )

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        restored = await runtime.get_case(thread_id="THREAD-DISPOSITION")
        assert restored == confirmation
        disposed = await runtime.resume_case(
            thread_id="THREAD-DISPOSITION",
            response=_bound_response(restored.pending_interrupt, decision="confirm"),
        )

    assert disposed.case["case_version"] == 3
    assert disposed.case["write_receipts"][-1]["action_type"] == "record_disposition"
    assert disposed.case["reconciliations"][0]["unaccounted"] == 0
    assert disposed.case["evidence_gaps"] == []
    assert disposed.pending_interrupt["action"]["action_type"] == "create_facility_tasks"
    assert _operation_count(operations_path) == 3

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        result = disposed
        while result.pending_interrupt is not None:
            pending = result.pending_interrupt
            decision = (
                "approve" if pending["kind"] in {"action_review", "closure_review"} else "confirm"
            )
            result = await runtime.resume_case(
                thread_id="THREAD-DISPOSITION",
                response=_bound_response(pending, decision=decision),
            )

    assert result.case["status"] == "open_closure_blocked"
    assert result.pending_interrupt is None
    assert "close_case" not in [receipt["action_type"] for receipt in result.case["write_receipts"]]


@pytest.mark.asyncio
async def test_read_and_model_failures_are_labelled_or_fail_closed(tmp_path: Path) -> None:
    """Break caught: a dependency failure silently invents evidence or drops provenance."""
    from recallops.agents.runtime import RecallOpsRuntime

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "fallback-checkpoints.sqlite3",
        operations_path=tmp_path / "fallback-operations.sqlite3",
    ) as runtime:
        runtime.inject_failure("registry_transient_failure")
        runtime.inject_failure("model_failure")
        fallback = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Investigate the affected lots with cited official evidence.",
            case_id="THREAD-FALLBACK",
            thread_id="THREAD-FALLBACK",
        )

    assert fallback.pending_interrupt["official_evidence"]["provenance"] == (
        "OFFICIAL_OPENFDA_SNAPSHOT"
    )
    assert any(
        "pinned OFFICIAL_OPENFDA_SNAPSHOT fallback" in item for item in fallback.case["warnings"]
    )
    assert fallback.case["model_trace"] == [
        {
            "operation": "deep_supervisor_reasoning",
            "status": "fallback",
            "fallback": "deterministic_fixed_specialists",
        }
    ]
    registry_attempts = [
        item for item in fallback.case["tool_trace"] if item["operation"] == "get_recall"
    ]
    assert [item["status"] for item in registry_attempts] == ["retry", "error"]
    assert fallback.case["retry_state"]["read_attempts"] == 2

    for scenario in ("unavailable_evidence", "malformed_evidence"):
        async with RecallOpsRuntime.open(
            checkpoint_path=tmp_path / f"{scenario}-checkpoints.sqlite3",
            operations_path=tmp_path / f"{scenario}-operations.sqlite3",
        ) as runtime:
            runtime.inject_failure(scenario)
            blocked = await runtime.start_case(
                recall_number="H-1230-2026",
                question="Investigate without fabricating missing evidence.",
                case_id=f"THREAD-{scenario}",
                thread_id=f"THREAD-{scenario}",
            )
        assert blocked.pending_interrupt is None
        assert blocked.case["status"] == "escalated"
        assert blocked.case["failure_state"]["stage"] == "regulatory_intake"
        assert _operation_count(tmp_path / f"{scenario}-operations.sqlite3") == 0


@pytest.mark.asyncio
async def test_watchdog_and_ambiguous_scope_escalate_without_a_write_gate(tmp_path: Path) -> None:
    """Break caught: stalled reasoning or ambiguous-only scope can reach containment."""
    from recallops.agents.runtime import RecallOpsRuntime

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "watchdog-checkpoints.sqlite3",
        operations_path=tmp_path / "watchdog-operations.sqlite3",
    ) as runtime:
        runtime.inject_failure("repeated_progress_signature")
        stalled = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Investigate the recall.",
            case_id="THREAD-STALLED",
            thread_id="THREAD-STALLED",
        )
    assert stalled.case["status"] == "escalated"
    assert stalled.case["watchdog"]["repeat_count"] == 2
    assert stalled.case["node_trace"] == ["intake", "retrieve_context"]
    assert stalled.pending_interrupt is None

    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "ambiguous-checkpoints.sqlite3",
        operations_path=tmp_path / "ambiguous-operations.sqlite3",
    ) as runtime:
        ambiguous = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Can LOT-AMBIG-175 be held?",
            case_id="THREAD-AMBIGUOUS",
            thread_id="THREAD-AMBIGUOUS",
            scope_lot_ids=["LOT-AMBIG-175"],
        )
    assert ambiguous.case["status"] == "escalated"
    assert ambiguous.pending_interrupt is None
    assert _operation_count(tmp_path / "ambiguous-operations.sqlite3") == 0


@pytest.mark.parametrize(
    "scenario",
    [
        "rag_failure",
        "find_candidate_products_transient_failure",
        "find_candidate_products_malformed_evidence",
        "match_lots_transient_failure",
        "match_lots_malformed_evidence",
        "trace_forward_transient_failure",
        "trace_forward_malformed_evidence",
        "trace_backward_transient_failure",
        "trace_backward_malformed_evidence",
        "get_inventory_transient_failure",
        "get_inventory_malformed_evidence",
        "reconcile_units_transient_failure",
        "reconcile_units_malformed_evidence",
    ],
)
@pytest.mark.asyncio
async def test_rag_and_every_traceability_read_fail_to_a_terminal_checkpoint(
    tmp_path: Path,
    scenario: str,
) -> None:
    """Break caught: a read failure strands an investigating checkpoint with no interrupt."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        runtime.inject_failure(scenario)
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Fail closed if required investigation evidence is unavailable.",
            case_id=f"THREAD-READ-FAIL-{scenario}",
            thread_id=f"THREAD-READ-FAIL-{scenario}",
        )

    assert result.case["status"] == "escalated"
    assert result.case["failure_state"]["stage"] in {
        "retrieve_context",
        "product_lot_match",
        "trace_forward_backward",
        "reconcile",
    }
    assert result.pending_interrupt is None
    assert result.next_nodes == ()
    assert _operation_count(operations_path) == 0


@pytest.mark.asyncio
async def test_closure_review_survives_restart_before_final_confirmation(tmp_path: Path) -> None:
    """Break caught: the final closure review is transient or doubles the close receipt."""
    from recallops.agents.runtime import RecallOpsRuntime

    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Contain LOT-PROBABLE-160 and close only after all acknowledgments.",
            case_id="THREAD-CLOSURE-RESTART",
            thread_id="THREAD-CLOSURE-RESTART",
            scope_lot_ids=["LOT-PROBABLE-160"],
        )
        while result.pending_interrupt["kind"] != "closure_review":
            pending = result.pending_interrupt
            decision = "approve" if pending["kind"] == "action_review" else "confirm"
            result = await runtime.resume_case(
                thread_id="THREAD-CLOSURE-RESTART",
                response=_bound_response(pending, decision=decision),
            )
        closure_review = result

    assert closure_review.case["status"] == "closure_review_required"
    assert _operation_count(operations_path) == 5

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        restored = await runtime.get_case(thread_id="THREAD-CLOSURE-RESTART")
        assert restored == closure_review
        confirmation = await runtime.resume_case(
            thread_id="THREAD-CLOSURE-RESTART",
            response=_bound_response(restored.pending_interrupt, decision="approve"),
        )

    assert confirmation.pending_interrupt["kind"] == "execution_confirmation"
    assert _operation_count(operations_path) == 5

    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint_path,
        operations_path=operations_path,
    ) as runtime:
        restored_confirmation = await runtime.get_case(thread_id="THREAD-CLOSURE-RESTART")
        closed = await runtime.resume_case(
            thread_id="THREAD-CLOSURE-RESTART",
            response=_bound_response(
                restored_confirmation.pending_interrupt,
                decision="confirm",
            ),
        )

    assert closed.case["status"] == "closed"
    assert closed.case["case_version"] == 6


@pytest.mark.asyncio
async def test_closure_rationale_edit_returns_to_closure_review(tmp_path: Path) -> None:
    """Break caught: a valid closure edit is routed to ordinary action review."""
    from recallops.agents.runtime import RecallOpsRuntime

    operations_path = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations_path,
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Reach closure and re-review an edited closure rationale.",
            case_id="CASE-CLOSURE-EDIT",
            thread_id="THREAD-CLOSURE-EDIT",
            scope_lot_ids=["LOT-PROBABLE-160"],
        )
        while result.pending_interrupt["kind"] != "closure_review":
            pending = result.pending_interrupt
            decision = "approve" if pending["kind"] == "action_review" else "confirm"
            result = await runtime.resume_case(
                thread_id="THREAD-CLOSURE-EDIT",
                response=_bound_response(pending, decision=decision),
            )

        closure = result.pending_interrupt
        edited_action = deepcopy(closure["action"])
        edited_action["rationale"] = (
            "Close only after a second review of every authoritative closure invariant."
        )
        response = _bound_response(closure, decision="edit")
        response["edited_action"] = edited_action
        edited = await runtime.resume_case(
            thread_id="THREAD-CLOSURE-EDIT",
            response=response,
        )

    assert edited.case["status"] == "closure_review_required"
    assert edited.pending_interrupt["kind"] == "closure_review"
    assert edited.pending_interrupt["action"]["rationale"] == edited_action["rationale"]
    assert _operation_count(operations_path) == 5
