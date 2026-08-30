import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pytest

from recallops.models import ApprovalDecision, Reconciliation
from recallops.services.operations import (
    ApprovalRequiredError,
    ClosureBlockedError,
    IdempotencyConflictError,
    OperationsService,
    OperationStoreError,
    StaleCaseVersionError,
)


def approval(version: int = 0) -> ApprovalDecision:
    return ApprovalDecision(
        decision="approve",
        actor="reviewer",
        justification="evidence",
        approved_at=datetime(2026, 8, 30, 12, version, tzinfo=UTC),
        approved_case_version=version,
    )


def reconciliation(*, unaccounted: int = 0, verified: bool = True) -> Reconciliation:
    on_hand = 10 - unaccounted
    return Reconciliation(
        lot_id="LOT-EXACT-170",
        received=10,
        on_hand=on_hand,
        quarantined=0,
        sold=0,
        returned=0,
        disposed=0,
        unaccounted=unaccounted,
        verified=verified,
        evidence_ids=["EV-RECEIVE", "INV-EXACT"],
        component_evidence={
            "received": ["EV-RECEIVE"],
            "on_hand": ["INV-EXACT"],
            "quarantined": [],
            "sold": [],
            "returned": [],
            "disposed": [],
            "unaccounted": ["EV-RECEIVE", "INV-EXACT"],
        },
    )


def case_input(
    *, unaccounted: int = 0, verified: bool = True, evidence_gaps: list[str] | None = None
) -> dict[str, Any]:
    return {
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": ["LOT-EXACT-170"],
        "trace_event_ids": ["EV-RECEIVE"],
        "required_facilities": ["DC-NORTH"],
        "reconciliation": [reconciliation(unaccounted=unaccounted, verified=verified)],
        "evidence_gaps": evidence_gaps or [],
    }


def create(service: OperationsService, case_id: str, *, unaccounted: int = 0) -> None:
    service.create_case(
        case_id=case_id,
        **case_input(unaccounted=unaccounted),
        approval=approval(0),
        expected_case_version=0,
        idempotency_key=f"{case_id}-create",
    )


def _process_hold(database: str, key: str) -> str:
    try:
        OperationsService(storage_path=Path(database)).apply_inventory_hold(
            case_id="CASE-CAS",
            lot_ids=["LOT-EXACT-170"],
            approval=approval(1),
            expected_case_version=1,
            idempotency_key=key,
        )
        return "won"
    except StaleCaseVersionError:
        return "stale"


def _process_close_or_task(database: str, action: str) -> str:
    service = OperationsService(storage_path=Path(database))
    try:
        if action == "close":
            service.close_case(
                case_id="CASE-RACE",
                approval=approval(3),
                expected_case_version=3,
                idempotency_key="race-close",
            )
        else:
            service.create_facility_tasks(
                case_id="CASE-RACE",
                facility_ids=["STORE-01"],
                approval=approval(3),
                expected_case_version=3,
                idempotency_key="race-task",
            )
        return action
    except StaleCaseVersionError:
        return "stale"


def test_sqlite_idempotency_binds_the_full_request_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    first = OperationsService(storage_path=database)
    approved = approval(0)
    receipt = first.create_case(
        case_id="CASE-SQL",
        **case_input(),
        approval=approved,
        expected_case_version=0,
        idempotency_key="request-1",
    )
    assert (
        OperationsService(storage_path=database)
        .create_case(
            case_id="CASE-SQL",
            **case_input(),
            approval=approved,
            expected_case_version=0,
            idempotency_key="request-1",
        )
        .receipt_id
        == receipt.receipt_id
    )
    with pytest.raises(IdempotencyConflictError):
        OperationsService(storage_path=database).create_case(
            case_id="OTHER",
            **case_input(),
            approval=approval(0),
            expected_case_version=0,
            idempotency_key="request-1",
        )


def test_sqlite_compare_and_swap_allows_only_one_separate_process_expected_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    create(OperationsService(storage_path=database), "CASE-CAS")

    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        outcomes = set(pool.map(_process_hold, [str(database)] * 2, ["hold-a", "hold-b"]))
    assert outcomes == {"won", "stale"}


