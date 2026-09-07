"""Transactional SQLite-backed, approval-gated simulated operations."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from recallops.agents.specialists import investigate_recall
from recallops.config import get_settings
from recallops.data.loaders import load_recall_snapshot
from recallops.models import (
    ApprovalDecision,
    AuditReceipt,
    Disposition,
    DispositionEvent,
    ProposedAction,
    RecallCaseState,
    Reconciliation,
    proposed_action_digest,
    validate_case_version,
)
from recallops.services.traceability import TraceabilityService

INITIAL_CHECKPOINT_HEAD = "__recallops_initial_checkpoint__"
WORKFLOW_MUTATION_LEASE_SECONDS = 30.0


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


class _OperationsStore:
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
                      case_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE,
                      owner_token TEXT, checkpoint_head TEXT, attempt_token TEXT,
                      attempt_expected_head TEXT, attempt_request_digest TEXT,
                      attempt_execution_id TEXT, attempt_execution_request_digest TEXT,
                      attempt_state TEXT,
                      attempt_expires_at REAL);
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
                    CREATE TABLE IF NOT EXISTS inventory_holds (
                      case_id TEXT NOT NULL REFERENCES cases(case_id), lot_id TEXT NOT NULL,
                      receipt_id TEXT NOT NULL, held_at TEXT NOT NULL,
                      PRIMARY KEY(case_id, lot_id));
                    CREATE TABLE IF NOT EXISTS disposition_events (
                      event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(case_id),
                      lot_id TEXT NOT NULL, event_json TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS execution_grants (
                      grant_token TEXT PRIMARY KEY, case_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                      checkpoint_head TEXT NOT NULL, case_version INTEGER NOT NULL,
                      workflow_request_digest TEXT NOT NULL, execution_id TEXT NOT NULL,
                      execution_request_digest TEXT NOT NULL, action_type TEXT NOT NULL,
                      action_digest TEXT NOT NULL, actor TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL, operation_request_hash TEXT NOT NULL,
                      consumed_at REAL, receipt_id TEXT,
                      UNIQUE(case_id, idempotency_key, operation_request_hash));
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
            identity_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(workflow_identities)")
            }
            if "owner_token" not in identity_columns:
                connection.execute("ALTER TABLE workflow_identities ADD COLUMN owner_token TEXT")
            for column, declaration in (
                ("checkpoint_head", "TEXT"),
                ("attempt_token", "TEXT"),
                ("attempt_expected_head", "TEXT"),
                ("attempt_request_digest", "TEXT"),
                ("attempt_execution_id", "TEXT"),
                ("attempt_execution_request_digest", "TEXT"),
                ("attempt_state", "TEXT"),
                ("attempt_expires_at", "REAL"),
            ):
                if column not in identity_columns:
                    connection.execute(
                        f"ALTER TABLE workflow_identities ADD COLUMN {column} {declaration}"
                    )
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
                        "INSERT INTO workflow_identities (case_id, thread_id) VALUES (?, ?)",
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
        recall_number: str,
        confirmed_lot_ids: list[str],
        trace_event_ids: list[str],
        required_facilities: list[str],
        reconciliation: list[Reconciliation],
    ) -> tuple[Any, Any, list[dict[str, Any]]]:
        try:
            recall = load_recall_snapshot(recall_number, data_dir=self.traceability.data_dir)
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise ValueError(f"authoritative recall {recall_number!r} does not exist") from error
        if recall.recall_number != recall_number:
            raise ValueError("authoritative recall identifier mismatch")
        intelligence = investigate_recall(recall)
        lot_matches = {
            item["lot_id"]: item for item in self.traceability.match_lots(intelligence.predicate)
        }
        known_lot_ids = {item["lot_id"] for item in self.traceability.dataset["lots"]}
        unknown_lot_ids = set(confirmed_lot_ids) - known_lot_ids
        if unknown_lot_ids:
            raise ValueError(
                f"authoritative evidence has no such lot(s): {sorted(unknown_lot_ids)}"
            )
        ineligible = {
            lot_id: lot_matches[lot_id]["classification"]
            for lot_id in confirmed_lot_ids
            if lot_matches[lot_id]["classification"] not in {"exact", "probable"}
        }
        if ineligible:
            raise ValueError(
                f"recall predicate classification rejects the requested case lot(s): {ineligible}"
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
        return recall, intelligence.predicate, [lot_matches[lot_id] for lot_id in confirmed_lot_ids]

    def _lot_evidence_ids(self, lot_id: str) -> set[str]:
        return {event["event_id"] for event in self.traceability.trace_forward(lot_id)} | {
            position["position_id"] for position in self.traceability.get_inventory(lot_id)
        }

    def _facility_evidence_ids(self, state: RecallCaseState, facility_id: str) -> set[str]:
        evidence: set[str] = set()
        for lot_id in state.confirmed_lot_ids:
            events = self.traceability.trace_forward(lot_id)
            positions = self.traceability.get_inventory(lot_id)
            if any(
                facility_id in {event.get("from_facility"), event.get("to_facility")}
                for event in events
            ) or any(position["facility_id"] == facility_id for position in positions):
                evidence.update(self._lot_evidence_ids(lot_id))
        return evidence

    def _reconciliation_with_dispositions(
        self,
        lot_id: str,
        events: list[DispositionEvent],
    ) -> Reconciliation:
        base = self.traceability.reconcile_units(lot_id)
        quantities = base.model_dump(mode="python")
        component_evidence = {
            component: list(identifiers)
            for component, identifiers in base.component_evidence.items()
        }
        remaining = base.unaccounted
        component_by_disposition = {
            "dispose_unaccounted": "disposed",
            "quarantined": "quarantined",
            "returned": "returned",
        }
        for event in events:
            if event.lot_id != lot_id or event.quantity > remaining:
                raise ClosureBlockedError("disposition event exceeds the authoritative residual")
            component = component_by_disposition[event.disposition]
            quantities[component] += event.quantity
            remaining -= event.quantity
            component_evidence[component] = [
                *component_evidence[component],
                event.event_id,
            ]
        quantities["unaccounted"] = remaining
        evidence_ids = list(
            dict.fromkeys(
                identifier
                for identifiers in component_evidence.values()
                for identifier in identifiers
            )
        )
        return Reconciliation.model_validate(
            {
                **quantities,
                "evidence_ids": evidence_ids,
                "component_evidence": component_evidence,
                "verified": True,
            }
        )

    @staticmethod
    def _require_related_target_evidence(
        proposed_action: ProposedAction,
        expected: dict[str, set[str]],
    ) -> None:
        supplied = {
            target: set(identifiers)
            for target, identifiers in proposed_action.evidence_by_target.items()
        }
        if set(supplied) != set(expected) or any(
            not supplied[target] or not supplied[target].issubset(expected[target])
            for target in expected
        ):
            raise ClosureBlockedError(
                "action evidence must be complete, authoritative, and related to each target"
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

    def reserve_workflow_identity(
        self,
        case_id: str,
        thread_id: str,
        owner_token: str,
    ) -> bool:
        """Atomically reserve the public case/thread identity before graph initialization."""
        if type(case_id) is not str or not case_id.strip():
            raise ValueError("case_id must be a nonblank exact string")
        if type(thread_id) is not str or not thread_id.strip():
            raise ValueError("thread_id must be a nonblank exact string")
        if type(owner_token) is not str or not owner_token.strip():
            raise ValueError("owner_token must be a nonblank exact string")
        try:
            with self._transaction() as conn:
                by_case = conn.execute(
                    "SELECT thread_id, owner_token FROM workflow_identities WHERE case_id=?",
                    (case_id,),
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
                    if by_case["owner_token"] != owner_token:
                        raise ValueError(
                            f"case_id {case_id!r} is reserved by another checkpoint store"
                        )
                    return False
                conn.execute(
                    "INSERT INTO workflow_identities "
                    "(case_id, thread_id, owner_token, checkpoint_head) VALUES (?, ?, ?, ?)",
                    (case_id, thread_id, owner_token, INITIAL_CHECKPOINT_HEAD),
                )
                return True
        except ValueError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to reserve workflow identity: {error}") from error

    def validate_or_claim_workflow_identity(
        self,
        case_id: str,
        thread_id: str,
        owner_token: str,
        *,
        legacy_owner_token: str,
        checkpoint_case_id: str,
        checkpoint_thread_id: str,
        checkpoint_id: str,
    ) -> None:
        """Validate every resume and atomically migrate only proven legacy ownership."""
        for name, value in (
            ("case_id", case_id),
            ("thread_id", thread_id),
            ("owner_token", owner_token),
            ("legacy_owner_token", legacy_owner_token),
            ("checkpoint_case_id", checkpoint_case_id),
            ("checkpoint_thread_id", checkpoint_thread_id),
            ("checkpoint_id", checkpoint_id),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonblank exact string")
        if checkpoint_case_id != case_id or checkpoint_thread_id != thread_id:
            raise ValueError("checkpoint identity does not match the requested case/thread")
        try:
            with self._transaction() as conn:
                by_case = conn.execute(
                    "SELECT thread_id, owner_token FROM workflow_identities WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                by_thread = conn.execute(
                    "SELECT case_id, owner_token FROM workflow_identities WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
                if by_case is None or by_thread is None:
                    raise ValueError("workflow identity is not reserved in this Operations store")
                if by_case["thread_id"] != thread_id or by_thread["case_id"] != case_id:
                    raise ValueError("workflow identity is bound to another case or thread")
                stored_owner = by_case["owner_token"]
                if stored_owner is None:
                    updated = conn.execute(
                        "UPDATE workflow_identities SET owner_token=? "
                        "WHERE case_id=? AND thread_id=? AND owner_token IS NULL",
                        (owner_token, case_id, thread_id),
                    )
                    if updated.rowcount != 1:
                        stored_owner = conn.execute(
                            "SELECT owner_token FROM workflow_identities WHERE case_id=?",
                            (case_id,),
                        ).fetchone()["owner_token"]
                        if stored_owner != owner_token:
                            raise ValueError(
                                f"case_id {case_id!r} is reserved by another checkpoint store"
                            )
                elif stored_owner != owner_token:
                    if stored_owner != legacy_owner_token:
                        raise ValueError(
                            f"case_id {case_id!r} is reserved by another checkpoint store"
                        )
                    updated = conn.execute(
                        "UPDATE workflow_identities SET owner_token=? "
                        "WHERE case_id=? AND thread_id=? AND owner_token=?",
                        (owner_token, case_id, thread_id, stored_owner),
                    )
                    if updated.rowcount != 1:
                        raise ValueError(
                            f"case_id {case_id!r} is reserved by another checkpoint store"
                        )
        except ValueError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to validate workflow identity: {error}") from error

    @staticmethod
    def _validate_request_digest(request_digest: str) -> None:
        if (
            type(request_digest) is not str
            or len(request_digest) != 64
            or any(character not in "0123456789abcdef" for character in request_digest)
        ):
            raise ValueError("request_digest must be a canonical SHA-256 hex digest")

    def issue_workflow_execution_grant(
        self,
        *,
        owner_token: str,
        case_id: str,
        thread_id: str,
        proposed_action: ProposedAction,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
        execution_id: str,
        execution_request_digest: str,
        details: dict[str, Any],
        target_ids: list[str],
        evidence_ids: list[str] | None = None,
    ) -> str:
        """Mint a one-use capability only while an owned workflow resume is active.

        This method is intentionally absent from every MCP surface. The LangGraph executor
        calls it after both review and execution-confirmation interrupts have completed.
        """

        for name, value in (
            ("case_id", case_id),
            ("thread_id", thread_id),
            ("owner_token", owner_token),
            ("execution_id", execution_id),
            ("idempotency_key", idempotency_key),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonblank exact string")
        self._validate_request_digest(execution_request_digest)
        self._approval(
            approval,
            expected_case_version,
            case_id=case_id,
            action_type=proposed_action.action_type,
            proposed_action=proposed_action,
            target_ids=target_ids,
            evidence_ids=evidence_ids,
        )
        operation_request_hash = self._request_hash(
            case_id,
            proposed_action.action_type,
            expected_case_version,
            details,
            approval,
            proposed_action,
        )
        action_digest = proposed_action_digest(proposed_action)
        try:
            with self._transaction() as conn:
                identity = conn.execute(
                    "SELECT thread_id, owner_token, checkpoint_head, attempt_token, "
                    "attempt_expected_head, attempt_request_digest, attempt_execution_id, "
                    "attempt_execution_request_digest, attempt_state, attempt_expires_at "
                    "FROM workflow_identities WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if (
                    identity is None
                    or identity["thread_id"] != thread_id
                    or identity["owner_token"] != owner_token
                    or not identity["checkpoint_head"]
                    or not identity["attempt_token"]
                    or identity["attempt_expected_head"] != identity["checkpoint_head"]
                    or not identity["attempt_request_digest"]
                    or identity["attempt_execution_id"] != execution_id
                    or identity["attempt_execution_request_digest"] != execution_request_digest
                    or identity["attempt_state"] != "active"
                    or (identity["attempt_expires_at"] or 0) <= time.time()
                ):
                    raise ApprovalRequiredError(
                        "execution grant requires an active trusted workflow checkpoint resume"
                    )
                completed = conn.execute(
                    "SELECT request_hash FROM receipts WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if completed is not None:
                    if completed["request_hash"] != operation_request_hash:
                        raise IdempotencyConflictError(
                            "idempotency key is bound to a different request"
                        )
                    completed_grant = conn.execute(
                        "SELECT grant_token FROM execution_grants "
                        "WHERE case_id=? AND idempotency_key=? AND operation_request_hash=?",
                        (case_id, idempotency_key, operation_request_hash),
                    ).fetchone()
                    if completed_grant is None:
                        raise OperationStoreError(
                            "completed workflow write is missing its execution-grant audit record"
                        )
                    return str(completed_grant["grant_token"])
                existing = conn.execute(
                    "SELECT * FROM execution_grants WHERE case_id=? AND idempotency_key=?",
                    (case_id, idempotency_key),
                ).fetchone()
                bindings = {
                    "case_id": case_id,
                    "thread_id": thread_id,
                    "checkpoint_head": identity["checkpoint_head"],
                    "case_version": expected_case_version,
                    "workflow_request_digest": identity["attempt_request_digest"],
                    "execution_id": execution_id,
                    "execution_request_digest": execution_request_digest,
                    "action_type": proposed_action.action_type,
                    "action_digest": action_digest,
                    "actor": approval.actor,
                    "idempotency_key": idempotency_key,
                    "operation_request_hash": operation_request_hash,
                }
                if existing is not None:
                    if any(existing[name] != value for name, value in bindings.items()):
                        raise IdempotencyConflictError(
                            "execution grant is bound to a different workflow request"
                        )
                    return str(existing["grant_token"])
                token = secrets.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO execution_grants "
                    "(grant_token, case_id, thread_id, checkpoint_head, case_version, "
                    "workflow_request_digest, execution_id, execution_request_digest, "
                    "action_type, action_digest, actor, idempotency_key, "
                    "operation_request_hash, consumed_at, receipt_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (token, *bindings.values()),
                )
                return token
        except (ApprovalRequiredError, IdempotencyConflictError):
            raise
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to issue execution grant: {error}") from error

    @staticmethod
    def _consume_execution_grant(
        conn: sqlite3.Connection,
        *,
        token: str | None,
        case_id: str,
        thread_id: str,
        action: str,
        expected: int,
        key: str,
        request_hash: str,
        proposed_action: ProposedAction,
        actor: str,
    ) -> None:
        if type(token) is not str or not token.strip():
            raise ApprovalRequiredError("write requires a one-time workflow execution grant")
        row = conn.execute(
            "SELECT grant.*, identity.checkpoint_head AS active_checkpoint_head, "
            "identity.attempt_expected_head AS active_attempt_head, "
            "identity.attempt_request_digest AS active_request_digest, "
            "identity.attempt_execution_id AS active_execution_id, "
            "identity.attempt_execution_request_digest AS active_execution_request_digest, "
            "identity.attempt_state AS active_attempt_state, "
            "identity.attempt_expires_at AS active_attempt_expires_at "
            "FROM execution_grants AS grant "
            "JOIN workflow_identities AS identity "
            "ON identity.case_id=grant.case_id AND identity.thread_id=grant.thread_id "
            "WHERE grant.grant_token=?",
            (token,),
        ).fetchone()
        expected_bindings = {
            "case_id": case_id,
            "thread_id": thread_id,
            "case_version": expected,
            "action_type": action,
            "action_digest": proposed_action_digest(proposed_action),
            "actor": actor,
            "idempotency_key": key,
            "operation_request_hash": request_hash,
        }
        if row is None or any(row[name] != value for name, value in expected_bindings.items()):
            raise ApprovalRequiredError("execution grant does not match the exact reviewed write")
        if (
            row["active_checkpoint_head"] != row["checkpoint_head"]
            or row["active_attempt_head"] != row["checkpoint_head"]
            or row["active_request_digest"] != row["workflow_request_digest"]
            or row["active_execution_id"] != row["execution_id"]
            or row["active_execution_request_digest"] != row["execution_request_digest"]
            or row["active_attempt_state"] != "active"
            or (row["active_attempt_expires_at"] or 0) <= time.time()
        ):
            raise ApprovalRequiredError(
                "execution grant is no longer attached to its active workflow resume"
            )
        if row["consumed_at"] is not None:
            raise ApprovalRequiredError("execution grant has already been consumed")
        updated = conn.execute(
            "UPDATE execution_grants SET consumed_at=? WHERE grant_token=? AND consumed_at IS NULL",
            (time.time(), token),
        )
        if updated.rowcount != 1:
            raise ApprovalRequiredError("execution grant has already been consumed")

    def claim_workflow_mutation(
        self,
        case_id: str,
        thread_id: str,
        owner_token: str,
        expected_checkpoint_head: str,
        attempt_token: str,
        request_digest: str,
        *,
        execution_id: str | None = None,
        execution_request_digest: str | None = None,
        lease_seconds: float = WORKFLOW_MUTATION_LEASE_SECONDS,
    ) -> None:
        """Atomically fence one mutation attempt at the exact durable checkpoint head."""
        for name, value in (
            ("case_id", case_id),
            ("thread_id", thread_id),
            ("owner_token", owner_token),
            ("expected_checkpoint_head", expected_checkpoint_head),
            ("attempt_token", attempt_token),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonblank exact string")
        self._validate_request_digest(request_digest)
        if (execution_id is None) != (execution_request_digest is None):
            raise ValueError("execution_id and execution_request_digest must be supplied together")
        if execution_id is not None:
            if type(execution_id) is not str or not execution_id.strip():
                raise ValueError("execution_id must be a nonblank exact string")
            self._validate_request_digest(execution_request_digest)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
            raise TypeError("lease_seconds must be a positive number")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        conflict: str | None = None
        try:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT thread_id, owner_token, checkpoint_head, attempt_token, "
                    "attempt_expected_head, attempt_request_digest, attempt_execution_id, "
                    "attempt_execution_request_digest, attempt_state, attempt_expires_at "
                    "FROM workflow_identities "
                    "WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if row is None or row["thread_id"] != thread_id:
                    raise ValueError("workflow mutation identity is not reserved exactly")
                if row["owner_token"] != owner_token:
                    raise ValueError("workflow mutation is owned by another checkpoint store")
                checkpoint_head = row["checkpoint_head"]
                if checkpoint_head is None:
                    checkpoint_head = expected_checkpoint_head
                    conn.execute(
                        "UPDATE workflow_identities SET checkpoint_head=? WHERE case_id=?",
                        (checkpoint_head, case_id),
                    )
                if checkpoint_head != expected_checkpoint_head:
                    raise ValueError(
                        "checkpoint head is stale and cannot fork the durable workflow"
                    )
                expires_at = time.time() + float(lease_seconds)
                if row["attempt_token"] is None:
                    conn.execute(
                        "UPDATE workflow_identities SET attempt_token=?, "
                        "attempt_expected_head=?, attempt_request_digest=?, "
                        "attempt_execution_id=?, attempt_execution_request_digest=?, "
                        "attempt_state='active', attempt_expires_at=? "
                        "WHERE case_id=? AND thread_id=? AND owner_token=?",
                        (
                            attempt_token,
                            expected_checkpoint_head,
                            request_digest,
                            execution_id,
                            execution_request_digest,
                            expires_at,
                            case_id,
                            thread_id,
                            owner_token,
                        ),
                    )
                elif (
                    row["attempt_token"] == attempt_token
                    and row["attempt_expected_head"] == expected_checkpoint_head
                ):
                    if row["attempt_request_digest"] != request_digest:
                        raise ValueError(
                            "workflow mutation request digest does not match this attempt"
                        )
                    if (
                        row["attempt_execution_id"] != execution_id
                        or row["attempt_execution_request_digest"] != execution_request_digest
                    ):
                        raise ValueError("workflow execution request does not match this attempt")
                    elif (
                        row["attempt_state"] == "active"
                        and (row["attempt_expires_at"] or 0) > time.time()
                    ):
                        conflict = "workflow mutation is already active or uncertain"
                    else:
                        conn.execute(
                            "UPDATE workflow_identities SET attempt_state='active', "
                            "attempt_expires_at=? WHERE case_id=? AND attempt_token=?",
                            (expires_at, case_id, attempt_token),
                        )
                else:
                    if (row["attempt_expires_at"] or 0) <= time.time():
                        conn.execute(
                            "UPDATE workflow_identities SET attempt_state='uncertain' "
                            "WHERE case_id=?",
                            (case_id,),
                        )
                    conflict = "workflow mutation is already active or uncertain"
            if conflict is not None:
                raise ValueError(conflict)
        except ValueError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to claim workflow mutation: {error}") from error

    def advance_workflow_mutation(
        self,
        case_id: str,
        thread_id: str,
        owner_token: str,
        expected_checkpoint_head: str,
        new_checkpoint_head: str,
        attempt_token: str,
        request_digest: str,
    ) -> None:
        """Atomically bind the successful checkpoint head and close its fencing attempt."""
        for name, value in (
            ("case_id", case_id),
            ("thread_id", thread_id),
            ("owner_token", owner_token),
            ("expected_checkpoint_head", expected_checkpoint_head),
            ("new_checkpoint_head", new_checkpoint_head),
            ("attempt_token", attempt_token),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonblank exact string")
        self._validate_request_digest(request_digest)
        try:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT thread_id, owner_token, checkpoint_head, attempt_token, "
                    "attempt_expected_head, attempt_request_digest FROM workflow_identities "
                    "WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if row is None or row["thread_id"] != thread_id:
                    raise ValueError("workflow mutation identity is not reserved exactly")
                if row["owner_token"] != owner_token:
                    raise ValueError("workflow mutation is owned by another checkpoint store")
                if row["checkpoint_head"] == new_checkpoint_head and row["attempt_token"] is None:
                    return
                if (
                    row["checkpoint_head"] != expected_checkpoint_head
                    or row["attempt_token"] != attempt_token
                    or row["attempt_expected_head"] != expected_checkpoint_head
                    or row["attempt_request_digest"] != request_digest
                ):
                    raise ValueError("workflow mutation fence no longer matches this attempt")
                conn.execute(
                    "UPDATE workflow_identities SET checkpoint_head=?, attempt_token=NULL, "
                    "attempt_expected_head=NULL, attempt_request_digest=NULL, "
                    "attempt_execution_id=NULL, attempt_execution_request_digest=NULL, "
                    "attempt_state=NULL, attempt_expires_at=NULL "
                    "WHERE case_id=?",
                    (new_checkpoint_head, case_id),
                )
        except ValueError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to advance workflow mutation: {error}") from error

    def release_workflow_mutation(
        self,
        case_id: str,
        thread_id: str,
        owner_token: str,
        expected_checkpoint_head: str,
        attempt_token: str,
        request_digest: str,
    ) -> None:
        """Release an attempt only while its checkpoint head is provably unchanged."""
        for name, value in (
            ("case_id", case_id),
            ("thread_id", thread_id),
            ("owner_token", owner_token),
            ("expected_checkpoint_head", expected_checkpoint_head),
            ("attempt_token", attempt_token),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonblank exact string")
        self._validate_request_digest(request_digest)
        try:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT thread_id, owner_token, checkpoint_head, attempt_token, "
                    "attempt_expected_head, attempt_request_digest FROM workflow_identities "
                    "WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if row is None or row["thread_id"] != thread_id:
                    raise ValueError("workflow mutation identity is not reserved exactly")
                if row["owner_token"] != owner_token:
                    raise ValueError("workflow mutation is owned by another checkpoint store")
                if (
                    row["attempt_token"] is None
                    and row["checkpoint_head"] == expected_checkpoint_head
                ):
                    return
                if (
                    row["checkpoint_head"] != expected_checkpoint_head
                    or row["attempt_token"] != attempt_token
                    or row["attempt_expected_head"] != expected_checkpoint_head
                    or row["attempt_request_digest"] != request_digest
                ):
                    raise ValueError("workflow mutation cannot be released after its head changed")
                conn.execute(
                    "UPDATE workflow_identities SET attempt_token=NULL, "
                    "attempt_expected_head=NULL, attempt_request_digest=NULL, "
                    "attempt_execution_id=NULL, attempt_execution_request_digest=NULL, "
                    "attempt_state=NULL, attempt_expires_at=NULL "
                    "WHERE case_id=?",
                    (case_id,),
                )
        except ValueError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to release workflow mutation: {error}") from error

    def recover_workflow_mutation(
        self,
        case_id: str,
        thread_id: str,
        owner_token: str,
        expected_checkpoint_head: str,
        recovered_checkpoint_head: str,
        attempt_token: str,
        request_digest: str,
    ) -> None:
        """Finish an uncertain attempt only for the store carrying its persisted token."""
        for name, value in (
            ("case_id", case_id),
            ("thread_id", thread_id),
            ("owner_token", owner_token),
            ("expected_checkpoint_head", expected_checkpoint_head),
            ("recovered_checkpoint_head", recovered_checkpoint_head),
            ("attempt_token", attempt_token),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonblank exact string")
        self._validate_request_digest(request_digest)
        try:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT thread_id, owner_token, checkpoint_head, attempt_token, "
                    "attempt_expected_head, attempt_request_digest FROM workflow_identities "
                    "WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if row is None or row["thread_id"] != thread_id:
                    raise ValueError("workflow mutation identity is not reserved exactly")
                if row["owner_token"] != owner_token:
                    raise ValueError("workflow mutation is owned by another checkpoint store")
                if (
                    row["checkpoint_head"] == recovered_checkpoint_head
                    and row["attempt_token"] is None
                ):
                    return
                if (
                    row["checkpoint_head"] != expected_checkpoint_head
                    or row["attempt_token"] != attempt_token
                    or row["attempt_expected_head"] != expected_checkpoint_head
                    or row["attempt_request_digest"] != request_digest
                    or recovered_checkpoint_head == expected_checkpoint_head
                ):
                    raise ValueError("workflow mutation recovery proof does not match")
                conn.execute(
                    "UPDATE workflow_identities SET checkpoint_head=?, attempt_token=NULL, "
                    "attempt_expected_head=NULL, attempt_request_digest=NULL, "
                    "attempt_execution_id=NULL, attempt_execution_request_digest=NULL, "
                    "attempt_state=NULL, attempt_expires_at=NULL "
                    "WHERE case_id=?",
                    (recovered_checkpoint_head, case_id),
                )
        except ValueError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise OperationStoreError(f"unable to recover workflow mutation: {error}") from error

    def release_workflow_identity(
        self,
        case_id: str,
        thread_id: str,
        owner_token: str,
    ) -> None:
        """Release only an unmaterialized exact reservation after failed initialization."""
        try:
            with self._transaction() as conn:
                if conn.execute("SELECT 1 FROM cases WHERE case_id=?", (case_id,)).fetchone():
                    return
                conn.execute(
                    "DELETE FROM workflow_identities "
                    "WHERE case_id=? AND thread_id=? AND owner_token=?",
                    (case_id, thread_id, owner_token),
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
        execution_grant: str | None,
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
                self._consume_execution_grant(
                    conn,
                    token=execution_grant,
                    case_id=case_id,
                    thread_id=state.thread_id,
                    action=action,
                    expected=expected,
                    key=key,
                    request_hash=request_hash,
                    proposed_action=proposed_action,
                    actor=approval.actor,
                )
                if validator:
                    validator(conn, state)
                if self._before_cas_hook:
                    self._before_cas_hook(action)
                if transform:
                    state = transform(state)
                receipt_details = dict(details)
                if action == "record_disposition":
                    receipt_details["disposition_event"] = state.disposition_events[-1].model_dump(
                        mode="json"
                    )
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
                        **receipt_details,
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
                conn.execute(
                    "UPDATE execution_grants SET receipt_id=? WHERE grant_token=?",
                    (receipt.receipt_id, execution_grant),
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
                if action == "apply_inventory_hold":
                    for lot_id in details["lot_ids"]:
                        conn.execute(
                            "INSERT INTO inventory_holds VALUES (?, ?, ?, ?)",
                            (case_id, lot_id, receipt.receipt_id, receipt.created_at.isoformat()),
                        )
                if action == "record_disposition":
                    event = state.disposition_events[-1]
                    conn.execute(
                        "INSERT INTO disposition_events VALUES (?, ?, ?, ?)",
                        (event.event_id, case_id, event.lot_id, event.model_dump_json()),
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
        execution_grant: str | None = None,
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
                        "INSERT INTO workflow_identities (case_id, thread_id) VALUES (?, ?)",
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
                self._consume_execution_grant(
                    conn,
                    token=execution_grant,
                    case_id=case_id,
                    thread_id=durable_thread_id,
                    action="create_case",
                    expected=expected_case_version,
                    key=idempotency_key,
                    request_hash=request_hash,
                    proposed_action=proposed_action,
                    actor=approval.actor,
                )
                if expected_case_version != 0:
                    raise StaleCaseVersionError("new cases require expected version 0")
                recall, predicate, candidate_lots = self._validate_authoritative_evidence(
                    recall_number=recall_number,
                    confirmed_lot_ids=confirmed_lot_ids,
                    trace_event_ids=trace_event_ids,
                    required_facilities=required_facilities,
                    reconciliation=reconciliations,
                )
                exact_case_evidence = {
                    lot_id: {event["event_id"] for event in self.traceability.trace_forward(lot_id)}
                    for lot_id in confirmed_lot_ids
                }
                supplied_case_evidence = {
                    target: set(identifiers)
                    for target, identifiers in proposed_action.evidence_by_target.items()
                }
                if supplied_case_evidence != exact_case_evidence:
                    raise ValueError(
                        "authoritative case action evidence must cover exact lot trace events"
                    )
                authoritative_gaps = [
                    f"{item.lot_id}: {item.unaccounted} unaccounted units"
                    for item in reconciliations
                    if item.unaccounted
                ]
                if evidence_gaps != authoritative_gaps:
                    raise ValueError(
                        "authoritative evidence gaps must be complete, exact, and caller-independent"
                    )
                state = RecallCaseState(
                    case_id=case_id,
                    thread_id=durable_thread_id,
                    recall_number=recall_number,
                    question=question,
                    source_mode=self.source_mode,
                    recall=recall,
                    recall_predicate=predicate,
                    candidate_lots=candidate_lots,
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
                conn.execute(
                    "UPDATE execution_grants SET receipt_id=? WHERE grant_token=?",
                    (receipt.receipt_id, execution_grant),
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
        execution_grant: str | None = None,
    ) -> AuditReceipt:
        if not lot_ids or any(type(item) is not str or not item.strip() for item in lot_ids):
            raise ValueError("lot_ids must be nonempty exact strings")
        if len(lot_ids) != len(set(lot_ids)):
            raise ValueError("lot_ids must be unique")

        def validator(conn: sqlite3.Connection, state: RecallCaseState) -> None:
            unrelated = set(lot_ids) - set(state.confirmed_lot_ids)
            if unrelated:
                raise ClosureBlockedError(
                    f"inventory hold lots are outside the eligible case scope: {sorted(unrelated)}"
                )
            already_held = {
                row["lot_id"]
                for row in conn.execute(
                    "SELECT lot_id FROM inventory_holds WHERE case_id=?", (case_id,)
                ).fetchall()
            } & set(lot_ids)
            if already_held:
                raise ClosureBlockedError(
                    f"inventory hold already exists for case lot(s): {sorted(already_held)}"
                )
            self._require_related_target_evidence(
                proposed_action,
                {lot_id: self._lot_evidence_ids(lot_id) for lot_id in lot_ids},
            )

        return self._mutate(
            case_id=case_id,
            action="apply_inventory_hold",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            execution_grant=execution_grant,
            details={"lot_ids": lot_ids},
            target_ids=lot_ids,
            validator=validator,
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
        execution_grant: str | None = None,
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

        def validator(conn: sqlite3.Connection, state: RecallCaseState) -> None:
            if conn.execute("SELECT 1 FROM tasks WHERE case_id=? LIMIT 1", (case_id,)).fetchone():
                raise ClosureBlockedError("facility tasks already exist for this case")
            unrelated = set(facility_ids) - set(state.required_facilities)
            if unrelated:
                raise ClosureBlockedError(
                    "facility tasks must target authoritative required facilities: "
                    f"{sorted(unrelated)}"
                )
            if set(facility_ids) != set(state.required_facilities):
                raise ClosureBlockedError(
                    "facility tasks must cover every authoritative required facility"
                )
            held = {
                row["lot_id"]
                for row in conn.execute(
                    "SELECT lot_id FROM inventory_holds WHERE case_id=?", (case_id,)
                ).fetchall()
            }
            if held != set(state.confirmed_lot_ids):
                raise ClosureBlockedError(
                    "facility tasks require prior inventory holds for every case lot"
                )
            self._require_related_target_evidence(
                proposed_action,
                {
                    facility_id: self._facility_evidence_ids(state, facility_id)
                    for facility_id in facility_ids
                },
            )

        return self._mutate(
            case_id=case_id,
            action="create_facility_tasks",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            execution_grant=execution_grant,
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
        execution_grant: str | None = None,
    ) -> AuditReceipt:
        def validator(conn: sqlite3.Connection, state: RecallCaseState) -> None:
            if not conn.execute(
                "SELECT 1 FROM tasks WHERE case_id=? AND facility_id=?", (case_id, facility_id)
            ).fetchone():
                raise ClosureBlockedError("acknowledgment requires an existing facility task")
            self._require_related_target_evidence(
                proposed_action,
                {facility_id: self._facility_evidence_ids(state, facility_id)},
            )

        return self._mutate(
            case_id=case_id,
            action="record_acknowledgment",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            execution_grant=execution_grant,
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
        execution_grant: str | None = None,
    ) -> AuditReceipt:
        if disposition not in {"dispose_unaccounted", "quarantined", "returned"}:
            raise ValueError("invalid disposition")
        if not evidence_id.strip():
            raise ValueError("evidence_id must be nonblank")

        event_holder: dict[str, DispositionEvent] = {}

        def validator(conn: sqlite3.Connection, state: RecallCaseState) -> None:
            if lot_id not in state.confirmed_lot_ids:
                raise ClosureBlockedError("disposition lot is outside the eligible case scope")
            if not conn.execute(
                "SELECT 1 FROM inventory_holds WHERE case_id=? AND lot_id=?",
                (case_id, lot_id),
            ).fetchone():
                raise ClosureBlockedError("disposition requires a prior valid inventory hold")
            existing = [event for event in state.disposition_events if event.lot_id == lot_id]
            current = self._reconciliation_with_dispositions(lot_id, existing)
            persisted = next((item for item in state.reconciliation if item.lot_id == lot_id), None)
            if persisted != current:
                raise ClosureBlockedError(
                    "persisted reconciliation conflicts with append-only disposition authority"
                )
            if current.unaccounted <= 0:
                raise ClosureBlockedError("disposition requires a positive unaccounted residual")
            if evidence_id not in current.component_evidence["unaccounted"]:
                raise ClosureBlockedError(
                    "disposition source evidence is not authoritative for this lot residual"
                )
            self._require_related_target_evidence(proposed_action, {lot_id: {evidence_id}})
            event_holder["event"] = DispositionEvent(
                event_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"{case_id}:disposition-event:{lot_id}:{idempotency_key}",
                    )
                ),
                lot_id=lot_id,
                disposition=disposition,
                quantity=current.unaccounted,
                occurred_at=datetime.now(UTC),
                provenance="WORKFLOW_APPROVED_SYNTHETIC_DISPOSITION",
                source_evidence_ids=(evidence_id,),
            )

        def transform(state: RecallCaseState) -> RecallCaseState:
            event = event_holder["event"]
            disposition_events = [*state.disposition_events, event]
            reconciliations = [
                self._reconciliation_with_dispositions(
                    item.lot_id,
                    [entry for entry in disposition_events if entry.lot_id == item.lot_id],
                )
                for item in state.reconciliation
            ]
            gaps = [
                f"{item.lot_id}: {item.unaccounted} unaccounted units"
                for item in reconciliations
                if item.unaccounted
            ]
            return state.model_copy(
                update={
                    "reconciliation": reconciliations,
                    "disposition_events": disposition_events,
                    "evidence_gaps": gaps,
                }
            )

        return self._mutate(
            case_id=case_id,
            action="record_disposition",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            execution_grant=execution_grant,
            details={"lot_id": lot_id, "disposition": disposition, "evidence_id": evidence_id},
            target_ids=[lot_id],
            evidence_ids=[evidence_id],
            transform=transform,
            validator=validator,
        )

    def close_case(
        self,
        *,
        case_id: str,
        proposed_action: ProposedAction,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
        execution_grant: str | None = None,
    ) -> AuditReceipt:
        def validator(conn: sqlite3.Connection, state: RecallCaseState) -> None:
            if not state.confirmed_lot_ids or not state.trace_event_ids or not state.reconciliation:
                raise ClosureBlockedError("closure blocked: nonempty evidence is required")
            held_lots = {
                row["lot_id"]
                for row in conn.execute(
                    "SELECT lot_id FROM inventory_holds WHERE case_id=?", (case_id,)
                ).fetchall()
            }
            if held_lots != set(state.confirmed_lot_ids):
                raise ClosureBlockedError(
                    "closure blocked: prior inventory hold is required for every case lot"
                )
            if {item.lot_id for item in state.reconciliation} != set(state.confirmed_lot_ids):
                raise ClosureBlockedError(
                    "closure blocked: reconciliation does not cover confirmed lots"
                )
            try:
                base_reconciliations = [
                    self.traceability.reconcile_units(lot_id) for lot_id in state.confirmed_lot_ids
                ]
                recall, predicate, candidate_lots = self._validate_authoritative_evidence(
                    recall_number=state.recall_number,
                    confirmed_lot_ids=state.confirmed_lot_ids,
                    trace_event_ids=state.trace_event_ids,
                    required_facilities=state.required_facilities,
                    reconciliation=base_reconciliations,
                )
            except ValueError as error:
                raise ClosureBlockedError(f"closure blocked: {error}") from error
            if (
                state.recall != recall
                or state.recall_predicate != predicate
                or state.candidate_lots != candidate_lots
            ):
                raise ClosureBlockedError(
                    "closure blocked: persisted recall predicate or lot authority changed"
                )
            disposition_rows = conn.execute(
                "SELECT event_json FROM disposition_events WHERE case_id=? ORDER BY rowid",
                (case_id,),
            ).fetchall()
            stored_dispositions = [
                DispositionEvent.model_validate_json(row["event_json"]) for row in disposition_rows
            ]
            if stored_dispositions != state.disposition_events:
                raise ClosureBlockedError(
                    "closure blocked: disposition event ledger disagrees with case state"
                )
            expected_reconciliations = [
                self._reconciliation_with_dispositions(
                    lot_id,
                    [event for event in stored_dispositions if event.lot_id == lot_id],
                )
                for lot_id in state.confirmed_lot_ids
            ]
            if state.reconciliation != expected_reconciliations:
                raise ClosureBlockedError(
                    "closure blocked: reconciliation disagrees with base and disposition events"
                )
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
            trace_event_ids = set(state.trace_event_ids) | {
                event.event_id for event in stored_dispositions
            }
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
            if task_facilities != set(state.required_facilities) or any(
                not row["acknowledged"] or row["status"] != "acknowledged" for row in tasks
            ):
                raise ClosureBlockedError("closure blocked: facility acknowledgement remains")
            if state.acknowledgements != {
                row["facility_id"]: bool(row["acknowledged"]) for row in tasks
            }:
                raise ClosureBlockedError(
                    "closure blocked: authoritative acknowledgements disagree with case state"
                )

        return self._mutate(
            case_id=case_id,
            action="close_case",
            approval=approval,
            proposed_action=proposed_action,
            expected=expected_case_version,
            key=idempotency_key,
            execution_grant=execution_grant,
            details={},
            target_ids=[],
            transform=lambda s: s.model_copy(update={"status": "closed"}),
            validator=validator,
        )


class OperationsService:
    """Consumer-facing Operations API: reads and grant-consuming mutations only."""

    __slots__ = ("__store",)

    def __init__(
        self,
        storage_path: Path | None = None,
        failure_injector: Callable[[str], None] | None = None,
        before_cas_hook: Callable[[str], None] | None = None,
        traceability: TraceabilityService | None = None,
    ) -> None:
        self.__store = _OperationsStore(
            storage_path=storage_path,
            failure_injector=failure_injector,
            before_cas_hook=before_cas_hook,
            traceability=traceability,
        )

    @property
    def storage_path(self) -> Path:
        return self.__store.storage_path

    @property
    def source_mode(self) -> str:
        return self.__store.source_mode

    @property
    def traceability(self) -> TraceabilityService:
        return self.__store.traceability

    def get_case(self, case_id: str) -> RecallCaseState | None:
        return self.__store.get_case(case_id)

    def get_receipt(self, idempotency_key: str) -> AuditReceipt | None:
        return self.__store.get_receipt(idempotency_key)

    def get_thread_for_case(self, case_id: str) -> str | None:
        return self.__store.get_thread_for_case(case_id)

    def get_case_id_for_thread(self, thread_id: str) -> str | None:
        return self.__store.get_case_id_for_thread(thread_id)

    def get_case_for_thread(self, thread_id: str) -> RecallCaseState | None:
        return self.__store.get_case_for_thread(thread_id)

    def get_lot_evidence_ids(self, lot_id: str) -> frozenset[str]:
        return frozenset(self.__store._lot_evidence_ids(lot_id))

    def get_facility_evidence_ids(
        self,
        state: RecallCaseState,
        facility_id: str,
    ) -> frozenset[str]:
        return frozenset(self.__store._facility_evidence_ids(state, facility_id))

    @property
    def cases(self) -> dict[str, RecallCaseState]:
        return self.__store.cases

    def create_case(self, **kwargs: Any) -> AuditReceipt:
        return self.__store.create_case(**kwargs)

    def apply_inventory_hold(self, **kwargs: Any) -> AuditReceipt:
        return self.__store.apply_inventory_hold(**kwargs)

    def create_facility_tasks(self, **kwargs: Any) -> AuditReceipt:
        return self.__store.create_facility_tasks(**kwargs)

    def record_acknowledgment(self, **kwargs: Any) -> AuditReceipt:
        return self.__store.record_acknowledgment(**kwargs)

    def record_disposition(self, **kwargs: Any) -> AuditReceipt:
        return self.__store.record_disposition(**kwargs)

    def close_case(self, **kwargs: Any) -> AuditReceipt:
        return self.__store.close_case(**kwargs)


class _WorkflowAuthorizationBroker:
    """Runtime-only owner capability for checkpoint fencing and grant issuance."""

    __slots__ = ("__store", "__owner_token")

    def __init__(self, service: OperationsService, owner_token: str) -> None:
        try:
            parsed = UUID(owner_token)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("workflow owner capability must be a canonical random UUID") from error
        if parsed.version != 4 or str(parsed) != owner_token:
            raise ValueError("workflow owner capability must be a canonical random UUID")
        self.__store = object.__getattribute__(service, "_OperationsService__store")
        self.__owner_token = owner_token

    def reserve_workflow_identity(self, case_id: str, thread_id: str) -> bool:
        return self.__store.reserve_workflow_identity(case_id, thread_id, self.__owner_token)

    def validate_or_claim_workflow_identity(
        self,
        case_id: str,
        thread_id: str,
        *,
        legacy_owner_token: str,
        checkpoint_case_id: str,
        checkpoint_thread_id: str,
        checkpoint_id: str,
    ) -> None:
        self.__store.validate_or_claim_workflow_identity(
            case_id,
            thread_id,
            self.__owner_token,
            legacy_owner_token=legacy_owner_token,
            checkpoint_case_id=checkpoint_case_id,
            checkpoint_thread_id=checkpoint_thread_id,
            checkpoint_id=checkpoint_id,
        )

    def claim_workflow_mutation(
        self,
        case_id: str,
        thread_id: str,
        expected_checkpoint_head: str,
        attempt_token: str,
        request_digest: str,
        *,
        execution_id: str | None = None,
        execution_request_digest: str | None = None,
        lease_seconds: float = WORKFLOW_MUTATION_LEASE_SECONDS,
    ) -> None:
        self.__store.claim_workflow_mutation(
            case_id,
            thread_id,
            self.__owner_token,
            expected_checkpoint_head,
            attempt_token,
            request_digest,
            execution_id=execution_id,
            execution_request_digest=execution_request_digest,
            lease_seconds=lease_seconds,
        )

    def issue_workflow_execution_grant(self, **kwargs: Any) -> str:
        return self.__store.issue_workflow_execution_grant(
            owner_token=self.__owner_token,
            **kwargs,
        )

    def advance_workflow_mutation(
        self,
        case_id: str,
        thread_id: str,
        expected_checkpoint_head: str,
        new_checkpoint_head: str,
        attempt_token: str,
        request_digest: str,
    ) -> None:
        self.__store.advance_workflow_mutation(
            case_id,
            thread_id,
            self.__owner_token,
            expected_checkpoint_head,
            new_checkpoint_head,
            attempt_token,
            request_digest,
        )

    def release_workflow_mutation(
        self,
        case_id: str,
        thread_id: str,
        expected_checkpoint_head: str,
        attempt_token: str,
        request_digest: str,
    ) -> None:
        self.__store.release_workflow_mutation(
            case_id,
            thread_id,
            self.__owner_token,
            expected_checkpoint_head,
            attempt_token,
            request_digest,
        )

    def recover_workflow_mutation(
        self,
        case_id: str,
        thread_id: str,
        expected_checkpoint_head: str,
        recovered_checkpoint_head: str,
        attempt_token: str,
        request_digest: str,
    ) -> None:
        self.__store.recover_workflow_mutation(
            case_id,
            thread_id,
            self.__owner_token,
            expected_checkpoint_head,
            recovered_checkpoint_head,
            attempt_token,
            request_digest,
        )

    def release_workflow_identity(self, case_id: str, thread_id: str) -> None:
        self.__store.release_workflow_identity(case_id, thread_id, self.__owner_token)


def _workflow_authorization_broker(
    service: OperationsService,
    owner_token: str,
) -> _WorkflowAuthorizationBroker:
    """Create the private broker used only by runtime/checkpoint coordination."""

    return _WorkflowAuthorizationBroker(service, owner_token)
