import json
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from recallops.models import (
    ApprovalBinding,
    ApprovalDecision,
    ProposedAction,
    Reconciliation,
    proposed_action_digest,
)
from recallops.services.operations import (
    ApprovalRequiredError,
    ClosureBlockedError,
    IdempotencyConflictError,
    OperationsService,
    OperationStoreError,
    StaleCaseVersionError,
)
from recallops.services.traceability import TraceabilityService

TRACEABILITY = TraceabilityService()
SUCCESS_LOT = "LOT-PROBABLE-160"
GAPPED_LOT = "LOT-EXACT-170"


def reviewed(
    case_id: str,
    action_type: str,
    version: int,
    target_ids: list[str],
    *,
    evidence_ids: list[str] | None = None,
    actor: str = "reviewer",
) -> dict[str, Any]:
    action_evidence = (
        evidence_ids or [f"EVIDENCE-{target_id}" for target_id in target_ids] if target_ids else []
    )
    action = ProposedAction(
        action_id=f"{case_id}-{action_type}-{version}",
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids,
        rationale=f"Reviewed {action_type} against authoritative evidence.",
        evidence_ids=action_evidence,
        evidence_by_target={target_id: action_evidence for target_id in target_ids},
        expected_case_version=version,
    )
    approval = ApprovalDecision(
        decision="approve",
        actor=actor,
        justification="evidence",
        approved_at=datetime(2026, 8, 30, 12, version, tzinfo=UTC),
        approved_case_version=version,
        approved_case_id=case_id,
        action_ids=[action.action_id],
        action_bindings=[
            ApprovalBinding(
                action_id=action.action_id,
                action_digest=proposed_action_digest(action),
            )
        ],
    )
    return {"proposed_action": action, "approval": approval}


def reconciliation(*, lot_id: str = SUCCESS_LOT, verified: bool = True) -> Reconciliation:
    value = TRACEABILITY.reconcile_units(lot_id)
    return value.model_copy(update={"verified": verified})


def case_input(
    *,
    lot_id: str = SUCCESS_LOT,
    verified: bool = True,
    evidence_gaps: list[str] | None = None,
) -> dict[str, Any]:
    events = TRACEABILITY.trace_forward(lot_id)
    return {
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": [lot_id],
        "trace_event_ids": [event["event_id"] for event in events],
        "required_facilities": sorted(
            {
                facility
                for event in events
                for facility in (event.get("from_facility"), event.get("to_facility"))
                if facility
            }
        ),
        "reconciliation": [reconciliation(lot_id=lot_id, verified=verified)],
        "evidence_gaps": evidence_gaps or [],
    }


def create(service: OperationsService, case_id: str, *, lot_id: str = SUCCESS_LOT) -> None:
    payload = case_input(lot_id=lot_id)
    service.create_case(
        case_id=case_id,
        **payload,
        **reviewed(
            case_id,
            "create_case",
            0,
            payload["confirmed_lot_ids"],
            evidence_ids=payload["trace_event_ids"],
        ),
        expected_case_version=0,
        idempotency_key=f"{case_id}-create",
    )


