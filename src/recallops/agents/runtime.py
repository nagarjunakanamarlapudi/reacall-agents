"""Public durable runtime boundary for the RecallOps LangGraph."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import sys
import weakref
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, field_serializer

from recallops.agents.policies import strict_json_value
from recallops.agents.workflow import (
    FailureController,
    ReadOnlyWorkflow,
    _execute_workflow,
    build_workflow,
)
from recallops.mcp.gateway import DirectGateway, StdioMCPGateway
from recallops.paths import DATA_DIR, PROJECT_ROOT
from recallops.retrieval.agentic import AgenticRetriever, ClosedRetrievalGateway
from recallops.services.operations import (
    INITIAL_CHECKPOINT_HEAD,
    OperationsService,
    _workflow_authorization_broker,
    _WorkflowAuthorizationBroker,
)
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService


class FrozenSequence(tuple[Any, ...]):
    """Tuple-backed JSON sequence with ergonomic equality against other sequences."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return False

    __hash__ = tuple.__hash__

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        return [_thaw_json(item, memo=memo) for item in self]


class FrozenDict(Mapping[str, Any]):
    """JSON-compatible mapping that rejects every normal mutation surface."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_data", MappingProxyType(dict(value)))

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("RuntimeResult mappings are immutable")

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(dict(self._data))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return False

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        return {key: _thaw_json(value, memo=memo) for key, value in self.items()}


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return FrozenSequence(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any, *, memo: dict[int, Any] | None = None) -> Any:
    memo = memo or {}
    if isinstance(value, Mapping):
        return {key: _thaw_json(item, memo=memo) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item, memo=memo) for item in value]
    return copy.deepcopy(value, memo)


class RuntimeResult(BaseModel):
    """Detached JSON view of a durable workflow checkpoint."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    case: FrozenDict
    pending_interrupt: FrozenDict | None
    next_nodes: tuple[str, ...]
    checkpoint_id: str | None

    @field_serializer("case", "pending_interrupt")
    def serialize_frozen_mapping(self, value: FrozenDict | None) -> Any:
        return None if value is None else _thaw_json(value)


_RUNTIME_WORKFLOWS: weakref.WeakKeyDictionary[Any, ReadOnlyWorkflow] = weakref.WeakKeyDictionary()
_RUNTIME_AUTHORIZERS: weakref.WeakKeyDictionary[Any, _WorkflowAuthorizationBroker] = (
    weakref.WeakKeyDictionary()
)


def _checkpoint_store_owner(path: Path) -> str:
    """Return the random UUID durably embedded in this checkpoint SQLite database."""
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS recallops_runtime_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS recallops_mutation_attempts "
            "(case_id TEXT NOT NULL, thread_id TEXT NOT NULL, attempt_token TEXT NOT NULL, "
            "expected_checkpoint_head TEXT NOT NULL, request_digest TEXT NOT NULL, "
            "state TEXT NOT NULL, "
            "PRIMARY KEY (case_id, thread_id))"
        )
        attempt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(recallops_mutation_attempts)")
        }
        if "request_digest" not in attempt_columns:
            connection.execute(
                "ALTER TABLE recallops_mutation_attempts ADD COLUMN request_digest TEXT"
            )
        row = connection.execute(
            "SELECT value FROM recallops_runtime_metadata WHERE key='checkpoint_store_id'"
        ).fetchone()
        if row is None:
            owner_token = str(uuid4())
            connection.execute(
                "INSERT INTO recallops_runtime_metadata (key, value) VALUES (?, ?)",
                ("checkpoint_store_id", owner_token),
            )
        else:
            owner_token = row[0]
            parsed = UUID(owner_token)
            if parsed.version != 4 or str(parsed) != owner_token:
                raise ValueError("checkpoint store identity must be a canonical random UUID")
        connection.execute("COMMIT")
        return owner_token
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _load_checkpoint_attempt(
    path: Path,
    case_id: str,
    thread_id: str,
) -> tuple[str, str, str] | None:
    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        row = connection.execute(
            "SELECT attempt_token, expected_checkpoint_head, request_digest "
            "FROM recallops_mutation_attempts "
            "WHERE case_id=? AND thread_id=?",
            (case_id, thread_id),
        ).fetchone()
    if row is None:
        return None
    if row[2] is None:
        raise ValueError("legacy checkpoint mutation marker lacks a bound request digest")
    _validate_request_digest(str(row[2]))
    return str(row[0]), str(row[1]), str(row[2])


