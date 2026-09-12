"""Read-only live investigation with an observable-only, credential-free result."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from time import perf_counter
from typing import Any, Literal
from uuid import UUID

from langchain.agents.structured_output import StructuredOutputError
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import get_debug, get_verbose, set_debug, set_verbose
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from recallops.agents.deep_supervisor import (
    READ_TOOL_OBSERVATIONS,
    ReadToolObservation,
    SupervisorResponse,
    build_deep_supervisor,
    specialist_catalog,
)
from recallops.agents.specialists import (
    ContainmentProposal,
    ProductLotAssessment,
    RecallIntelligence,
    TraceabilityAssessment,
)
from recallops.llm import LLMSettings, build_chat_model, sanitize_llm_error
from recallops.mcp.gateway import DirectGateway, StdioMCPGateway

_SPECIALIST_RESPONSES = {
    "recall-intelligence": RecallIntelligence,
    "product-lot-matching": ProductLotAssessment,
    "traceability-reconciliation": TraceabilityAssessment,
    "containment-communications": ContainmentProposal,
}

_CONSOLE_LOCK = Lock()
_CONSOLE_USERS = 0
_CONSOLE_SETTINGS = (False, False)


@contextmanager
def _suppress_console_tracing() -> Iterator[None]:
    """Keep process-global console tracing off until all overlapping live calls exit.

    The lock protects only entry/exit bookkeeping, never an await. This works across
    event loops and threads without serializing model calls. Other LangChain work
    temporarily shares the suppressed console settings because the SDK flags are global.
    """
    global _CONSOLE_USERS, _CONSOLE_SETTINGS
    with _CONSOLE_LOCK:
        if _CONSOLE_USERS == 0:
            _CONSOLE_SETTINGS = get_debug(), get_verbose()
        set_debug(False)
        set_verbose(False)
        _CONSOLE_USERS += 1
    try:
        yield
    finally:
        with _CONSOLE_LOCK:
            _CONSOLE_USERS -= 1
            if _CONSOLE_USERS == 0:
                set_debug(_CONSOLE_SETTINGS[0])
                set_verbose(_CONSOLE_SETTINGS[1])


class LiveExecutionEvent(BaseModel):
    """A name and measured outcome, never arguments, messages, or provider metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["model", "tool"]
    name: str
    status: Literal["completed", "failed"]
    duration_ms: float = Field(ge=0)


