"""Public durable runtime boundary for the RecallOps LangGraph."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar, Literal
from uuid import NAMESPACE_URL, uuid5

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

from recallops.agents.policies import strict_json_value
from recallops.agents.workflow import FailureController, build_workflow
from recallops.mcp.gateway import DirectGateway, StdioMCPGateway
from recallops.paths import DATA_DIR, PROJECT_ROOT
from recallops.retrieval.agentic import AgenticRetriever, ClosedRetrievalGateway
from recallops.services.operations import OperationsService
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService


class RuntimeResult(BaseModel):
    """Detached JSON view of a durable workflow checkpoint."""

    model_config = ConfigDict(frozen=True)

    case: dict[str, Any]
    pending_interrupt: dict[str, Any] | None
    next_nodes: tuple[str, ...]
    checkpoint_id: str | None


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
        graph: Any,
        failures: FailureController,
        checkpointer: AsyncSqliteSaver,
        checkpoint_key: str,
        operations_service: OperationsService,
    ) -> None:
        self.graph = graph
        self._failures = failures
        self._checkpointer = checkpointer
        self._checkpoint_key = checkpoint_key
        self._operations_service = operations_service

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
        traceability = TraceabilityService(
            data_dir=DATA_DIR,
            source_mode="snapshot",
        )
        operations_service = OperationsService(
            storage_path=operations,
            traceability=traceability,
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
                checkpointer=saver,
            )
            yield cls(
                graph=graph,
                failures=failures,
                checkpointer=saver,
                checkpoint_key=str(checkpoint),
                operations_service=operations_service,
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
    def _pending(snapshot: Any) -> dict[str, Any] | None:
        interruptions = tuple(snapshot.interrupts or ())
        if not interruptions:
            return None
        if len(interruptions) != 1:
            raise RuntimeError("RecallOps expects exactly one pending human interrupt")
        value = interruptions[0].value
        normalized = strict_json_value(value)
        if not isinstance(normalized, dict):
            raise RuntimeError("pending interrupt payload must be a JSON object")
        return normalized

    @classmethod
    def _result(cls, snapshot: Any) -> RuntimeResult:
        case = strict_json_value(snapshot.values or {})
        if not isinstance(case, dict):
            raise RuntimeError("workflow checkpoint values must be a JSON object")
        return RuntimeResult(
            case=case,
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
        async with self._thread_lock("start", namespace="identity"):
            async with self._thread_lock(generated_thread):
                existing = await self.graph.aget_state(config)
                if self._checkpoint_id(existing) is not None:
                    raise ValueError(
                        f"thread {generated_thread!r} already has a durable checkpoint"
                    )
                await self._validate_checkpoint_identity(generated_case, generated_thread)
                reserved = self._operations_service.reserve_workflow_identity(
                    generated_case, generated_thread
                )
                try:
                    await self.graph.ainvoke(
                        initial,
                        config,
                        version="v2",
                        stream_mode="values",
                        durability="sync",
                    )
                except BaseException:
                    created = await self.graph.aget_state(config)
                    if reserved and self._checkpoint_id(created) is None:
                        self._operations_service.release_workflow_identity(
                            generated_case, generated_thread
                        )
                    raise
                return self._result(await self.graph.aget_state(config))

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
            before = await self.graph.aget_state(config)
            before_checkpoint_id = self._checkpoint_id(before)
            if before_checkpoint_id is None:
                raise KeyError(f"unknown thread {thread_id!r}")
            pending = self._pending(before)
            if pending is None:
                raise ValueError(f"thread {thread_id!r} has no pending interrupt")
            if self._failures.consume("stale_decision_version"):
                raise ValueError("injected stale decision/version rejected before resume")
            if self._failures.consume("changed_action_digest"):
                raise ValueError("injected changed action digest rejected before resume")
            normalized = self._validate_resume_binding(
                thread_id=thread_id,
                response=response,
                pending=pending,
            )
            current = await self.graph.aget_state(config)
            if self._checkpoint_id(current) != before_checkpoint_id:
                raise RuntimeError("checkpoint changed during resume binding validation")
            await self.graph.ainvoke(
                Command(resume=normalized),
                config,
                version="v2",
                stream_mode="values",
                durability="sync",
            )
            return self._result(await self.graph.aget_state(config))

    async def get_case(self, *, thread_id: str) -> RuntimeResult | None:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        if self._checkpoint_id(snapshot) is None:
            return None
        return self._result(snapshot)

    def inject_failure(self, scenario: str, *, times: int = 1) -> None:
        self._failures.inject(scenario, times)