def _validate_request_digest(request_digest: str) -> None:
    if (
        type(request_digest) is not str
        or len(request_digest) != 64
        or any(character not in "0123456789abcdef" for character in request_digest)
    ):
        raise ValueError("request digest must be a canonical SHA-256 hex digest")


def _mutation_request_digest(
    *,
    case_id: str,
    thread_id: str,
    expected_checkpoint_head: str,
    interrupt_kind: str,
    normalized_response: Any = None,
    pending: Mapping[str, Any] | None = None,
    initial_payload: Mapping[str, Any] | None = None,
) -> str:
    """Bind one exact start or human command to its durable fencing attempt."""
    normalized_pending = pending or {}
    contract = strict_json_value(
        {
            "schema": "recallops-mutation-request-v1",
            "case_id": case_id,
            "thread_id": thread_id,
            "expected_checkpoint_head": expected_checkpoint_head,
            "interrupt_kind": interrupt_kind,
            "human_response": normalized_response,
            "pending_action_id": normalized_pending.get("action_id")
            or normalized_pending.get("action", {}).get("action_id"),
            "pending_action_digest": normalized_pending.get("action_digest"),
            "execution_id": normalized_pending.get("execution_id"),
            "idempotency_key": normalized_pending.get("idempotency_key"),
            "initial_payload": initial_payload,
        }
    )
    encoded = json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _execution_attempt_binding(pending: Mapping[str, Any]) -> tuple[str | None, str | None]:
    if pending.get("kind") not in {"execution_confirmation", "write_outcome_recovery"}:
        return None, None
    execution_id = pending.get("execution_id")
    if type(execution_id) is not str or not execution_id.strip():
        raise ValueError("execution interrupt requires a nonblank execution_id")
    encoded = json.dumps(
        strict_json_value(pending),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return execution_id, hashlib.sha256(encoded).hexdigest()


def _prepare_checkpoint_attempt(
    path: Path,
    case_id: str,
    thread_id: str,
    expected_checkpoint_head: str,
    request_digest: str,
) -> str:
    _validate_request_digest(request_digest)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT attempt_token, expected_checkpoint_head, request_digest "
            "FROM recallops_mutation_attempts "
            "WHERE case_id=? AND thread_id=?",
            (case_id, thread_id),
        ).fetchone()
        if row is not None:
            if row[1] != expected_checkpoint_head:
                raise RuntimeError("checkpoint mutation marker belongs to another head")
            if row[2] != request_digest:
                raise ValueError("checkpoint mutation marker request digest does not match")
            attempt_token = str(row[0])
        else:
            attempt_token = str(uuid4())
            connection.execute(
                "INSERT INTO recallops_mutation_attempts "
                "(case_id, thread_id, attempt_token, expected_checkpoint_head, "
                "request_digest, state) VALUES (?, ?, ?, ?, ?, 'prepared')",
                (case_id, thread_id, attempt_token, expected_checkpoint_head, request_digest),
            )
        connection.execute("COMMIT")
        return attempt_token
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _clear_checkpoint_attempt(
    path: Path,
    case_id: str,
    thread_id: str,
    attempt_token: str,
    request_digest: str,
) -> None:
    _validate_request_digest(request_digest)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM recallops_mutation_attempts "
            "WHERE case_id=? AND thread_id=? AND attempt_token=? AND request_digest=?",
            (case_id, thread_id, attempt_token, request_digest),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