def test_idempotency_binds_approval_identity_and_close_replays(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    service = OperationsService(storage_path=database)
    approved = approval(0)
    service.create_case(
        case_id="CASE-REPLAY",
        **case_input(unaccounted=1),
        approval=approved,
        expected_case_version=0,
        idempotency_key="create",
    )
    with pytest.raises(IdempotencyConflictError):
        service.create_case(
            case_id="CASE-REPLAY",
            **case_input(unaccounted=1),
            approval=ApprovalDecision(
                decision="approve",
                actor="other",
                justification="evidence",
                approved_at=datetime.now(UTC),
                approved_case_version=0,
            ),
            expected_case_version=0,
            idempotency_key="create",
        )
    service.record_disposition(
        case_id="CASE-REPLAY",
        lot_id="LOT-EXACT-170",
        disposition="dispose_unaccounted",
        evidence_id="EV-DISPOSE",
        approval=approval(1),
        expected_case_version=1,
        idempotency_key="dispose",
    )
    service.create_facility_tasks(
        case_id="CASE-REPLAY",
        facility_ids=["DC-NORTH"],
        approval=approval(2),
        expected_case_version=2,
        idempotency_key="tasks",
    )
    service.record_acknowledgment(
        case_id="CASE-REPLAY",
        facility_id="DC-NORTH",
        approval=approval(3),
        expected_case_version=3,
        idempotency_key="ack",
    )
    first = service.close_case(
        case_id="CASE-REPLAY",
        approval=approval(4),
        expected_case_version=4,
        idempotency_key="close",
    )
    assert (
        OperationsService(storage_path=database)
        .close_case(
            case_id="CASE-REPLAY",
            approval=approval(4),
            expected_case_version=4,
            idempotency_key="close",
        )
        .receipt_id
        == first.receipt_id
    )


def test_create_case_requires_real_evidence_and_fresh_case_cannot_close(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    for missing in (
        "confirmed_lot_ids",
        "trace_event_ids",
        "required_facilities",
        "reconciliation",
    ):
        payload = case_input()
        payload[missing] = []
        with pytest.raises(ValueError, match=missing):
            service.create_case(
                case_id=f"CASE-EMPTY-{missing}",
                **payload,
                approval=approval(0),
                expected_case_version=0,
                idempotency_key=f"empty-{missing}",
            )

    create(service, "CASE-FRESH")
    with pytest.raises(ClosureBlockedError, match="task|acknowledgement"):
        service.close_case(
            case_id="CASE-FRESH",
            approval=approval(1),
            expected_case_version=1,
            idempotency_key="fresh-close",
        )


def test_realistic_case_sequence_persists_tasks_and_closes(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    service = OperationsService(storage_path=database)
    create(service, "CASE-SUCCESS")
    service.create_facility_tasks(
        case_id="CASE-SUCCESS",
        facility_ids=["DC-NORTH"],
        approval=approval(1),
        expected_case_version=1,
        idempotency_key="success-tasks",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM tasks WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-NORTH"),
        ).fetchone() == ("pending",)
        assert connection.execute(
            "SELECT acknowledged FROM acknowledgements WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-NORTH"),
        ).fetchone() == (0,)
    service.record_acknowledgment(
        case_id="CASE-SUCCESS",
        facility_id="DC-NORTH",
        approval=approval(2),
        expected_case_version=2,
        idempotency_key="success-ack",
    )
    receipt = service.close_case(
        case_id="CASE-SUCCESS",
        approval=approval(3),
        expected_case_version=3,
        idempotency_key="success-close",
    )

    state = service.get_case("CASE-SUCCESS")
    assert receipt.case_version == 4
    assert state is not None and state.status == "closed"
    with sqlite3.connect(database) as connection:
        task = connection.execute(
            "SELECT status FROM tasks WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-NORTH"),
        ).fetchone()
        acknowledgement = connection.execute(
            "SELECT acknowledged FROM acknowledgements WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-NORTH"),
        ).fetchone()
    assert task == ("acknowledged",)
    assert acknowledgement == (1,)