def test_legacy_cases_are_transactionally_backfilled_into_case_threads(tmp_path: Path) -> None:
    database = tmp_path / "legacy-backfill.sqlite3"
    service = OperationsService(storage_path=database)
    create(service, "CASE-LEGACY-ONE")
    create(service, "CASE-LEGACY-TWO")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE case_threads")
        connection.execute("DROP TABLE workflow_identities")
        row = connection.execute(
            "SELECT state_json FROM cases WHERE case_id='CASE-LEGACY-TWO'"
        ).fetchone()
        state = json.loads(row[0])
        state["thread_id"] = "CASE-LEGACY-ONE"
        connection.execute(
            "UPDATE cases SET state_json=? WHERE case_id='CASE-LEGACY-TWO'",
            (json.dumps(state),),
        )

    with pytest.raises(OperationStoreError, match="identity|thread"):
        OperationsService(storage_path=database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM case_threads").fetchone()[0] == 0

    with sqlite3.connect(database) as connection:
        state["thread_id"] = "THREAD-LEGACY-TWO"
        connection.execute(
            "UPDATE cases SET state_json=? WHERE case_id='CASE-LEGACY-TWO'",
            (json.dumps(state),),
        )
    migrated = OperationsService(storage_path=database)
    assert migrated.get_thread_for_case("CASE-LEGACY-ONE") == "CASE-LEGACY-ONE"
    assert migrated.get_thread_for_case("CASE-LEGACY-TWO") == "THREAD-LEGACY-TWO"
    assert migrated.get_case_for_thread("THREAD-LEGACY-TWO").case_id == "CASE-LEGACY-TWO"


def test_legacy_create_hash_replays_once_then_migrates_to_thread_bound_hash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-hash.sqlite3"
    service = OperationsService(storage_path=database)
    case_id = "CASE-LEGACY-HASH"
    payload = case_input()
    decision = reviewed(
        case_id,
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    receipt = service.create_case(
        case_id=case_id,
        **payload,
        **decision,
        expected_case_version=0,
        idempotency_key="legacy-create-key",
    )
    legacy_details = dict(receipt.details)
    legacy_details.pop("reviewed_action")
    legacy_details.pop("thread_id")
    legacy_hash = service._request_hash(
        case_id,
        "create_case",
        0,
        legacy_details,
        decision["approval"],
        decision["proposed_action"],
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE receipts SET request_hash=? WHERE idempotency_key='legacy-create-key'",
            (legacy_hash,),
        )

    restarted = OperationsService(storage_path=database)
    replay = restarted.create_case(
        case_id=case_id,
        thread_id=case_id,
        **payload,
        **decision,
        expected_case_version=0,
        idempotency_key="legacy-create-key",
    )
    assert replay == receipt
    current_details = {**legacy_details, "thread_id": case_id}
    expected_hash = restarted._request_hash(
        case_id,
        "create_case",
        0,
        current_details,
        decision["approval"],
        decision["proposed_action"],
    )
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT request_hash FROM receipts WHERE idempotency_key='legacy-create-key'"
        ).fetchone()[0]
    assert stored == expected_hash


def make_ready(service: OperationsService, case_id: str) -> int:
    create(service, case_id)
    service.create_facility_tasks(
        case_id=case_id,
        facility_ids=["DC-SOUTH", "STORE-03"],
        **reviewed(case_id, "create_facility_tasks", 1, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=1,
        idempotency_key=f"{case_id}-tasks",
    )
    version = 2
    for facility_id in ("DC-SOUTH", "STORE-03"):
        service.record_acknowledgment(
            case_id=case_id,
            facility_id=facility_id,
            **reviewed(case_id, "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"{case_id}-ack-{facility_id}",
        )
        version += 1
    return version


def assert_begin_immediate_is_locked(database: Path) -> None:
    connection = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            connection.execute("BEGIN IMMEDIATE")
    finally:
        connection.close()


def _process_hold(database: str, key: str) -> str:
    try:
        OperationsService(storage_path=Path(database)).apply_inventory_hold(
            case_id="CASE-CAS",
            lot_ids=["LOT-EXACT-170"],
            **reviewed("CASE-CAS", "apply_inventory_hold", 1, ["LOT-EXACT-170"]),
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
                **reviewed("CASE-RACE", "close_case", 4, []),
                expected_case_version=4,
                idempotency_key="race-close",
            )
        else:
            service.create_facility_tasks(
                case_id="CASE-RACE",
                facility_ids=["DC-SOUTH"],
                **reviewed("CASE-RACE", "create_facility_tasks", 4, ["DC-SOUTH"]),
                expected_case_version=4,
                idempotency_key="race-task",
            )
        return action
    except StaleCaseVersionError:
        return "stale"


def test_sqlite_idempotency_binds_the_full_request_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    first = OperationsService(storage_path=database)
    payload = case_input()
    approved = reviewed(
        "CASE-SQL",
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    receipt = first.create_case(
        case_id="CASE-SQL",
        **payload,
        **approved,
        expected_case_version=0,
        idempotency_key="request-1",
    )
    assert (
        OperationsService(storage_path=database)
        .create_case(
            case_id="CASE-SQL",
            **payload,
            **approved,
            expected_case_version=0,
            idempotency_key="request-1",
        )
        .receipt_id
        == receipt.receipt_id
    )
    with pytest.raises(IdempotencyConflictError):
        OperationsService(storage_path=database).create_case(
            case_id="OTHER",
            **payload,
            **reviewed(
                "OTHER",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
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
    payload = case_input()
    approved = reviewed(
        "CASE-REPLAY",
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    service.create_case(
        case_id="CASE-REPLAY",
        **payload,
        **approved,
        expected_case_version=0,
        idempotency_key="create",
    )
    with pytest.raises(IdempotencyConflictError):
        service.create_case(
            case_id="CASE-REPLAY",
            **payload,
            **reviewed(
                "CASE-REPLAY",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
                actor="other",
            ),
            expected_case_version=0,
            idempotency_key="create",
        )
    service.create_facility_tasks(
        case_id="CASE-REPLAY",
        facility_ids=["DC-SOUTH", "STORE-03"],
        **reviewed("CASE-REPLAY", "create_facility_tasks", 1, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=1,
        idempotency_key="tasks",
    )
    for version, facility_id in enumerate(("DC-SOUTH", "STORE-03"), start=2):
        service.record_acknowledgment(
            case_id="CASE-REPLAY",
            facility_id=facility_id,
            **reviewed("CASE-REPLAY", "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"ack-{facility_id}",
        )
    first = service.close_case(
        case_id="CASE-REPLAY",
        **reviewed("CASE-REPLAY", "close_case", 4, []),
        expected_case_version=4,
        idempotency_key="close",
    )
    assert (
        OperationsService(storage_path=database)
        .close_case(
            case_id="CASE-REPLAY",
            **reviewed("CASE-REPLAY", "close_case", 4, []),
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
        targets = payload["confirmed_lot_ids"]
        evidence = payload["trace_event_ids"]
        with pytest.raises(ValueError, match=missing):
            service.create_case(
                case_id=f"CASE-EMPTY-{missing}",
                **payload,
                **reviewed(
                    f"CASE-EMPTY-{missing}",
                    "create_case",
                    0,
                    targets,
                    evidence_ids=evidence,
                ),
                expected_case_version=0,
                idempotency_key=f"empty-{missing}",
            )

    create(service, "CASE-FRESH")
    with pytest.raises(ClosureBlockedError, match="task|acknowledgement"):
        service.close_case(
            case_id="CASE-FRESH",
            **reviewed("CASE-FRESH", "close_case", 1, []),
            expected_case_version=1,
            idempotency_key="fresh-close",
        )


def test_realistic_case_sequence_persists_tasks_and_closes(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    service = OperationsService(storage_path=database)
    create(service, "CASE-SUCCESS")
    service.create_facility_tasks(
        case_id="CASE-SUCCESS",
        facility_ids=["DC-SOUTH", "STORE-03"],
        **reviewed("CASE-SUCCESS", "create_facility_tasks", 1, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=1,
        idempotency_key="success-tasks",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM tasks WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-SOUTH"),
        ).fetchone() == ("pending",)
        assert connection.execute(
            "SELECT acknowledged FROM acknowledgements WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-SOUTH"),
        ).fetchone() == (0,)
    for version, facility_id in enumerate(("DC-SOUTH", "STORE-03"), start=2):
        service.record_acknowledgment(
            case_id="CASE-SUCCESS",
            facility_id=facility_id,
            **reviewed("CASE-SUCCESS", "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"success-ack-{facility_id}",
        )
    receipt = service.close_case(
        case_id="CASE-SUCCESS",
        **reviewed("CASE-SUCCESS", "close_case", 4, []),
        expected_case_version=4,
        idempotency_key="success-close",
    )

    state = service.get_case("CASE-SUCCESS")
    assert receipt.case_version == 5
    assert state is not None and state.status == "closed"
    with sqlite3.connect(database) as connection:
        task = connection.execute(
            "SELECT status FROM tasks WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-SOUTH"),
        ).fetchone()
        acknowledgement = connection.execute(
            "SELECT acknowledged FROM acknowledgements WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-SOUTH"),
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
            **reviewed("CASE-NO-TASK", "record_acknowledgment", 1, ["DC-NORTH"]),
            expected_case_version=1,
            idempotency_key="no-task-ack",
        )


def test_disposition_rejects_values_outside_the_concrete_domain(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-BAD-DISPOSITION", lot_id=GAPPED_LOT)
    with pytest.raises(ValueError, match="disposition"):
        service.record_disposition(
            case_id="CASE-BAD-DISPOSITION",
            lot_id="LOT-EXACT-170",
            disposition="made_up",
            evidence_id="EV-NOPE",
            **reviewed(
                "CASE-BAD-DISPOSITION",
                "record_disposition",
                1,
                ["LOT-EXACT-170"],
                evidence_ids=["EV-NOPE"],
            ),
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
    make_ready(service, "CASE-RACE")

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
    make_ready(service, "CASE-CLOSED")
    service.close_case(
        case_id="CASE-CLOSED",
        **reviewed("CASE-CLOSED", "close_case", 4, []),
        expected_case_version=4,
        idempotency_key="closed-close",
    )
    with pytest.raises(ClosureBlockedError, match="closed"):
        service.create_facility_tasks(
            case_id="CASE-CLOSED",
            facility_ids=["DC-SOUTH"],
            **reviewed("CASE-CLOSED", "create_facility_tasks", 5, ["DC-SOUTH"]),
            expected_case_version=5,
            idempotency_key="closed-late-task",
        )


@pytest.mark.parametrize(
    ("case_id", "case_payload", "message"),
    [
        ("CASE-GAP", case_input(evidence_gaps=["missing shipment"]), "unresolved evidence"),
    ],
)
def test_close_requires_zero_gaps_and_verified_reconciliation(
    tmp_path: Path, case_id: str, case_payload: dict[str, Any], message: str
) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    service.create_case(
        case_id=case_id,
        **case_payload,
        **reviewed(
            case_id,
            "create_case",
            0,
            case_payload["confirmed_lot_ids"],
            evidence_ids=case_payload["trace_event_ids"],
        ),
        expected_case_version=0,
        idempotency_key=f"{case_id}-create",
    )
    service.create_facility_tasks(
        case_id=case_id,
        facility_ids=["DC-SOUTH", "STORE-03"],
        **reviewed(case_id, "create_facility_tasks", 1, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=1,
        idempotency_key=f"{case_id}-tasks",
    )
    for version, facility_id in enumerate(("DC-SOUTH", "STORE-03"), start=2):
        service.record_acknowledgment(
            case_id=case_id,
            facility_id=facility_id,
            **reviewed(case_id, "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"{case_id}-ack-{facility_id}",
        )
    with pytest.raises(ClosureBlockedError, match=message):
        service.close_case(
            case_id=case_id,
            **reviewed(case_id, "close_case", 4, []),
            expected_case_version=4,
            idempotency_key=f"{case_id}-close",
        )


def test_close_rejects_approval_for_an_older_case_version(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    make_ready(service, "CASE-OLD-APPROVAL")
    with pytest.raises(ApprovalRequiredError, match="current expected case version"):
        stale_review = reviewed("CASE-OLD-APPROVAL", "close_case", 3, [])
        stale_review["proposed_action"] = stale_review["proposed_action"].model_copy(
            update={"expected_case_version": 4}
        )
        service.close_case(
            case_id="CASE-OLD-APPROVAL",
            **stale_review,
            expected_case_version=4,
            idempotency_key="old-approval-close",
        )


def test_case_creation_rejects_non_authoritative_trace_evidence(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    payload = case_input()
    payload["trace_event_ids"] = ["EV-001"]
    with pytest.raises(ValueError, match="authoritative"):
        service.create_case(
            case_id="CASE-EVIDENCE-MISMATCH",
            **payload,
            **reviewed(
                "CASE-EVIDENCE-MISMATCH",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key="evidence-mismatch-create",
        )


def test_changed_approved_case_version_conflicts_with_existing_idempotency_key(
    tmp_path: Path,
) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-APPROVAL-HASH")
    service.apply_inventory_hold(
        case_id="CASE-APPROVAL-HASH",
        lot_ids=[SUCCESS_LOT],
        **reviewed("CASE-APPROVAL-HASH", "apply_inventory_hold", 1, [SUCCESS_LOT]),
        expected_case_version=1,
        idempotency_key="approval-version-key",
    )
    with pytest.raises(IdempotencyConflictError):
        changed_review = reviewed("CASE-APPROVAL-HASH", "apply_inventory_hold", 1, [SUCCESS_LOT])
        changed_review["approval"] = changed_review["approval"].model_copy(
            update={"approved_case_version": 0}
        )
        service.apply_inventory_hold(
            case_id="CASE-APPROVAL-HASH",
            lot_ids=[SUCCESS_LOT],
            **changed_review,
            expected_case_version=1,
            idempotency_key="approval-version-key",
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "bogus_lot",
        "missing_event",
        "unrelated_event",
        "bogus_inventory",
        "wrong_quantities",
        "legacy_fake_event",
        "legacy_fake_inventory",
        "wrong_facility",
    ],
)
def test_case_creation_rejects_each_non_authoritative_evidence_class(
    tmp_path: Path, tamper: str
) -> None:
    payload = case_input()
    if tamper == "bogus_lot":
        payload["confirmed_lot_ids"] = ["LOT-BOGUS"]
        payload["reconciliation"] = [
            payload["reconciliation"][0].model_copy(update={"lot_id": "LOT-BOGUS"})
        ]
    elif tamper in {"missing_event", "legacy_fake_event"}:
        payload["trace_event_ids"] = ["EV-NOT-FOUND" if tamper == "missing_event" else "EV-RECEIVE"]
    elif tamper == "unrelated_event":
        payload["trace_event_ids"] = [*payload["trace_event_ids"], "EV-001"]
    elif tamper == "wrong_facility":
        payload["required_facilities"] = ["DC-NORTH"]
    else:
        raw = payload["reconciliation"][0].model_dump(mode="json")
        if tamper == "wrong_quantities":
            raw["received"] += 1
            raw["unaccounted"] += 1
        else:
            replacement = "INV-NOT-FOUND" if tamper == "bogus_inventory" else "INV-EXACT"
            original = raw["component_evidence"]["on_hand"][0]
            raw["component_evidence"]["on_hand"] = [replacement]
            raw["component_evidence"]["unaccounted"] = [
                replacement if item == original else item
                for item in raw["component_evidence"]["unaccounted"]
            ]
            raw["evidence_ids"] = [
                replacement if item == original else item for item in raw["evidence_ids"]
            ]
        payload["reconciliation"] = [Reconciliation.model_validate(raw)]

    service = OperationsService(storage_path=tmp_path / f"{tamper}.sqlite3")
    with pytest.raises(ValueError, match="authoritative"):
        service.create_case(
            case_id=f"CASE-{tamper}",
            **payload,
            **reviewed(
                f"CASE-{tamper}",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key=f"create-{tamper}",
        )


def test_exact_lot_authoritative_fifty_unit_gap_cannot_be_closed(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "exact.sqlite3")
    create(service, "CASE-EXACT-GAP", lot_id=GAPPED_LOT)
    service.create_facility_tasks(
        case_id="CASE-EXACT-GAP",
        facility_ids=["DC-NORTH", "STORE-01", "STORE-02"],
        **reviewed(
            "CASE-EXACT-GAP",
            "create_facility_tasks",
            1,
            ["DC-NORTH", "STORE-01", "STORE-02"],
        ),
        expected_case_version=1,
        idempotency_key="exact-tasks",
    )
    version = 2
    for facility_id in ("DC-NORTH", "STORE-01", "STORE-02"):
        service.record_acknowledgment(
            case_id="CASE-EXACT-GAP",
            facility_id=facility_id,
            **reviewed("CASE-EXACT-GAP", "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"exact-ack-{facility_id}",
        )
        version += 1
    with pytest.raises(ClosureBlockedError, match="50|unaccounted"):
        service.close_case(
            case_id="CASE-EXACT-GAP",
            **reviewed("CASE-EXACT-GAP", "close_case", version, []),
            expected_case_version=version,
            idempotency_key="exact-close",
        )


def test_closure_revalidates_authority_after_caller_disposition_changes_evidence(
    tmp_path: Path,
) -> None:
    service = OperationsService(storage_path=tmp_path / "closure-authority.sqlite3")
    version = make_ready(service, "CASE-CLOSURE-AUTHORITY")
    service.record_disposition(
        case_id="CASE-CLOSURE-AUTHORITY",
        lot_id=SUCCESS_LOT,
        disposition="dispose_unaccounted",
        evidence_id="EV-RECEIVE",
        **reviewed(
            "CASE-CLOSURE-AUTHORITY",
            "record_disposition",
            version,
            [SUCCESS_LOT],
            evidence_ids=["EV-RECEIVE"],
        ),
        expected_case_version=version,
        idempotency_key="caller-disposition",
    )
    with pytest.raises(ClosureBlockedError, match="authoritative"):
        service.close_case(
            case_id="CASE-CLOSURE-AUTHORITY",
            **reviewed("CASE-CLOSURE-AUTHORITY", "close_case", version + 1, []),
            expected_case_version=version + 1,
            idempotency_key="caller-close",
        )


def test_authoritative_traceability_dependency_is_injectable(tmp_path: Path) -> None:
    dataset = deepcopy(TRACEABILITY.dataset)
    next(item for item in dataset["events"] if item["event_id"] == "EV-004")["quantity"] += 1
    next(
        item
        for item in dataset["inventory_positions"]
        if item["position_id"] == "INV-LOT-PROBABLE-160"
    )["on_hand"] += 1
    injected = TraceabilityService(dataset=dataset)
    events = injected.trace_forward(SUCCESS_LOT)
    payload = {
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": [SUCCESS_LOT],
        "trace_event_ids": [event["event_id"] for event in events],
        "required_facilities": ["DC-SOUTH", "STORE-03"],
        "reconciliation": [injected.reconcile_units(SUCCESS_LOT)],
        "evidence_gaps": [],
    }
    service = OperationsService(storage_path=tmp_path / "injected.sqlite3", traceability=injected)
    service.create_case(
        case_id="CASE-INJECTED-AUTHORITY",
        **payload,
        **reviewed(
            "CASE-INJECTED-AUTHORITY",
            "create_case",
            0,
            payload["confirmed_lot_ids"],
            evidence_ids=payload["trace_event_ids"],
        ),
        expected_case_version=0,
        idempotency_key="injected-authority",
    )
    assert service.get_case("CASE-INJECTED-AUTHORITY").reconciliation[0].received == 901

    default_service = OperationsService(storage_path=tmp_path / "default.sqlite3")
    with pytest.raises(ValueError, match="authoritative reconciliation"):
        default_service.create_case(
            case_id="CASE-DEFAULT-AUTHORITY",
            **payload,
            **reviewed(
                "CASE-DEFAULT-AUTHORITY",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key="default-authority",
        )


def test_close_wins_deterministically_while_competing_task_waits_outside_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "close-wins.sqlite3"
    seed = OperationsService(storage_path=database)
    version = make_ready(seed, "CASE-CLOSE-WINS")
    close_validated = Event()
    release_close = Event()
    task_started = Event()
    task_done = Event()
    outcomes: dict[str, str] = {}

    def barrier(action: str) -> None:
        if action == "close_case":
            close_validated.set()
            assert release_close.wait(timeout=5)

    close_service = OperationsService(storage_path=database, before_cas_hook=barrier)
    task_service = OperationsService(storage_path=database)

    def close() -> None:
        close_service.close_case(
            case_id="CASE-CLOSE-WINS",
            **reviewed("CASE-CLOSE-WINS", "close_case", version, []),
            expected_case_version=version,
            idempotency_key="close-wins-close",
        )
        outcomes["close"] = "won"

    def task() -> None:
        task_started.set()
        try:
            task_service.create_facility_tasks(
                case_id="CASE-CLOSE-WINS",
                facility_ids=["DC-SOUTH"],
                **reviewed(
                    "CASE-CLOSE-WINS",
                    "create_facility_tasks",
                    version,
                    ["DC-SOUTH"],
                ),
                expected_case_version=version,
                idempotency_key="close-wins-task",
            )
            outcomes["task"] = "won"
        except StaleCaseVersionError:
            outcomes["task"] = "stale"
        finally:
            task_done.set()

    close_thread = Thread(target=close)
    task_thread = Thread(target=task)
    close_thread.start()
    assert close_validated.wait(timeout=5)
    task_thread.start()
    assert task_started.wait(timeout=5)
    assert_begin_immediate_is_locked(database)
    assert not task_done.is_set()
    release_close.set()
    close_thread.join(timeout=5)
    task_thread.join(timeout=5)
    assert not close_thread.is_alive() and not task_thread.is_alive()
    assert outcomes == {"close": "won", "task": "stale"}
    state = seed.get_case("CASE-CLOSE-WINS")
    assert state is not None and state.status == "closed"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE case_id=? AND status='pending'",
            ("CASE-CLOSE-WINS",),
        ).fetchone() == (0,)


def test_task_wins_deterministically_before_competing_close_can_validate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "task-wins.sqlite3"
    seed = OperationsService(storage_path=database)
    version = make_ready(seed, "CASE-TASK-WINS")
    task_validated = Event()
    release_task = Event()
    close_started = Event()
    close_done = Event()
    outcomes: dict[str, str] = {}

    def barrier(action: str) -> None:
        if action == "create_facility_tasks":
            task_validated.set()
            assert release_task.wait(timeout=5)

    task_service = OperationsService(storage_path=database, before_cas_hook=barrier)
    close_service = OperationsService(storage_path=database)

    def task() -> None:
        task_service.create_facility_tasks(
            case_id="CASE-TASK-WINS",
            facility_ids=["DC-SOUTH"],
            **reviewed("CASE-TASK-WINS", "create_facility_tasks", version, ["DC-SOUTH"]),
            expected_case_version=version,
            idempotency_key="task-wins-task",
        )
        outcomes["task"] = "won"

    def close() -> None:
        close_started.set()
        try:
            close_service.close_case(
                case_id="CASE-TASK-WINS",
                **reviewed("CASE-TASK-WINS", "close_case", version, []),
                expected_case_version=version,
                idempotency_key="task-wins-close",
            )
            outcomes["close"] = "won"
        except StaleCaseVersionError:
            outcomes["close"] = "stale"
        finally:
            close_done.set()

    task_thread = Thread(target=task)
    close_thread = Thread(target=close)
    task_thread.start()
    assert task_validated.wait(timeout=5)
    close_thread.start()
    assert close_started.wait(timeout=5)
    assert_begin_immediate_is_locked(database)
    assert not close_done.is_set()
    release_task.set()
    task_thread.join(timeout=5)
    close_thread.join(timeout=5)
    assert not task_thread.is_alive() and not close_thread.is_alive()
    assert outcomes == {"task": "won", "close": "stale"}
    state = seed.get_case("CASE-TASK-WINS")
    assert state is not None and state.status == "open"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE case_id=? AND status='pending'",
            ("CASE-TASK-WINS",),
        ).fetchone() == (1,)