class _LockEntry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class RecallOpsRuntime:
    """Own the SQLite checkpointer and trusted read/write service closures."""

    _thread_locks: ClassVar[dict[tuple[int, str, str, str], _LockEntry]] = {}

    def __init__(
        self,
        *,
        workflow: ReadOnlyWorkflow,
        failures: FailureController,
        checkpointer: AsyncSqliteSaver,
        checkpoint_key: str,
        authorization_broker: _WorkflowAuthorizationBroker,
    ) -> None:
        _RUNTIME_WORKFLOWS[self] = workflow
        _RUNTIME_AUTHORIZERS[self] = authorization_broker
        self._failures = failures
        self._checkpointer = checkpointer
        self._checkpoint_key = checkpoint_key

    @classmethod
    def _active_lock_count(cls) -> int:
        return len(cls._thread_locks)

    @asynccontextmanager
    async def _thread_lock(
        self,
        thread_id: str,
        *,
        namespace: str = "thread",
    ) -> AsyncIterator[None]:
        key = (id(asyncio.get_running_loop()), self._checkpoint_key, namespace, thread_id)
        entry = self._thread_locks.setdefault(key, _LockEntry())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._thread_locks.get(key) is entry:
                self._thread_locks.pop(key)

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        *,
        checkpoint_path: Path | str,
        operations_path: Path | str,
        transport: Literal["direct", "stdio"] = "direct",
    ) -> AsyncIterator[RecallOpsRuntime]:
        """Open both durable stores and close the async checkpointer explicitly."""
        if transport not in {"direct", "stdio"}:
            raise ValueError("transport must be 'direct' or 'stdio'")
        checkpoint = Path(checkpoint_path).expanduser().resolve()
        operations = Path(operations_path).expanduser().resolve()
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        operations.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_owner_token = _checkpoint_store_owner(checkpoint)
        traceability = TraceabilityService(
            data_dir=DATA_DIR,
            source_mode="snapshot",
        )
        operations_service = OperationsService(
            storage_path=operations,
            traceability=traceability,
        )
        authorization_broker = _workflow_authorization_broker(
            operations_service,
            checkpoint_owner_token,
        )
        if transport == "direct":
            gateway = DirectGateway(
                registry=RecallRegistryService(
                    data_dir=DATA_DIR,
                    source_mode="snapshot",
                ),
                traceability=traceability,
                operations=operations_service,
            )
            retrieval_gateway = ClosedRetrievalGateway.direct()
        else:
            environment = {
                **os.environ,
                "RECALLOPS_DATA_DIR": str(DATA_DIR),
                "RECALLOPS_SOURCE_MODE": "snapshot",
                "RECALLOPS_OPERATIONS_DB": str(operations),
            }

            def connection(module: str) -> dict[str, object]:
                return {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", module],
                    "cwd": str(PROJECT_ROOT),
                    "env": environment,
                }

            gateway = StdioMCPGateway(
                {
                    "registry": connection("recallops.mcp.recall_registry_server"),
                    "traceability": connection("recallops.mcp.traceability_server"),
                    "operations": connection("recallops.mcp.operations_server"),
                }
            )
            retrieval_gateway = ClosedRetrievalGateway.stdio()
        failures = FailureController()
        retriever = AgenticRetriever(retrieval_gateway)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
            await saver.setup()
            graph = build_workflow(
                gateway=gateway,
                retriever=retriever,
                failures=failures,
                operations_service=operations_service,
                authorization_broker=authorization_broker,
                checkpointer=saver,
            )
            yield cls(
                workflow=graph,
                failures=failures,
                checkpointer=saver,
                checkpoint_key=str(checkpoint),
                authorization_broker=authorization_broker,
            )

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        if type(thread_id) is not str or not thread_id.strip():
            raise ValueError("thread_id must be nonblank")
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _checkpoint_id(snapshot: Any) -> str | None:
        config = snapshot.config or {}
        configurable = config.get("configurable", {})
        value = configurable.get("checkpoint_id")
        return value if isinstance(value, str) else None

    async def _validate_checkpoint_identity(self, case_id: str, thread_id: str) -> None:
        async for item in self._checkpointer.alist(None):
            values = item.checkpoint.get("channel_values", {})
            stored_case = values.get("case_id")
            stored_thread = values.get("thread_id")
            if type(stored_case) is not str or type(stored_thread) is not str:
                continue
            if stored_case == case_id and stored_thread != thread_id:
                raise ValueError(
                    f"case_id {case_id!r} is already bound to thread {stored_thread!r}"
                )
            if stored_thread == thread_id and stored_case != case_id:
                raise ValueError(
                    f"thread_id {thread_id!r} is already bound to case {stored_case!r}"
                )

    @staticmethod
    def _pending(snapshot: Any) -> FrozenDict | None:
        interruptions = tuple(snapshot.interrupts or ())
        if not interruptions:
            return None
        if len(interruptions) != 1:
            raise RuntimeError("RecallOps expects exactly one pending human interrupt")
        value = interruptions[0].value
        normalized = strict_json_value(value)
        if not isinstance(normalized, dict):
            raise RuntimeError("pending interrupt payload must be a JSON object")
        return _freeze_json(normalized)

    @classmethod
    def _result(cls, snapshot: Any) -> RuntimeResult:
        case = strict_json_value(snapshot.values or {})
        if not isinstance(case, dict):
            raise RuntimeError("workflow checkpoint values must be a JSON object")
        return RuntimeResult(
            case=_freeze_json(case),
            pending_interrupt=cls._pending(snapshot),
            next_nodes=tuple(snapshot.next or ()),
            checkpoint_id=cls._checkpoint_id(snapshot),
        )

    async def start_case(
        self,
        *,
        recall_number: str,
        question: str,
        case_id: str | None = None,
        thread_id: str | None = None,
        scope_lot_ids: list[str] | tuple[str, ...] | None = None,
    ) -> RuntimeResult:
        if type(recall_number) is not str:
            raise TypeError("recall_number must be an exact string")
        if type(question) is not str:
            raise TypeError("question must be an exact string")
        if not recall_number.strip():
            raise ValueError("recall_number must be nonblank")
        if not question.strip():
            raise ValueError("question must be nonblank")
        if case_id is not None and type(case_id) is not str:
            raise TypeError("case_id must be an exact string")
        if case_id is not None and not case_id.strip():
            raise ValueError("case_id must be a nonblank string")
        if thread_id is not None and type(thread_id) is not str:
            raise TypeError("thread_id must be an exact string")
        if thread_id is not None and not thread_id.strip():
            raise ValueError("thread_id must be a nonblank string")
        generated_case = (
            case_id or thread_id or f"CASE-{uuid5(NAMESPACE_URL, recall_number + ':' + question)}"
        )
        generated_thread = thread_id or generated_case
        config = self._config(generated_thread)
        if scope_lot_ids is None:
            scope = []
        elif type(scope_lot_ids) not in {list, tuple}:
            raise TypeError("scope_lot_ids must be a list or tuple of exact strings")
        else:
            scope = list(scope_lot_ids)
        if any(type(item) is not str or not item.strip() for item in scope):
            raise ValueError("scope_lot_ids must contain nonblank strings")
        if len(scope) != len(set(scope)):
            raise ValueError("scope_lot_ids must be unique")
        initial = {
            "case_id": generated_case,
            "thread_id": generated_thread,
            "recall_number": recall_number,
            "question": question,
            "scope_lot_ids": scope,
        }
        request_digest = _mutation_request_digest(
            case_id=generated_case,
            thread_id=generated_thread,
            expected_checkpoint_head=INITIAL_CHECKPOINT_HEAD,
            interrupt_kind="start",
            initial_payload=initial,
        )
        async with self._thread_lock("start", namespace="identity"):
            async with self._thread_lock(generated_thread):
                workflow = _RUNTIME_WORKFLOWS[self]
                existing = await workflow.aget_state(config)
                if self._checkpoint_id(existing) is not None:
                    raise ValueError(
                        f"thread {generated_thread!r} already has a durable checkpoint"
                    )
                await self._validate_checkpoint_identity(generated_case, generated_thread)
                authorization = _RUNTIME_AUTHORIZERS[self]
                reserved = authorization.reserve_workflow_identity(
                    generated_case,
                    generated_thread,
                )
                checkpoint_path = Path(self._checkpoint_key)
                attempt_token = _prepare_checkpoint_attempt(
                    checkpoint_path,
                    generated_case,
                    generated_thread,
                    INITIAL_CHECKPOINT_HEAD,
                    request_digest,
                )
                claimed = False
                try:
                    authorization.claim_workflow_mutation(
                        generated_case,
                        generated_thread,
                        INITIAL_CHECKPOINT_HEAD,
                        attempt_token,
                        request_digest,
                    )
                    claimed = True
                    await _execute_workflow(workflow, initial, config)
                    created = await workflow.aget_state(config)
                    created_checkpoint_id = self._checkpoint_id(created)
                    if created_checkpoint_id is None:
                        raise RuntimeError("workflow start completed without a durable checkpoint")
                    authorization.advance_workflow_mutation(
                        generated_case,
                        generated_thread,
                        INITIAL_CHECKPOINT_HEAD,
                        created_checkpoint_id,
                        attempt_token,
                        request_digest,
                    )
                    _clear_checkpoint_attempt(
                        checkpoint_path,
                        generated_case,
                        generated_thread,
                        attempt_token,
                        request_digest,
                    )
                except BaseException:
                    created = await workflow.aget_state(config)
                    created_checkpoint_id = self._checkpoint_id(created)
                    if claimed:
                        if created_checkpoint_id is None:
                            authorization.release_workflow_mutation(
                                generated_case,
                                generated_thread,
                                INITIAL_CHECKPOINT_HEAD,
                                attempt_token,
                                request_digest,
                            )
                        else:
                            authorization.advance_workflow_mutation(
                                generated_case,
                                generated_thread,
                                INITIAL_CHECKPOINT_HEAD,
                                created_checkpoint_id,
                                attempt_token,
                                request_digest,
                            )
                        _clear_checkpoint_attempt(
                            checkpoint_path,
                            generated_case,
                            generated_thread,
                            attempt_token,
                            request_digest,
                        )
                    if reserved and created_checkpoint_id is None:
                        authorization.release_workflow_identity(
                            generated_case,
                            generated_thread,
                        )
                    raise
                return self._result(created)

    @staticmethod
    def _require_equal(response: Mapping[str, Any], pending: Mapping[str, Any], key: str) -> None:
        expected = pending.get(key)
        if (
            key not in response
            or type(response[key]) is not type(expected)
            or response[key] != expected
        ):
            raise ValueError(f"resume {key} does not match the pending interrupt")

    @classmethod
    def _validate_resume_binding(
        cls, *, thread_id: str, response: Any, pending: Mapping[str, Any]
    ) -> dict[str, Any]:
        normalized = strict_json_value(response)
        if not isinstance(normalized, dict):
            raise TypeError("resume response must be a JSON object")
        for key in ("kind", "case_id", "thread_id", "case_version"):
            cls._require_equal(normalized, pending, key)
        if normalized["thread_id"] != thread_id:
            raise ValueError("resume thread_id does not match the requested thread")
        kind = pending["kind"]
        if kind in {"action_review", "closure_review"}:
            expected_action_id = pending["action"]["action_id"]
            if (
                type(normalized.get("action_id")) is not str
                or normalized.get("action_id") != expected_action_id
            ):
                raise ValueError("resume action_id does not match the pending interrupt")
            cls._require_equal(normalized, pending, "action_digest")
        elif kind in {"execution_confirmation", "write_outcome_recovery"}:
            for key in ("action_id", "action_digest", "execution_id", "idempotency_key"):
                cls._require_equal(normalized, pending, key)
        else:
            raise ValueError(f"unknown pending interrupt kind {kind!r}")
        return normalized

    async def resume_case(self, *, thread_id: str, response: Any) -> RuntimeResult:
        config = self._config(thread_id)
        async with self._thread_lock(thread_id):
            workflow = _RUNTIME_WORKFLOWS[self]
            before = await workflow.aget_state(config)
            before_checkpoint_id = self._checkpoint_id(before)
            if before_checkpoint_id is None:
                raise KeyError(f"unknown thread {thread_id!r}")
            pending = self._pending(before)
            if pending is None:
                raise ValueError(f"thread {thread_id!r} has no pending interrupt")
            checkpoint_case_id = before.values.get("case_id")
            checkpoint_thread_id = before.values.get("thread_id")
            authorization = _RUNTIME_AUTHORIZERS[self]
            authorization.validate_or_claim_workflow_identity(
                checkpoint_case_id,
                thread_id,
                legacy_owner_token=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"recallops-checkpoint-owner:{self._checkpoint_key}",
                    )
                ),
                checkpoint_case_id=checkpoint_case_id,
                checkpoint_thread_id=checkpoint_thread_id,
                checkpoint_id=before_checkpoint_id,
            )
            normalized = self._validate_resume_binding(
                thread_id=thread_id,
                response=response,
                pending=pending,
            )
            request_digest = _mutation_request_digest(
                case_id=checkpoint_case_id,
                thread_id=thread_id,
                expected_checkpoint_head=before_checkpoint_id,
                interrupt_kind=pending["kind"],
                normalized_response=normalized,
                pending=pending,
            )
            checkpoint_path = Path(self._checkpoint_key)
            marker = _load_checkpoint_attempt(
                checkpoint_path,
                checkpoint_case_id,
                thread_id,
            )
            if marker is not None and marker[1] != before_checkpoint_id:
                authorization.recover_workflow_mutation(
                    checkpoint_case_id,
                    thread_id,
                    marker[1],
                    before_checkpoint_id,
                    marker[0],
                    marker[2],
                )
                _clear_checkpoint_attempt(
                    checkpoint_path,
                    checkpoint_case_id,
                    thread_id,
                    marker[0],
                    marker[2],
                )
            attempt_token = _prepare_checkpoint_attempt(
                checkpoint_path,
                checkpoint_case_id,
                thread_id,
                before_checkpoint_id,
                request_digest,
            )
            claimed = False
            try:
                execution_id, execution_request_digest = _execution_attempt_binding(pending)
                authorization.claim_workflow_mutation(
                    checkpoint_case_id,
                    thread_id,
                    before_checkpoint_id,
                    attempt_token,
                    request_digest,
                    execution_id=execution_id,
                    execution_request_digest=execution_request_digest,
                )
                claimed = True
                if self._failures.consume("stale_decision_version"):
                    raise ValueError("injected stale decision/version rejected before resume")
                if self._failures.consume("changed_action_digest"):
                    raise ValueError("injected changed action digest rejected before resume")
                current = await workflow.aget_state(config)
                if self._checkpoint_id(current) != before_checkpoint_id:
                    raise RuntimeError("checkpoint changed during resume binding validation")
                await _execute_workflow(workflow, Command(resume=normalized), config)
                after = await workflow.aget_state(config)
                after_checkpoint_id = self._checkpoint_id(after)
                if after_checkpoint_id is None:
                    raise RuntimeError("workflow resume completed without a durable checkpoint")
                authorization.advance_workflow_mutation(
                    checkpoint_case_id,
                    thread_id,
                    before_checkpoint_id,
                    after_checkpoint_id,
                    attempt_token,
                    request_digest,
                )
                _clear_checkpoint_attempt(
                    checkpoint_path,
                    checkpoint_case_id,
                    thread_id,
                    attempt_token,
                    request_digest,
                )
                return self._result(after)
            except BaseException:
                if claimed:
                    after = await workflow.aget_state(config)
                    after_checkpoint_id = self._checkpoint_id(after)
                    if (
                        after_checkpoint_id is not None
                        and after_checkpoint_id != before_checkpoint_id
                    ):
                        authorization.advance_workflow_mutation(
                            checkpoint_case_id,
                            thread_id,
                            before_checkpoint_id,
                            after_checkpoint_id,
                            attempt_token,
                            request_digest,
                        )
                    else:
                        authorization.release_workflow_mutation(
                            checkpoint_case_id,
                            thread_id,
                            before_checkpoint_id,
                            attempt_token,
                            request_digest,
                        )
                    _clear_checkpoint_attempt(
                        checkpoint_path,
                        checkpoint_case_id,
                        thread_id,
                        attempt_token,
                        request_digest,
                    )
                raise

    async def get_case(self, *, thread_id: str) -> RuntimeResult | None:
        snapshot = await _RUNTIME_WORKFLOWS[self].aget_state(self._config(thread_id))
        if self._checkpoint_id(snapshot) is None:
            return None
        return self._result(snapshot)

    async def get_case_history(self, *, thread_id: str) -> tuple[RuntimeResult, ...]:
        config = self._config(thread_id)
        snapshots = [
            self._result(snapshot)
            async for snapshot in _RUNTIME_WORKFLOWS[self].aget_state_history(config)
        ]
        return tuple(snapshots)

    def inject_failure(self, scenario: str, *, times: int = 1) -> None:
        self._failures.inject(scenario, times)