class LiveReasoningSummary(BaseModel):
    """Safe to persist alongside a case; model claims are advisory, not verified evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["openai"] = "openai"
    model: str
    status: Literal["completed", "failed"]
    plan: list[str] = Field(default_factory=list)
    specialist_sequence: list[str] = Field(default_factory=list)
    read_tool_sequence: list[str] = Field(default_factory=list)
    response_summary: SupervisorResponse | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: float = Field(ge=0)
    fallback_used: bool = False
    error_category: (
        Literal[
            "authentication",
            "rate_limit",
            "timeout",
            "invalid_response",
            "provider_error",
            "budget_exceeded",
        ]
        | None
    ) = None
    events: list[LiveExecutionEvent] = Field(default_factory=list)


class _SafeCallbacks(BaseCallbackHandler):
    """Project callbacks immediately; do not retain any callback payload or exception."""

    run_inline = True

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.pending: dict[UUID, tuple[float, Literal["model", "tool"], str]] = {}
        self.events: list[tuple[float, LiveExecutionEvent]] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.has_usage = False
        self.plan: list[str] = []
        self.specialists: list[str] = []
        self.roles: dict[UUID, str] = {}
        self.invalid_response = False

    def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self.pending[run_id] = (perf_counter(), "model", self.model_name)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(run_id, "completed")
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if usage:
                    self.has_usage = True
                    for name in self.usage:
                        value = usage.get(name)
                        if type(value) is int and value >= 0:
                            self.usage[name] += value

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(run_id, "failed")

    def on_tool_start(
        self, serialized: Any, input_str: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        name = serialized.get("name") if isinstance(serialized, dict) else None
        if name in {"write_todos", "task", "ls", "read_file"}:
            self.pending[run_id] = (perf_counter(), "tool", name)
            inputs = kwargs.get("inputs")
            role = inputs.get("subagent_type") if isinstance(inputs, dict) else None
            if name == "task" and role in {item.name for item in specialist_catalog()}:
                self.roles[run_id] = role

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        status = (
            "failed"
            if isinstance(output, ToolMessage) and output.status == "error"
            else "completed"
        )
        role = self.roles.get(run_id)
        if role is not None:
            messages = (
                output.update.get("messages", [])
                if isinstance(output, Command) and isinstance(output.update, dict)
                else [output]
            )
            if (
                len(messages) != 1
                or not isinstance(messages[0], ToolMessage)
                or not isinstance(messages[0].content, str)
            ):
                status = "failed"
            else:
                try:
                    _SPECIALIST_RESPONSES[role].model_validate_json(messages[0].content)
                except ValidationError:
                    status = "failed"
            if status == "failed":
                self.invalid_response = True
        self._finish(run_id, status)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(run_id, "failed")

    def _finish(self, run_id: UUID, status: Literal["completed", "failed"]) -> None:
        pending = self.pending.pop(run_id, None)
        role = self.roles.pop(run_id, None)
        if pending is not None:
            started_at, kind, name = pending
            if status == "completed" and kind == "tool":
                if name == "write_todos":
                    self.plan = [item.name for item in specialist_catalog()]
                elif name == "task" and role is not None:
                    self.specialists.append(role)
            self.events.append(
                (
                    started_at,
                    LiveExecutionEvent(
                        kind=kind,
                        name=name,
                        status=status,
                        duration_ms=(perf_counter() - started_at) * 1000,
                    ),
                )
            )


class LiveReasoningService:
    """Retain only non-secret settings; each call creates and discards its model and graph."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    async def run(
        self, question: str, *, transport: Literal["direct", "stdio"]
    ) -> LiveReasoningSummary:
        started_at = perf_counter()
        callbacks = _SafeCallbacks(self.settings.model)
        reads: list[ReadToolObservation] = []
        token = READ_TOOL_OBSERVATIONS.set(reads)
        plan: list[str] = []
        specialists: list[str] = []
        response = None
        error_category = None
        try:
            if (
                not isinstance(question, str)
                or not question.strip()
                or transport not in {"direct", "stdio"}
            ):
                raise ValueError("Invalid live investigation request")
            # Cover construction too: compiled graphs capture the global debug setting.
            with _suppress_console_tracing(), tracing_context(enabled=False):
                model = build_chat_model(self.settings).model_copy(
                    update={
                        "cache": False,
                        "callbacks": None,
                        "verbose": False,
                    }
                )
                gateway = (
                    DirectGateway()
                    if transport == "direct"
                    else StdioMCPGateway(
                        {
                            name: {
                                "transport": "stdio",
                                "command": sys.executable,
                                "args": ["-m", module],
                            }
                            for name, module in (
                                ("registry", "recallops.mcp.recall_registry_server"),
                                ("traceability", "recallops.mcp.traceability_server"),
                            )
                        }
                    )
                )
                supervisor = build_deep_supervisor(model=model, read_gateway=gateway)
                result = await supervisor.graph.ainvoke(
                    {"messages": [HumanMessage(content=question)]},
                    config={"callbacks": [callbacks], "recursion_limit": 100},
                )
            fixed = [item.name for item in specialist_catalog()]
            if callbacks.invalid_response:
                raise ValueError("Invalid specialist response")
            # Validate successful tool results rather than equating requested tasks with completion.
            calls: dict[str, tuple[str, Any]] = {}
            for message in result.get("messages", []):
                if isinstance(message, AIMessage):
                    for call in message.tool_calls:
                        calls[call["id"]] = (call["name"], call["args"])
                elif isinstance(message, ToolMessage):
                    name, args = calls.pop(message.tool_call_id, ("", {}))
                    if message.status == "error":
                        raise ValueError("Live tool failed")
                    if name == "write_todos":
                        todos = args.get("todos", [])
                        if len(todos) != 4 or any(
                            not item.get("content", "").startswith(f"[{role}]")
                            for item, role in zip(todos, fixed, strict=True)
                        ):
                            raise ValueError("Invalid live plan")
                        plan = fixed.copy()
                    elif name == "task":
                        role = args.get("subagent_type")
                        if role not in fixed:
                            raise ValueError("Unknown specialist")
                        specialists.append(role)
            if plan != fixed or specialists != fixed or len(set(specialists)) != 4:
                raise ValueError("Incomplete live investigation")
            response = SupervisorResponse.model_validate(result.get("structured_response"))
            if any(item.status == "failed" for item in reads):
                raise ValueError("Live read failed")
        except Exception as error:
            response = None
            plan = callbacks.plan
            specialists = callbacks.specialists
            if isinstance(error, GraphRecursionError):
                error_category = "budget_exceeded"
            elif isinstance(error, (ValueError, ValidationError, StructuredOutputError)):
                error_category = "invalid_response"
            else:
                error_category, _ = sanitize_llm_error(error)
        finally:
            READ_TOOL_OBSERVATIONS.reset(token)
        observations = callbacks.events + [
            (
                item.started_at,
                LiveExecutionEvent(
                    kind="tool",
                    name=item.name,
                    status=item.status,
                    duration_ms=item.duration_ms,
                ),
            )
            for item in reads
        ]
        return LiveReasoningSummary(
            model=self.settings.model,
            status="failed" if error_category else "completed",
            plan=plan,
            specialist_sequence=specialists,
            read_tool_sequence=[
                item.name for item in sorted(reads, key=lambda item: item.started_at)
            ],
            response_summary=response,
            **{
                name: value if callbacks.has_usage else None
                for name, value in callbacks.usage.items()
            },
            duration_ms=(perf_counter() - started_at) * 1000,
            error_category=error_category,
            events=[event for _, event in sorted(observations, key=lambda item: item[0])],
        )
