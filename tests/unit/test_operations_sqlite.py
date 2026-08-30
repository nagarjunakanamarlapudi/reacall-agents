from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recallops.models import ApprovalDecision
from recallops.services.operations import (
    IdempotencyConflictError,
    OperationsService,
    StaleCaseVersionError,
)


def approval() -> ApprovalDecision:
    return ApprovalDecision(
        decision="approve",
        actor="reviewer",
        justification="evidence",
        approved_at=datetime.now(UTC),
    )


def test_sqlite_idempotency_binds_the_full_request_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    first = OperationsService(storage_path=database)
    approved = approval()
    receipt = first.create_case(
        case_id="CASE-SQL",
        recall_number="H-1230-2026",
        approval=approved,
        expected_case_version=0,
        idempotency_key="request-1",
    )
    assert (
        OperationsService(storage_path=database)
        .create_case(
            case_id="CASE-SQL",
            recall_number="H-1230-2026",
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
            recall_number="H-1230-2026",
            approval=approval(),
            expected_case_version=0,
            idempotency_key="request-1",
        )


def test_sqlite_compare_and_swap_allows_only_one_concurrent_expected_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    OperationsService(storage_path=database).create_case(
        case_id="CASE-CAS",
        recall_number="H-1230-2026",
        approval=approval(),
        expected_case_version=0,
        idempotency_key="create",
    )

    def hold(key: str) -> str:
        try:
            OperationsService(storage_path=database).apply_inventory_hold(
                case_id="CASE-CAS",
                lot_ids=["LOT-EXACT-170"],
                approval=approval(),
                expected_case_version=1,
                idempotency_key=key,
            )
            return "won"
        except StaleCaseVersionError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = set(pool.map(hold, ["hold-a", "hold-b"]))
    assert outcomes == {"won", "stale"}


def test_idempotency_binds_approval_identity_and_close_replays(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    service = OperationsService(storage_path=database)
    approved = approval()
    service.create_case(
        case_id="CASE-REPLAY",
        recall_number="H-1230-2026",
        approval=approved,
        expected_case_version=0,
        idempotency_key="create",
    )
    with pytest.raises(IdempotencyConflictError):
        service.create_case(
            case_id="CASE-REPLAY",
            recall_number="H-1230-2026",
            approval=ApprovalDecision(
                decision="approve",
                actor="other",
                justification="evidence",
                approved_at=datetime.now(UTC),
            ),
            expected_case_version=0,
            idempotency_key="create",
        )
    service.record_disposition(
        case_id="CASE-REPLAY",
        lot_id="LOT-EXACT-170",
        disposition="dispose_unaccounted",
        approval=approved,
        expected_case_version=1,
        idempotency_key="dispose",
    )
    service.record_acknowledgment(
        case_id="CASE-REPLAY",
        facility_id="DC-NORTH",
        approval=approved,
        expected_case_version=2,
        idempotency_key="ack",
    )
    first = service.close_case(
        case_id="CASE-REPLAY", approval=approved, expected_case_version=3, idempotency_key="close"
    )
    assert (
        OperationsService(storage_path=database)
        .close_case(
            case_id="CASE-REPLAY",
            approval=approved,
            expected_case_version=3,
            idempotency_key="close",
        )
        .receipt_id
        == first.receipt_id
    )
