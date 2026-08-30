"""Transactional SQLite-backed, approval-gated simulated operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from recallops.config import get_settings
from recallops.models import ApprovalDecision, AuditReceipt, RecallCaseState
from recallops.services.traceability import TraceabilityService


class ApprovalRequiredError(PermissionError):
    pass


class StaleCaseVersionError(ValueError):
    pass


class ClosureBlockedError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class OperationStoreError(RuntimeError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class OperationsService:
    """Every mutation is one SQLite IMMEDIATE transaction with a CAS version check."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self.storage_path = storage_path or get_settings().operations_db_path
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS cases (
                      case_id TEXT PRIMARY KEY, version INTEGER NOT NULL, state_json TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS receipts (
                      receipt_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                      case_id TEXT NOT NULL REFERENCES cases(case_id), action_type TEXT NOT NULL,
                      expected_version INTEGER NOT NULL, request_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
                      UNIQUE(case_id, action_type, idempotency_key));
                    CREATE TABLE IF NOT EXISTS acknowledgements (
                      case_id TEXT NOT NULL REFERENCES cases(case_id), facility_id TEXT NOT NULL,
                      acknowledged INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(case_id, facility_id));
                    CREATE TABLE IF NOT EXISTS tasks (
                      case_id TEXT NOT NULL REFERENCES cases(case_id), facility_id TEXT NOT NULL,
                      status TEXT NOT NULL, PRIMARY KEY(case_id, facility_id));
                """)
        except sqlite3.Error as error:
            raise OperationStoreError(f"unable to initialize operation store: {error}") from error

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.storage_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def _approval(self, approval: ApprovalDecision, key: str) -> None:
        if (
            approval.decision != "approve"
            or not approval.actor.strip()
            or not approval.justification.strip()
        ):
            raise ApprovalRequiredError("writes require approved, nonblank actor and justification")
        if not key or not key.strip():
            raise IdempotencyConflictError("idempotency key must be nonblank")

    def _request_hash(
        self,
        case_id: str,
        action: str,
        expected: int,
        details: dict[str, Any],
        approval: ApprovalDecision,
    ) -> str:
        return hashlib.sha256(
            _canonical(
                {
                    "case_id": case_id,
                    "action": action,
                    "expected": expected,
                    "details": details,
                    "approval": approval.model_dump(mode="json"),
                }
            ).encode()
        ).hexdigest()

    def get_case(self, case_id: str) -> RecallCaseState | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT state_json FROM cases WHERE case_id=?", (case_id,)
            ).fetchone()
        return RecallCaseState.model_validate_json(row["state_json"]) if row else None

    @property
    def cases(self) -> dict[str, RecallCaseState]:
        with self._connection() as conn:
            rows = conn.execute("SELECT case_id, state_json FROM cases").fetchall()
        return {
            row["case_id"]: RecallCaseState.model_validate_json(row["state_json"]) for row in rows
        }

    def _replay_or_conflict(
        self, conn: sqlite3.Connection, key: str, request_hash: str
    ) -> AuditReceipt | None:
        row = conn.execute(
            "SELECT request_hash, receipt_json FROM receipts WHERE idempotency_key=?", (key,)
        ).fetchone()
        if not row:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflictError("idempotency key is bound to a different request")
        return AuditReceipt.model_validate_json(row["receipt_json"])

    def _mutate(
        self,
        *,
        case_id: str,
        action: str,
        approval: ApprovalDecision,
        expected: int,
        key: str,
        details: dict[str, Any],
        transform: Any | None = None,
        validator: Any | None = None,
    ) -> AuditReceipt:
        self._approval(approval, key)
        request_hash = self._request_hash(case_id, action, expected, details, approval)
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                replay = self._replay_or_conflict(conn, key, request_hash)
                if replay:
                    conn.execute("COMMIT")
                    return replay
                row = conn.execute(
                    "SELECT version, state_json FROM cases WHERE case_id=?", (case_id,)
                ).fetchone()
                if not row:
                    raise KeyError(f"unknown case {case_id}")
                if row["version"] != expected:
                    raise StaleCaseVersionError(
                        f"expected version {expected}, current version is {row['version']}"
                    )
                state = RecallCaseState.model_validate_json(row["state_json"])
                if validator:
                    validator(conn, state)
                if transform:
                    state = transform(state)
                receipt = AuditReceipt(
                    receipt_id=str(uuid5(NAMESPACE_URL, f"{case_id}:{action}:{key}")),
                    case_id=case_id,
                    action_type=action,
                    actor=approval.actor,
                    justification=approval.justification,
                    idempotency_key=key,
                    case_version=expected + 1,
                    status="simulated",
                    details=details,
                )
                next_state = state.model_copy(
                    update={
                        "case_version": expected + 1,
                        "write_receipts": [*state.write_receipts, receipt],
                    }
                )
                updated = conn.execute(
                    "UPDATE cases SET version=?, state_json=? WHERE case_id=? AND version=?",
                    (expected + 1, next_state.model_dump_json(), case_id, expected),
                )
                if updated.rowcount != 1:
                    raise StaleCaseVersionError("compare-and-swap lost concurrent update")
                conn.execute(
                    "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt.receipt_id,
                        key,
                        case_id,
                        action,
                        expected,
                        request_hash,
                        receipt.model_dump_json(),
                    ),
                )
                if action == "create_facility_tasks":
                    for facility_id in details["facility_ids"]:
                        conn.execute(
                            "INSERT OR REPLACE INTO tasks VALUES (?, ?, ?)",
                            (case_id, facility_id, "pending"),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO acknowledgements VALUES (?, ?, ?)",
                            (case_id, facility_id, 0),
                        )
                if action == "record_acknowledgment":
                    conn.execute(
                        "INSERT OR REPLACE INTO acknowledgements VALUES (?, ?, ?)",
                        (case_id, details["facility_id"], 1),
                    )
                conn.execute("COMMIT")
                return receipt
        except (ApprovalRequiredError, StaleCaseVersionError, IdempotencyConflictError, KeyError):
            raise
        except sqlite3.Error as error:
            raise OperationStoreError(f"operation transaction failed: {error}") from error

    def create_case(
        self,
        *,
        case_id: str,
        recall_number: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
        question: str = "",
    ) -> AuditReceipt:
        self._approval(approval, idempotency_key)
        details = {"recall_number": recall_number, "question": question}
        request_hash = self._request_hash(
            case_id, "create_case", expected_case_version, details, approval
        )
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                replay = self._replay_or_conflict(conn, idempotency_key, request_hash)
                if replay:
                    conn.execute("COMMIT")
                    return replay
                if expected_case_version != 0:
                    raise StaleCaseVersionError("new cases require expected version 0")
                traceability = TraceabilityService()
                reconciliation = [traceability.reconcile_units("LOT-EXACT-170")]
                state = RecallCaseState(
                    case_id=case_id,
                    thread_id=case_id,
                    recall_number=recall_number,
                    question=question,
                    reconciliation=reconciliation,
                    acknowledgements={"DC-NORTH": False},
                )
                receipt = AuditReceipt(
                    receipt_id=str(
                        uuid5(NAMESPACE_URL, f"{case_id}:create_case:{idempotency_key}")
                    ),
                    case_id=case_id,
                    action_type="create_case",
                    actor=approval.actor,
                    justification=approval.justification,
                    idempotency_key=idempotency_key,
                    case_version=1,
                    status="simulated",
                    details=details,
                )
                state = state.model_copy(update={"case_version": 1, "write_receipts": [receipt]})
                conn.execute(
                    "INSERT INTO cases VALUES (?, ?, ?)", (case_id, 1, state.model_dump_json())
                )
                conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", (case_id, "DC-NORTH", "pending"))
                conn.execute(
                    "INSERT INTO acknowledgements VALUES (?, ?, ?)", (case_id, "DC-NORTH", 0)
                )
                conn.execute(
                    "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt.receipt_id,
                        idempotency_key,
                        case_id,
                        "create_case",
                        0,
                        request_hash,
                        receipt.model_dump_json(),
                    ),
                )
                conn.execute("COMMIT")
                return receipt
        except (ApprovalRequiredError, StaleCaseVersionError, IdempotencyConflictError):
            raise
        except sqlite3.IntegrityError as error:
            raise OperationStoreError(f"case creation failed: {error}") from error
        except sqlite3.Error as error:
            raise OperationStoreError(f"operation transaction failed: {error}") from error

    def apply_inventory_hold(
        self,
        *,
        case_id: str,
        lot_ids: list[str],
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        return self._mutate(
            case_id=case_id,
            action="apply_inventory_hold",
            approval=approval,
            expected=expected_case_version,
            key=idempotency_key,
            details={"lot_ids": lot_ids},
        )

    def create_facility_tasks(
        self,
        *,
        case_id: str,
        facility_ids: list[str],
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        def transform(state: RecallCaseState) -> RecallCaseState:
            return state.model_copy(
                update={
                    "acknowledgements": {
                        **state.acknowledgements,
                        **{facility: False for facility in facility_ids},
                    }
                }
            )

        return self._mutate(
            case_id=case_id,
            action="create_facility_tasks",
            approval=approval,
            expected=expected_case_version,
            key=idempotency_key,
            details={"facility_ids": facility_ids},
            transform=transform,
        )

    def record_acknowledgment(
        self,
        *,
        case_id: str,
        facility_id: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        def validator(conn: sqlite3.Connection, _: RecallCaseState) -> None:
            if not conn.execute(
                "SELECT 1 FROM tasks WHERE case_id=? AND facility_id=?", (case_id, facility_id)
            ).fetchone():
                raise ClosureBlockedError("acknowledgment requires an existing facility task")

        return self._mutate(
            case_id=case_id,
            action="record_acknowledgment",
            approval=approval,
            expected=expected_case_version,
            key=idempotency_key,
            details={"facility_id": facility_id},
            transform=lambda s: s.model_copy(
                update={"acknowledgements": {**s.acknowledgements, facility_id: True}}
            ),
            validator=validator,
        )

    def record_disposition(
        self,
        *,
        case_id: str,
        lot_id: str,
        disposition: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        def transform(state: RecallCaseState) -> RecallCaseState:
            reconciliations = []
            for item in state.reconciliation:
                if item.lot_id == lot_id and disposition == "dispose_unaccounted":
                    reconciliations.append(
                        item.model_copy(
                            update={"disposed": item.disposed + item.unaccounted, "unaccounted": 0}
                        )
                    )
                else:
                    reconciliations.append(item)
            return state.model_copy(update={"reconciliation": reconciliations})

        return self._mutate(
            case_id=case_id,
            action="record_disposition",
            approval=approval,
            expected=expected_case_version,
            key=idempotency_key,
            details={"lot_id": lot_id, "disposition": disposition},
            transform=transform,
        )

    def close_case(
        self,
        *,
        case_id: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        def validator(conn: sqlite3.Connection, state: RecallCaseState) -> None:
            if not state.reconciliation or any(
                item.unaccounted != 0 for item in state.reconciliation
            ):
                raise ClosureBlockedError(
                    "closure blocked: unaccounted reconciliation units remain"
                )
            if state.evidence_gaps or any(
                item.get("classification") == "ambiguous" for item in state.candidate_lots
            ):
                raise ClosureBlockedError("closure blocked: unresolved evidence remains")
            tasks = conn.execute(
                "SELECT facility_id FROM tasks WHERE case_id=?", (case_id,)
            ).fetchall()
            if not tasks or any(
                not conn.execute(
                    "SELECT acknowledged FROM acknowledgements WHERE case_id=? AND facility_id=?",
                    (case_id, row["facility_id"]),
                ).fetchone()["acknowledged"]
                for row in tasks
            ):
                raise ClosureBlockedError("closure blocked: facility acknowledgement remains")

        return self._mutate(
            case_id=case_id,
            action="close_case",
            approval=approval,
            expected=expected_case_version,
            key=idempotency_key,
            details={},
            transform=lambda s: s.model_copy(update={"status": "closed"}),
            validator=validator,
        )