def test_acknowledgement_requires_a_persisted_task(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-NO-TASK")
    with pytest.raises(ClosureBlockedError, match="existing facility task"):
        service.record_acknowledgment(
            case_id="CASE-NO-TASK",
            facility_id="DC-NORTH",
            approval=approval(1),
            expected_case_version=1,
            idempotency_key="no-task-ack",
        )


def test_disposition_rejects_values_outside_the_concrete_domain(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-BAD-DISPOSITION", unaccounted=1)
    with pytest.raises(ValueError, match="disposition"):
        service.record_disposition(
            case_id="CASE-BAD-DISPOSITION",
            lot_id="LOT-EXACT-170",
            disposition="made_up",
            evidence_id="EV-NOPE",
            approval=approval(1),
            expected_case_version=1,
            idempotency_key="bad-disposition",
        )


def test_failure_after_receipt_insert_rolls_back_the_whole_transaction(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"

    def fail(stage: str) -> None:
        if stage == "after_receipt_insert":
            raise RuntimeError("injected receipt failure")

    service = OperationsService(storage_path=database, failure_injector=fail)
    with pytest.raises(RuntimeError, match="injected receipt failure"):
        create(service, "CASE-ROLLBACK")

    recovered = OperationsService(storage_path=database)
    assert recovered.get_case("CASE-ROLLBACK") is None
    create(recovered, "CASE-ROLLBACK")
    assert recovered.get_case("CASE-ROLLBACK").case_version == 1


def test_corrupt_or_invalid_database_fails_with_typed_store_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a sqlite database")
    with pytest.raises(OperationStoreError):
        OperationsService(storage_path=corrupt)

    invalid = tmp_path / "directory.sqlite3"
    invalid.mkdir()
    with pytest.raises(OperationStoreError):
        OperationsService(storage_path=invalid)


def test_concurrent_task_creation_can_never_leave_a_closed_case_with_pending_ack(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    service = OperationsService(storage_path=database)
    create(service, "CASE-RACE")
    service.create_facility_tasks(
        case_id="CASE-RACE",
        facility_ids=["DC-NORTH"],
        approval=approval(1),
        expected_case_version=1,
        idempotency_key="race-initial-task",
    )
    service.record_acknowledgment(
        case_id="CASE-RACE",
        facility_id="DC-NORTH",
        approval=approval(2),
        expected_case_version=2,
        idempotency_key="race-initial-ack",
    )

    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        outcomes = set(pool.map(_process_close_or_task, [str(database)] * 2, ["close", "task"]))
    assert "stale" in outcomes

    state = service.get_case("CASE-RACE")
    with sqlite3.connect(database) as connection:
        pending_count = connection.execute(
            """
            SELECT COUNT(*) FROM tasks AS task
            LEFT JOIN acknowledgements AS ack
              ON ack.case_id=task.case_id AND ack.facility_id=task.facility_id
            WHERE task.case_id=? AND COALESCE(ack.acknowledged, 0)=0
            """,
            ("CASE-RACE",),
        ).fetchone()[0]
    assert not (state is not None and state.status == "closed" and pending_count)


def test_no_new_task_can_be_added_after_close(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-CLOSED")
    service.create_facility_tasks(
        case_id="CASE-CLOSED",
        facility_ids=["DC-NORTH"],
        approval=approval(1),
        expected_case_version=1,
        idempotency_key="closed-task",
    )
    service.record_acknowledgment(
        case_id="CASE-CLOSED",
        facility_id="DC-NORTH",
        approval=approval(2),
        expected_case_version=2,
        idempotency_key="closed-ack",
    )
    service.close_case(
        case_id="CASE-CLOSED",
        approval=approval(3),
        expected_case_version=3,
        idempotency_key="closed-close",
    )
    with pytest.raises(ClosureBlockedError, match="closed"):
        service.create_facility_tasks(
            case_id="CASE-CLOSED",
            facility_ids=["STORE-01"],
            approval=approval(4),
            expected_case_version=4,
            idempotency_key="closed-late-task",
        )


@pytest.mark.parametrize(
    ("case_id", "case_payload", "message"),
    [
        ("CASE-GAP", case_input(evidence_gaps=["missing shipment"]), "unresolved evidence"),
        ("CASE-UNVERIFIED", case_input(verified=False), "not verified"),
    ],
)
def test_close_requires_zero_gaps_and_verified_reconciliation(
    tmp_path: Path, case_id: str, case_payload: dict[str, Any], message: str
) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    service.create_case(
        case_id=case_id,
        **case_payload,
        approval=approval(0),
        expected_case_version=0,
        idempotency_key=f"{case_id}-create",
    )
    service.create_facility_tasks(
        case_id=case_id,
        facility_ids=["DC-NORTH"],
        approval=approval(1),
        expected_case_version=1,
        idempotency_key=f"{case_id}-tasks",
    )
    service.record_acknowledgment(
        case_id=case_id,
        facility_id="DC-NORTH",
        approval=approval(2),
        expected_case_version=2,
        idempotency_key=f"{case_id}-ack",
    )
    with pytest.raises(ClosureBlockedError, match=message):
        service.close_case(
            case_id=case_id,
            approval=approval(3),
            expected_case_version=3,
            idempotency_key=f"{case_id}-close",
        )


def test_close_rejects_approval_for_an_older_case_version(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-OLD-APPROVAL")
    service.create_facility_tasks(
        case_id="CASE-OLD-APPROVAL",
        facility_ids=["DC-NORTH"],
        approval=approval(1),
        expected_case_version=1,
        idempotency_key="old-approval-tasks",
    )
    service.record_acknowledgment(
        case_id="CASE-OLD-APPROVAL",
        facility_id="DC-NORTH",
        approval=approval(2),
        expected_case_version=2,
        idempotency_key="old-approval-ack",
    )
    with pytest.raises(ApprovalRequiredError, match="current expected case version"):
        service.close_case(
            case_id="CASE-OLD-APPROVAL",
            approval=approval(2),
            expected_case_version=3,
            idempotency_key="old-approval-close",
        )


def test_close_requires_reconciliation_event_evidence_in_the_case_trace(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    payload = case_input()
    payload["trace_event_ids"] = ["EV-UNRELATED"]
    service.create_case(
        case_id="CASE-EVIDENCE-MISMATCH",
        **payload,
        approval=approval(0),
        expected_case_version=0,
        idempotency_key="evidence-mismatch-create",
    )
    service.create_facility_tasks(
        case_id="CASE-EVIDENCE-MISMATCH",
        facility_ids=["DC-NORTH"],
        approval=approval(1),
        expected_case_version=1,
        idempotency_key="evidence-mismatch-tasks",
    )
    service.record_acknowledgment(
        case_id="CASE-EVIDENCE-MISMATCH",
        facility_id="DC-NORTH",
        approval=approval(2),
        expected_case_version=2,
        idempotency_key="evidence-mismatch-ack",
    )
    with pytest.raises(ClosureBlockedError, match="trace evidence"):
        service.close_case(
            case_id="CASE-EVIDENCE-MISMATCH",
            approval=approval(3),
            expected_case_version=3,
            idempotency_key="evidence-mismatch-close",
        )
