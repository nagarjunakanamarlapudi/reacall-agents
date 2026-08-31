"""Transactional SQLite-backed, approval-gated simulated operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from recallops.config import get_settings
from recallops.models import (
    ApprovalDecision,
    AuditReceipt,
    Disposition,
    ProposedAction,
    RecallCaseState,
    Reconciliation,
    proposed_action_digest,
    validate_case_version,
)
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

    def __init__(
        self,
        storage_path: Path | None = None,
        failure_injector: Callable[[str], None] | None = None,
        before_cas_hook: Callable[[str], None] | None = None,
        traceability: TraceabilityService | None = None,
    ) -> None:
        settings = get_settings()
        self.storage_path = storage_path or settings.operations_db_path
        self.source_mode = settings.source_mode
        self._failure_injector = failure_injector
        # Test-only synchronization seam; production callers leave this unset.
        # It runs after mutation invariants while BEGIN IMMEDIATE is still active.
        self._before_cas_hook = before_cas_hook
        self.traceability = traceability or TraceabilityService(
            data_dir=settings.data_dir,
            source_mode=settings.source_mode,
        )
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS cases (
                      case_id TEXT PRIMARY KEY, version INTEGER NOT NULL, state_json TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS case_threads (
                      case_id TEXT PRIMARY KEY REFERENCES cases(case_id),
                      thread_id TEXT NOT NULL UNIQUE);
                    CREATE TABLE IF NOT EXISTS workflow_identities (
                      case_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE);
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
                self._migrate_case_threads(connection)
        except (OSError, sqlite3.Error) as error:
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

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _migrate_case_threads(self, connection: sqlite3.Connection) -> None:
        """Backfill and validate the durable one-to-one identity mapping atomically."""
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT case_id, state_json FROM cases").fetchall()
            for row in rows:
                state = RecallCaseState.model_validate_json(row["state_json"])
                if state.case_id != row["case_id"]:
                    raise ValueError("case identity disagrees with persisted state")
                by_case = connection.execute(
                    "SELECT thread_id FROM case_threads WHERE case_id=?",
                    (state.case_id,),
                ).fetchone()
                by_thread = connection.execute(
                    "SELECT case_id FROM case_threads WHERE thread_id=?",
                    (state.thread_id,),
                ).fetchone()
                if by_case and by_case["thread_id"] != state.thread_id:
                    raise ValueError("case identity is already bound to another thread")
                if by_thread and by_thread["case_id"] != state.case_id:
                    raise ValueError("thread identity is already bound to another case")
                workflow_by_case = connection.execute(
                    "SELECT thread_id FROM workflow_identities WHERE case_id=?",
                    (state.case_id,),
                ).fetchone()
                workflow_by_thread = connection.execute(
                    "SELECT case_id FROM workflow_identities WHERE thread_id=?",
                    (state.thread_id,),
                ).fetchone()
                if workflow_by_case and workflow_by_case["thread_id"] != state.thread_id:
                    raise ValueError("workflow case identity is already bound to another thread")
                if workflow_by_thread and workflow_by_thread["case_id"] != state.case_id:
                    raise ValueError("workflow thread identity is already bound to another case")
                if not by_case:
                    connection.execute(
                        "INSERT INTO case_threads VALUES (?, ?)",
                        (state.case_id, state.thread_id),
                    )
                if not workflow_by_case:
                    connection.execute(
                        "INSERT INTO workflow_identities VALUES (?, ?)",
                        (state.case_id, state.thread_id),
                    )
            connection.execute("COMMIT")
        except (sqlite3.Error, ValueError) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise OperationStoreError(
                f"unable to migrate case/thread identity mapping: {error}"
            ) from error

    def _inject_failure(self, stage: str) -> None:
        if self._failure_injector:
            self._failure_injector(stage)

    @staticmethod
    def _validate_idempotency_key(key: str) -> None:
        if not key or not key.strip():
            raise IdempotencyConflictError("idempotency key must be nonblank")

    def _approval(
        self,
        approval: ApprovalDecision,
        expected: int,
        *,
        case_id: str,
        action_type: str,
        proposed_action: ProposedAction,
        target_ids: list[str],
        evidence_ids: list[str] | None = None,
    ) -> None:
        validate_case_version(expected, "expected_case_version")
        validate_case_version(
            approval.approved_case_version,
            "approved_case_version",
        )
        validate_case_version(
            proposed_action.expected_case_version,
            "proposed_action.expected_case_version",
        )
        try:
            proposed_action = ProposedAction.model_validate(
                proposed_action.model_dump(mode="python")
            )
        except ValidationError as error:
            raise ApprovalRequiredError("reviewed action contract is invalid") from error
        if (
            approval.decision != "approve"
            or not approval.actor.strip()
            or not approval.justification.strip()
        ):
            raise ApprovalRequiredError("writes require approved, nonblank actor and justification")
        if approval.approved_case_version != expected:
            raise ApprovalRequiredError(
                "approval must be bound to the current expected case version"
            )
        if approval.approved_case_id != case_id or proposed_action.case_id != case_id:
            raise ApprovalRequiredError("approval binding must match the operation case ID")
        if proposed_action.expected_case_version != expected:
            raise ApprovalRequiredError(
                "reviewed action must match the current expected case version"
            )
        if proposed_action.action_type != action_type:
            raise ApprovalRequiredError("reviewed action type does not match the operation")
        if tuple(target_ids) != proposed_action.target_ids:
            raise ApprovalRequiredError("reviewed action targets do not match the operation")
        if evidence_ids is not None and set(evidence_ids) != set(proposed_action.evidence_ids):
            raise ApprovalRequiredError("reviewed action evidence does not match the operation")
        binding = next(
            (
                item
                for item in approval.action_bindings
                if item.action_id == proposed_action.action_id
            ),
            None,
        )
        if binding is None or proposed_action.action_id not in approval.action_ids:
            raise ApprovalRequiredError("reviewed action is outside the approval binding scope")
        if binding.action_digest != proposed_action_digest(proposed_action):
            raise ApprovalRequiredError("approval binding does not match the reviewed action")

    def _validate_authoritative_evidence(
        self,
        *,
        confirmed_lot_ids: list[str],
        trace_event_ids: list[str],
        required_facilities: list[str],
        reconciliation: list[Reconciliation],
    ) -> None:
        known_lot_ids = {item["lot_id"] for item in self.traceability.dataset["lots"]}
        unknown_lot_ids = set(confirmed_lot_ids) - known_lot_ids
        if unknown_lot_ids:
            raise ValueError(
                f"authoritative evidence has no such lot(s): {sorted(unknown_lot_ids)}"
            )

        authoritative_event_ids: set[str] = set()
        authoritative_facilities: set[str] = set()
        authoritative_reconciliation: dict[str, Reconciliation] = {}
        for lot_id in confirmed_lot_ids:
            events = self.traceability.trace_forward(lot_id)
            authoritative_event_ids.update(event["event_id"] for event in events)
            authoritative_facilities.update(
                facility
                for event in events
                for facility in (event.get("from_facility"), event.get("to_facility"))
                if facility
            )
            authoritative_reconciliation[lot_id] = self.traceability.reconcile_units(lot_id)

        supplied_event_ids = set(trace_event_ids)
        if supplied_event_ids != authoritative_event_ids:
            missing = sorted(authoritative_event_ids - supplied_event_ids)
            unrelated = sorted(supplied_event_ids - authoritative_event_ids)
            raise ValueError(
                f"authoritative trace evidence mismatch (missing={missing}, unrelated={unrelated})"
            )
        if set(required_facilities) != authoritative_facilities:
            raise ValueError(
                "authoritative required facilities mismatch "
                f"(expected={sorted(authoritative_facilities)})"
            )

        supplied_reconciliation = {item.lot_id: item for item in reconciliation}
        for lot_id, authoritative in authoritative_reconciliation.items():
            supplied = supplied_reconciliation.get(lot_id)
            if supplied != authoritative:
                raise ValueError(
                    "authoritative reconciliation mismatch for "
                    f"{lot_id}: quantities and component evidence must match the dataset"
                )

    def _request_hash(
        self,
        case_id: str,
        action: str,
        expected: int,
        details: dict[str, Any],
        approval: ApprovalDecision,
        proposed_action: ProposedAction,
    ) -> str:
        return hashlib.sha256(
            _canonical(
                {
                    "case_id": case_id,
                    "action": action,
                    "expected": expected,
                    "details": details,
                    "approval": approval.model_dump(mode="json"),
                    "proposed_action": proposed_action.model_dump(mode="json"),
                }
            ).encode()
        ).hexdigest()

    def get_case(self, case_id: str) -> RecallCaseState | None:
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT state_json FROM cases WHERE case_id=?", (case_id,)
                ).fetchone()
            return RecallCaseState.model_validate_json(row["state_json"]) if row else None
        except (OSError, sqlite3.Error, ValueError) as error:
            raise OperationStoreError(f"unable to read operation store: {error}") from error

    def get_receipt(self, idempotency_key: str) -> AuditReceipt | None:
        """Return the authoritative receipt bound to an idempotency key, if any."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a nonblank string")
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT receipt_json FROM receipts WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
            return AuditReceipt.model_validate_json(row["receipt_json"]) if row else None
        except (OSError, sqlite3.Error, ValueError) as error:
            raise OperationStoreError(f"unable to read operation store: {error}") from error

    def get_thread_for_case(self, case_id: str) -> str | None:
        if type(case_id) is not str or not case_id.strip():
            raise ValueError("case_id must be a nonblank exact string")
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT thread_id FROM workflow_identities WHERE case_id=?", (case_id,)
                ).fetchone()
            return str(row["thread_id"]) if row else None
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to read operation store: {error}") from error

    def get_case_id_for_thread(self, thread_id: str) -> str | None:
        if type(thread_id) is not str or not thread_id.strip():
            raise ValueError("thread_id must be a nonblank exact string")
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT case_id FROM workflow_identities WHERE thread_id=?", (thread_id,)
                ).fetchone()
            return str(row["case_id"]) if row else None
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to read operation store: {error}") from error

    def reserve_workflow_identity(self, case_id: str, thread_id: str) -> bool:
        """Atomically reserve the public case/thread identity before graph initialization."""
        if type(case_id) is not str or not case_id.strip():
            raise ValueError("case_id must be a nonblank exact string")
        if type(thread_id) is not str or not thread_id.strip():
            raise ValueError("thread_id must be a nonblank exact string")
        try:
            with self._transaction() as conn:
                by_case = conn.execute(
                    "SELECT thread_id FROM workflow_identities WHERE case_id=?", (case_id,)
                ).fetchone()
                by_thread = conn.execute(
                    "SELECT case_id FROM workflow_identities WHERE thread_id=?", (thread_id,)
                ).fetchone()
                if by_case and by_case["thread_id"] != thread_id:
                    raise ValueError(
                        f"case_id {case_id!r} is already bound to thread {by_case['thread_id']!r}"
                    )
                if by_thread and by_thread["case_id"] != case_id:
                    raise ValueError(
                        f"thread_id {thread_id!r} is already bound to case {by_thread['case_id']!r}"
                    )
                if by_case:
                    return False
                conn.execute("INSERT INTO workflow_identities VALUES (?, ?)", (case_id, thread_id))
                return True
        except ValueError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to reserve workflow identity: {error}") from error

    def release_workflow_identity(self, case_id: str, thread_id: str) -> None:
        """Release only an unmaterialized exact reservation after failed initialization."""
        try:
            with self._transaction() as conn:
                if conn.execute("SELECT 1 FROM cases WHERE case_id=?", (case_id,)).fetchone():
                    return
                conn.execute(
                    "DELETE FROM workflow_identities WHERE case_id=? AND thread_id=?",
                    (case_id, thread_id),
                )
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to release workflow identity: {error}") from error

    def get_case_for_thread(self, thread_id: str) -> RecallCaseState | None:
        if type(thread_id) is not str or not thread_id.strip():
            raise ValueError("thread_id must be a nonblank exact string")
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT cases.state_json
                    FROM case_threads
                    JOIN cases USING (case_id)
                    WHERE case_threads.thread_id=?
                    """,
                    (thread_id,),
                ).fetchone()
            return RecallCaseState.model_validate_json(row["state_json"]) if row else None
        except (OSError, sqlite3.Error, ValueError) as error:
            raise OperationStoreError(f"unable to read operation store: {error}") from error

    @property
    def cases(self) -> dict[str, RecallCaseState]:
        try:
            with self._connection() as conn:
                rows = conn.execute("SELECT case_id, state_json FROM cases").fetchall()
            return {
                row["case_id"]: RecallCaseState.model_validate_json(row["state_json"])
                for row in rows
            }
        except (OSError, sqlite3.Error, ValueError) as error:
            raise OperationStoreError(f"unable to read operation store: {error}") from error

    def _replay_or_conflict(
        self,
        conn: sqlite3.Connection,
        key: str,
        request_hash: str,
        *,
        legacy_request_hashes: tuple[str, ...] = (),
    ) -> AuditReceipt | None:
        row = conn.execute(
            "SELECT request_hash, receipt_json FROM receipts WHERE idempotency_key=?", (key,)
        ).fetchone()
        if not row:
            return None
        if row["request_hash"] != request_hash:
            if row["request_hash"] not in legacy_request_hashes:
                raise IdempotencyConflictError("idempotency key is bound to a different request")
            conn.execute(
                "UPDATE receipts SET request_hash=? WHERE idempotency_key=?",
                (request_hash, key),
            )
        return AuditReceipt.model_validate_json(row["receipt_json"])

    def _mutate(
        self,
        *,
        case_id: str,
        action: str,
        approval: ApprovalDecision,
        proposed_action: ProposedAction,
        expected: int,
        key: str,
        details: dict[str, Any],
        target_ids: list[str],
        evidence_ids: list[str] | None = None,
        transform: Any | None = None,
        validator: Any | None = None,
    ) -> AuditReceipt:
        validate_case_version(expected, "expected_case_version")
        self._validate_idempotency_key(key)
        request_hash = self._request_hash(
            case_id, action, expected, details, approval, proposed_action
        )
        try:
            with self._transaction() as conn:
                replay = self._replay_or_conflict(conn, key, request_hash)
                if replay:
                    return replay
                self._approval(
                    approval,
                    expected,
                    case_id=case_id,
                    action_type=action,
                    proposed_action=proposed_action,
                    target_ids=target_ids,
                    evidence_ids=evidence_ids,
                )
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
                if state.status == "closed":
                    raise ClosureBlockedError("operation blocked: case is already closed")
                if validator:
                    validator(conn, state)
                if self._before_cas_hook:
                    self._before_cas_hook(action)
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
                    details={
                        **details,
                        "reviewed_action": proposed_action.model_dump(mode="json"),
                    },
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
                self._inject_failure("after_receipt_insert")
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
                    conn.execute(
                        "UPDATE tasks SET status='acknowledged' WHERE case_id=? AND facility_id=?",
                        (case_id, details["facility_id"]),
                    )
                return receipt
        except (
            ApprovalRequiredError,
            ClosureBlockedError,
            IdempotencyConflictError,
            KeyError,
            StaleCaseVersionError,
        ):
            raise
        except (OSError, sqlite3.Error, ValueError) as error:
            raise OperationStoreError(f"operation transaction failed: {error}") from error

    def create_case(
        self,
        *,
        case_id: str,
        recall_number: str,
        confirmed_lot_ids: list[str],
        trace_event_ids: list[str],
        required_facilities: list[str],
        reconciliation: list[Reconciliation | dict[str, Any]],
        evidence_gaps: list[str],
        proposed_action: ProposedAction,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
        question: str = "",
        thread_id: str | None = None,
    ) -> AuditReceipt:
        validate_case_version(expected_case_version, "expected_case_version")
        self._validate_idempotency_key(idempotency_key)
        durable_thread_id = case_id if thread_id is None else thread_id
        if type(durable_thread_id) is not str or not durable_thread_id.strip():
            raise ValueError("thread_id must be a nonblank string")
        required_nonempty = {
            "confirmed_lot_ids": confirmed_lot_ids,
            "trace_event_ids": trace_event_ids,
            "required_facilities": required_facilities,
            "reconciliation": reconciliation,
        }
        for field_name, values in required_nonempty.items():
            if not values:
                raise ValueError(f"{field_name} must be nonempty")
        for field_name, values in (
            ("confirmed_lot_ids", confirmed_lot_ids),
            ("trace_event_ids", trace_event_ids),
            ("required_facilities", required_facilities),
        ):
            if any(not value.strip() for value in values) or len(values) != len(set(values)):
                raise ValueError(f"{field_name} must contain unique nonblank identifiers")
        reconciliations = [Reconciliation.model_validate(item) for item in reconciliation]
        if len(reconciliations) != len(confirmed_lot_ids) or {
            item.lot_id for item in reconciliations
        } != set(confirmed_lot_ids):
            raise ValueError("reconciliation must cover exactly the confirmed_lot_ids")
        details = {
            "recall_number": recall_number,
            "question": question,
            "thread_id": durable_thread_id,
            "confirmed_lot_ids": confirmed_lot_ids,
            "trace_event_ids": trace_event_ids,
            "required_facilities": required_facilities,
            "reconciliation": [item.model_dump(mode="json") for item in reconciliations],
            "evidence_gaps": evidence_gaps,
        }
        request_hash = self._request_hash(
            case_id,
            "create_case",
            expected_case_version,
            details,
            approval,
            proposed_action,
        )
        legacy_request_hash = self._request_hash(
            case_id,
            "create_case",
            expected_case_version,
            {key: value for key, value in details.items() if key != "thread_id"},
            approval,
            proposed_action,
        )
        try:
            with self._transaction() as conn:
                by_case = conn.execute(
                    "SELECT thread_id FROM workflow_identities WHERE case_id=?", (case_id,)
                ).fetchone()
                by_thread = conn.execute(
                    "SELECT case_id FROM workflow_identities WHERE thread_id=?",
                    (durable_thread_id,),
                ).fetchone()
                if by_case and by_case["thread_id"] != durable_thread_id:
                    raise IdempotencyConflictError("case_id is already bound to another thread")
                if by_thread and by_thread["case_id"] != case_id:
                    raise IdempotencyConflictError("thread_id is already bound to another case")
                if not by_case:
                    conn.execute(
                        "INSERT INTO workflow_identities VALUES (?, ?)",
                        (case_id, durable_thread_id),
                    )
                replay = self._replay_or_conflict(
                    conn,
                    idempotency_key,
                    request_hash,
                    legacy_request_hashes=(legacy_request_hash,),
                )
                if replay:
                    return replay
                self._approval(
                    approval,
                    expected_case_version,
                    case_id=case_id,
                    action_type="create_case",
                    proposed_action=proposed_action,
                    target_ids=confirmed_lot_ids,
                    evidence_ids=trace_event_ids,
                )
                if expected_case_version != 0:
                    raise StaleCaseVersionError("new cases require expected version 0")
                self._validate_authoritative_evidence(
                    confirmed_lot_ids=confirmed_lot_ids,
                    trace_event_ids=trace_event_ids,
                    required_facilities=required_facilities,
                    reconciliation=reconciliations,
                )
                state = RecallCaseState(
                    case_id=case_id,
                    thread_id=durable_thread_id,
                    recall_number=recall_number,
                    question=question,
                    source_mode=self.source_mode,
                    confirmed_lot_ids=confirmed_lot_ids,
                    trace_event_ids=trace_event_ids,
                    required_facilities=required_facilities,
                    reconciliation=reconciliations,
                    evidence_gaps=evidence_gaps,
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
                    details={
                        **details,
                        "reviewed_action": proposed_action.model_dump(mode="json"),
                    },
                )
                state = state.model_copy(update={"case_version": 1, "write_receipts": [receipt]})
                conn.execute(
                    "INSERT INTO cases VALUES (?, ?, ?)", (case_id, 1, state.model_dump_json())
                )
                conn.execute(
                    "INSERT INTO case_threads VALUES (?, ?)",
                    (case_id, durable_thread_id),
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
                self._inject_failure("after_receipt_insert")
                return receipt
        except (
            ApprovalRequiredError,
            IdempotencyConflictError,
            StaleCaseVersionError,
            ValueError,
        ):
            raise
        except (OSError, sqlite3.IntegrityError) as error:
            raise OperationStoreError(f"case creation failed: {error}") from error
        except (sqlite3.Error, ValueError) as error:
            raise OperationStoreError(f"operation transaction failed: {error}") from error

    def apply_inventory_hold(
        self,
        *,
        case_id: str,
        lot_ids: list[str],
        proposed_action: ProposedAction,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        return self._mutate(
            case_id=case_id,
            action="apply_inventory_hold",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            details={"lot_ids": lot_ids},
            target_ids=lot_ids,
        )

    def create_facility_tasks(
        self,
        *,
        case_id: str,
        facility_ids: list[str],
        proposed_action: ProposedAction,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        if not facility_ids or any(not item.strip() for item in facility_ids):
            raise ValueError("facility_ids must be nonempty and nonblank")
        if len(facility_ids) != len(set(facility_ids)):
            raise ValueError("facility_ids must be unique")

        def transform(state: RecallCaseState) -> RecallCaseState:
            return state.model_copy(
                update={
                    "acknowledgements": {
                        **state.acknowledgements,
                        **{facility: False for facility in facility_ids},
                    },
                }
            )

        def validator(_: sqlite3.Connection, state: RecallCaseState) -> None:
            unrelated = set(facility_ids) - set(state.required_facilities)
            if unrelated:
                raise ClosureBlockedError(
                    "facility tasks must target authoritative required facilities: "
                    f"{sorted(unrelated)}"
                )

        return self._mutate(
            case_id=case_id,
            action="create_facility_tasks",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            details={"facility_ids": facility_ids},
            target_ids=facility_ids,
            transform=transform,
            validator=validator,
        )

    def record_acknowledgment(
        self,
        *,
        case_id: str,
        facility_id: str,
        proposed_action: ProposedAction,
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
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            details={"facility_id": facility_id},
            target_ids=[facility_id],
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
        disposition: Disposition,
        evidence_id: str,
        proposed_action: ProposedAction,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        if disposition not in {"dispose_unaccounted", "quarantined", "returned"}:
            raise ValueError("invalid disposition")
        if not evidence_id.strip():
            raise ValueError("evidence_id must be nonblank")

        def transform(state: RecallCaseState) -> RecallCaseState:
            reconciliations = []
            for item in state.reconciliation:
                if item.lot_id == lot_id and disposition == "dispose_unaccounted":
                    component_evidence = {
                        **item.component_evidence,
                        "disposed": [
                            *item.component_evidence.get("disposed", []),
                            evidence_id,
                        ],
                    }
                    evidence_ids = list(dict.fromkeys([*item.evidence_ids, evidence_id]))
                    component_evidence["unaccounted"] = evidence_ids
                    reconciliations.append(
                        Reconciliation.model_validate(
                            {
                                **item.model_dump(),
                                "disposed": item.disposed + item.unaccounted,
                                "unaccounted": 0,
                                "evidence_ids": evidence_ids,
                                "component_evidence": component_evidence,
                                "verified": True,
                            }
                        )
                    )
                else:
                    reconciliations.append(item)
            if not any(item.lot_id == lot_id for item in state.reconciliation):
                raise ClosureBlockedError(f"unknown reconciled lot {lot_id}")
            return state.model_copy(
                update={
                    "reconciliation": reconciliations,
                    "trace_event_ids": list(dict.fromkeys([*state.trace_event_ids, evidence_id])),
                    "evidence_gaps": [gap for gap in state.evidence_gaps if lot_id not in gap],
                }
            )

        return self._mutate(
            case_id=case_id,
            action="record_disposition",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            details={"lot_id": lot_id, "disposition": disposition, "evidence_id": evidence_id},
            target_ids=[lot_id],
            evidence_ids=[evidence_id],
            transform=transform,
        )

    def close_case(
        self,
        *,
        case_id: str,
        proposed_action: ProposedAction,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        def validator(conn: sqlite3.Connection, state: RecallCaseState) -> None:
            if not state.confirmed_lot_ids or not state.trace_event_ids or not state.reconciliation:
                raise ClosureBlockedError("closure blocked: nonempty evidence is required")
            if {item.lot_id for item in state.reconciliation} != set(state.confirmed_lot_ids):
                raise ClosureBlockedError(
                    "closure blocked: reconciliation does not cover confirmed lots"
                )
            try:
                self._validate_authoritative_evidence(
                    confirmed_lot_ids=state.confirmed_lot_ids,
                    trace_event_ids=state.trace_event_ids,
                    required_facilities=state.required_facilities,
                    reconciliation=state.reconciliation,
                )
            except ValueError as error:
                raise ClosureBlockedError(f"closure blocked: {error}") from error
            if any(item.unaccounted != 0 for item in state.reconciliation):
                remaining = sum(item.unaccounted for item in state.reconciliation)
                raise ClosureBlockedError(
                    f"closure blocked: {remaining} unaccounted reconciliation units remain"
                )
            if any(
                not item.verified or not item.evidence_ids or not item.component_evidence
                for item in state.reconciliation
            ):
                raise ClosureBlockedError("closure blocked: reconciliation is not verified")
            trace_event_ids = set(state.trace_event_ids)
            event_components = {"received", "quarantined", "sold", "returned", "disposed"}
            if any(
                not {
                    evidence_id
                    for component in event_components
                    for evidence_id in item.component_evidence[component]
                }.issubset(trace_event_ids)
                for item in state.reconciliation
            ):
                raise ClosureBlockedError(
                    "closure blocked: reconciliation trace evidence is missing"
                )
            if state.evidence_gaps or any(
                item.get("classification") == "ambiguous" for item in state.candidate_lots
            ):
                raise ClosureBlockedError("closure blocked: unresolved evidence remains")
            tasks = conn.execute(
                """
                SELECT task.facility_id, task.status, COALESCE(ack.acknowledged, 0) AS acknowledged
                FROM tasks AS task
                LEFT JOIN acknowledgements AS ack
                  ON ack.case_id=task.case_id AND ack.facility_id=task.facility_id
                WHERE task.case_id=?
                """,
                (case_id,),
            ).fetchall()
            task_facilities = {row["facility_id"] for row in tasks}
            if not set(state.required_facilities).issubset(task_facilities) or any(
                not row["acknowledged"] or row["status"] != "acknowledged" for row in tasks
            ):
                raise ClosureBlockedError("closure blocked: facility acknowledgement remains")

        return self._mutate(
            case_id=case_id,
            action="close_case",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            details={},
            target_ids=[],
            transform=lambda s: s.model_copy(update={"status": "closed"}),
            validator=validator,
        )
