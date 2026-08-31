"""Public durable runtime boundary for the RecallOps LangGraph."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

from recallops.agents.policies import strict_json_value
from recallops.agents.workflow import FailureController, build_workflow
from recallops.mcp.gateway import DirectGateway
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


class RecallOpsRuntime:
    """Own the SQLite checkpointer and trusted read/write service closures."""

    def __init__(
        self,
        *,
        graph: Any,
        failures: FailureController,
        checkpointer: AsyncSqliteSaver,
    ) -> None:
        self.graph = graph
        self._failures = failures
        self._checkpointer = checkpointer

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        *,
        checkpoint_path: Path | str,
        operations_path: Path | str,
    ) -> AsyncIterator[RecallOpsRuntime]:
        """Open both durable stores and close the async checkpointer explicitly."""
        checkpoint = Path(checkpoint_path).expanduser().resolve()
        operations = Path(operations_path).expanduser().resolve()
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        operations.parent.mkdir(parents=True, exist_ok=True)
        traceability = TraceabilityService()
        gateway = DirectGateway(
            registry=RecallRegistryService(),
            traceability=traceability,
            operations=OperationsService(
                storage_path=operations,
                traceability=traceability,
            ),
        )
        failures = FailureController()
        retriever = AgenticRetriever(ClosedRetrievalGateway.direct())
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
            await saver.setup()
            graph = build_workflow(
                gateway=gateway,
                retriever=retriever,
                failures=failures,
                checkpointer=saver,
            )
            yield cls(graph=graph, failures=failures, checkpointer=saver)

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must be nonblank")
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _checkpoint_id(snapshot: Any) -> str | None:
        config = snapshot.config or {}
        configurable = config.get("configurable", {})
        value = configurable.get("checkpoint_id")
        return value if isinstance(value, str) else None

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
        if not recall_number.strip() or not question.strip():
            raise ValueError("recall_number and question must be nonblank")
        generated_case = case_id or f"CASE-{uuid5(NAMESPACE_URL, recall_number + ':' + question)}"
        generated_thread = thread_id or generated_case
        config = self._config(generated_thread)
        existing = await self.graph.aget_state(config)
        if self._checkpoint_id(existing) is not None:
            raise ValueError(f"thread {generated_thread!r} already has a durable checkpoint")
        scope = list(scope_lot_ids or [])
        if any(not isinstance(item, str) or not item.strip() for item in scope):
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
        await self.graph.ainvoke(
            initial,
            config,
            version="v2",
            stream_mode="values",
            durability="sync",
        )
        return self._result(await self.graph.aget_state(config))

    @staticmethod
    def _require_equal(response: Mapping[str, Any], pending: Mapping[str, Any], key: str) -> None:
        if key not in response or response[key] != pending.get(key):
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
            if normalized.get("action_id") != expected_action_id:
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
        before = await self.graph.aget_state(config)
        if self._checkpoint_id(before) is None:
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
