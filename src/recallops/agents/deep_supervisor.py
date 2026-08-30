"""Optional live Deep Agents supervisor with fixed, read-only specialist scopes."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, NamedTuple, NotRequired

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.middleware import FilesystemMiddleware
from langchain.agents.middleware import (
    AgentMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.middleware.types import AgentState, PrivateStateAttr
from langchain_core.callbacks import AsyncCallbackManager
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_core.tools.base import _format_output
from pydantic import BaseModel, ConfigDict

from recallops.agents.prompts import (
    CONTAINMENT_COMMUNICATIONS_PROMPT,
    PRODUCT_LOT_MATCHING_PROMPT,
    RECALL_INTELLIGENCE_PROMPT,
    SUPERVISOR_PROMPT,
    TRACEABILITY_RECONCILIATION_PROMPT,
)
from recallops.agents.specialists import (
    ContainmentProposal,
    ProductLotAssessment,
    RecallIntelligence,
    TraceabilityAssessment,
)
from recallops.config import Settings, get_settings
from recallops.data.loaders import load_demo_dataset, load_recall_snapshot
from recallops.mcp.gateway import DirectGateway, StdioMCPGateway
from recallops.models import (
    CandidateProduct,
    InventoryPosition,
    LotMatch,
    ProductMetadata,
    RecallPredicate,
    TraceEvent,
)
from recallops.paths import PROJECT_ROOT
from recallops.services.operations import OperationsService
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService

OPERATIONAL_WRITE_TOOL_NAMES = frozenset(
    {
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    }
)
_FILESYSTEM_WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file", "delete", "execute"})
_PARENT_TOOL_NAMES = frozenset({"ls", "read_file", "task", "write_todos"})
_PARENT_GRAPH_NODES = frozenset(
    {
        "__start__",
        "model",
        "tools",
        "PatchToolCallsMiddleware.before_agent",
        "DelegationGuardMiddleware.after_model",
        "ToolCallLimitMiddleware[task].after_model",
        "TodoListMiddleware.after_model",
    }
)
_SUBAGENT_GRAPH_NODES = frozenset(
    {"__start__", "model", "tools", "PatchToolCallsMiddleware.before_agent"}
)
_STDIO_SERVERS = (
    ("registry", "recallops.mcp.recall_registry_server"),
    ("traceability", "recallops.mcp.traceability_server"),
    ("operations", "recallops.mcp.operations_server"),
)
_STDIO_READ_SERVERS = _STDIO_SERVERS[:2]
_PYTHON_EXECUTABLE = Path(sys.executable)
_REGISTRY_READ_TOOL_NAMES = frozenset({"search_recalls", "get_recall", "get_product_metadata"})
_TRACEABILITY_READ_TOOL_NAMES = frozenset(
    {
        "find_candidate_products",
        "match_lots",
        "trace_forward",
        "trace_backward",
        "get_inventory",
        "get_sales",
        "reconcile_units",
    }
)
_TRUSTED_READ_TOOL_NAMES = _REGISTRY_READ_TOOL_NAMES | _TRACEABILITY_READ_TOOL_NAMES
_RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "recall-intelligence": RecallIntelligence,
    "product-lot-matching": ProductLotAssessment,
    "traceability-reconciliation": TraceabilityAssessment,
    "containment-communications": ContainmentProposal,
}


class SpecialistDefinition(BaseModel):
    name: str
    description: str
    system_prompt: str
    allowed_tool_names: list[str]
    response_model_name: str


class DelegationState(AgentState):
    delegated_specialists: NotRequired[Annotated[list[str], PrivateStateAttr]]
    plan_written: NotRequired[Annotated[bool, PrivateStateAttr]]


class DelegationGuardMiddleware(AgentMiddleware):
    """Enforce the fixed four-role plan at the live-model runtime boundary."""

    state_schema = DelegationState

    def after_model(self, state: DelegationState, runtime: Any) -> dict[str, Any] | None:
        del runtime
        messages = state.get("messages", [])
        last_message = next(
            (message for message in reversed(messages) if isinstance(message, AIMessage)), None
        )
        if last_message is None:
            return None
        fixed = [item.name for item in specialist_catalog()]
        delegated = list(state.get("delegated_specialists", []))
        plan_written = state.get("plan_written", False)
        task_calls = []
        for call in last_message.tool_calls:
            if call["name"] == "write_todos":
                todos = call.get("args", {}).get("todos", [])
                if len(todos) != len(fixed):
                    raise ValueError("RecallOps requires exactly four todos")
                planned = [
                    name
                    for todo in todos
                    for name in fixed
                    if str(todo.get("content", "")).startswith(f"[{name}]")
                ]
                if planned != fixed:
                    raise ValueError("write_todos must contain the four fixed specialists in order")
                plan_written = True
            elif call["name"] == "task":
                task_calls.append(call)
        if task_calls and not state.get("plan_written", False):
            raise ValueError("write_todos plan must precede specialist delegation")
        for call in task_calls:
            specialist = call.get("args", {}).get("subagent_type")
            if specialist not in fixed:
                raise ValueError(f"unknown specialist delegation {specialist!r}")
            if specialist in delegated:
                raise ValueError(f"duplicate specialist delegation {specialist!r}")
            delegated.append(specialist)
        if len(delegated) > len(fixed):
            raise ValueError("specialist delegation budget exceeded")
        if not last_message.tool_calls and delegated != fixed:
            raise ValueError("live supervisor must complete exactly four specialist delegations")
        if task_calls:
            return {"delegated_specialists": delegated, "plan_written": plan_written}
        if plan_written != state.get("plan_written", False):
            return {"plan_written": plan_written}
        return None


@dataclass(frozen=True)
class DeepSupervisor:
    """The compiled graph plus an explicit, UI-friendly safety manifest."""

    graph: Any
    specialist_names: list[str]
    planning_tool_name: str
    delegation_tool_name: str
    operational_write_tool_names: list[str]
    exposed_read_tool_names: list[str]
    parent_tool_names: list[str]
    subagent_tool_names: dict[str, list[str]]
    capability_manifest: dict[str, str]


class _ReadConfig(NamedTuple):
    """Deeply immutable, digest-bound configuration retained by compiled tools."""

    transport: str
    data_dir: Path
    source_mode: str
    operations_db_path: Path
    python_executable: Path | None
    cwd: Path | None
    environment: tuple[tuple[str, str], ...]
    servers: tuple[tuple[str, str], ...]
    digest: str


class _SearchRecallsInput(BaseModel):
    query: str


class _GetRecallInput(BaseModel):
    recall_number: str


class _GetProductMetadataInput(BaseModel):
    upc: str


class _RecallPredicateInput(BaseModel):
    predicate: RecallPredicate


class _LotInput(BaseModel):
    lot_id: str


class _InventoryInput(BaseModel):
    lot_id: str | None = None


class _SealedCapabilityMeta(type):
    """Prevent supported post-build mutation of capability class authority."""

    def __setattr__(cls, name: str, value: Any) -> None:
        del name, value
        raise TypeError("sealed RecallOps capability class cannot be modified")

    def __delattr__(cls, name: str) -> None:
        del name
        raise TypeError("sealed RecallOps capability class cannot be modified")


class _SealedReadCapability(metaclass=_SealedCapabilityMeta):
    """Stateless callable whose authority lives in sealed, immutable class state.

    Threat boundary: ordinary gateway, middleware, tool, instance, and class
    configuration mutation is rejected. A same-interpreter actor able to replace
    Python code objects or module globals can replace the running program itself
    and is intentionally outside this boundary.
    """

    __slots__ = ()

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise TypeError("sealed RecallOps capability instance cannot be modified")

    def __delattr__(self, name: str) -> None:
        del name
        raise TypeError("sealed RecallOps capability instance cannot be modified")

    async def _read(self, payload: dict[str, Any]) -> Any:
        config, expected_digest, tool_name = _sealed_capability_authority(self)
        validated_payload = _validated_read_payload(tool_name, payload)
        return await _invoke_trusted_read(
            config,
            expected_digest,
            tool_name,
            validated_payload,
        )


class _SearchRecallsCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, query: str) -> Any:
        if type(query) is not str:
            raise ValueError("search_recalls query must be an exact string")
        return await self._read({"query": query})


class _GetRecallCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, recall_number: str) -> Any:
        if type(recall_number) is not str:
            raise ValueError("get_recall recall_number must be an exact string")
        return await self._read({"recall_number": recall_number})


class _GetProductMetadataCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, upc: str) -> Any:
        if type(upc) is not str:
            raise ValueError("get_product_metadata upc must be an exact string")
        return await self._read({"upc": upc})


class _FindCandidateProductsCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, predicate: RecallPredicate) -> Any:
        if type(predicate) is not RecallPredicate:
            raise ValueError("find_candidate_products predicate must be an exact RecallPredicate")
        return await self._read({"predicate": predicate})


class _MatchLotsCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, predicate: RecallPredicate) -> Any:
        if type(predicate) is not RecallPredicate:
            raise ValueError("match_lots predicate must be an exact RecallPredicate")
        return await self._read({"predicate": predicate})


class _TraceForwardCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, lot_id: str) -> Any:
        if type(lot_id) is not str:
            raise ValueError("trace_forward lot_id must be an exact string")
        return await self._read({"lot_id": lot_id})


class _TraceBackwardCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, lot_id: str) -> Any:
        if type(lot_id) is not str:
            raise ValueError("trace_backward lot_id must be an exact string")
        return await self._read({"lot_id": lot_id})


class _GetInventoryCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, lot_id: str | None = None) -> Any:
        if type(lot_id) not in {str, type(None)}:
            raise ValueError("get_inventory lot_id must be an exact string or None")
        return await self._read({"lot_id": lot_id})


class _GetSalesCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, lot_id: str) -> Any:
        if type(lot_id) is not str:
            raise ValueError("get_sales lot_id must be an exact string")
        return await self._read({"lot_id": lot_id})


class _ReconcileUnitsCapability(_SealedReadCapability):
    __slots__ = ()

    async def __call__(self, lot_id: str) -> Any:
        if type(lot_id) is not str:
            raise ValueError("reconcile_units lot_id must be an exact string")
        return await self._read({"lot_id": lot_id})


class _SealedToolMeta(type(BaseTool)):
    """Seal the wrapper class after Pydantic finishes constructing it."""

    def __new__(
        metaclass: type[_SealedToolMeta],
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> _SealedToolMeta:
        cls = super().__new__(metaclass, name, bases, namespace, **kwargs)
        type.__setattr__(cls, "model_config", MappingProxyType(dict(cls.model_config)))
        type.__setattr__(cls, "_recallops_sealed", True)
        return cls

    def __setattr__(cls, name: str, value: Any) -> None:
        if cls.__dict__.get("_recallops_sealed", False):
            raise TypeError("sealed RecallOps tool wrapper class cannot be modified")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if cls.__dict__.get("_recallops_sealed", False):
            raise TypeError("sealed RecallOps tool wrapper class cannot be modified")
        super().__delattr__(name)


class _SealedReadTool(BaseTool, metaclass=_SealedToolMeta):
    """Frozen metadata wrapper with authority only in sealed class state."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    _capability: ClassVar[_SealedReadCapability]
    _trusted_args_schema: ClassVar[type[BaseModel]]
    _trusted_description: ClassVar[str]

    def __getattribute__(self, name: str) -> Any:
        tool_type = type(self)
        class_state = vars(tool_type)
        if name == "name":
            capability = class_state["_capability"]
            return vars(type(capability))["_tool_name"]
        if name == "description":
            return class_state["_trusted_description"]
        if name == "args_schema":
            return class_state["_trusted_args_schema"]
        if name in {"callbacks", "tags", "metadata", "extras"}:
            return None
        if name in {
            "return_direct",
            "verbose",
            "handle_tool_error",
            "handle_validation_error",
        }:
            return False
        if name == "response_format":
            return "content"
        if name == "_injected_args_keys":
            return BaseTool._injected_args_keys.func(self)
        if name == "__dict__":
            state = super().__getattribute__("__dict__")
            return MappingProxyType(state)
        # Keep the complete invocation path on sealed class code even if a caller
        # writes shadow attributes into Pydantic's otherwise mutable storage.
        if name == "ainvoke":
            return _SealedReadTool.ainvoke.__get__(self, type(self))
        if name == "invoke":
            return _SealedReadTool.invoke.__get__(self, type(self))
        if name == "arun":
            return _SealedReadTool.arun.__get__(self, type(self))
        if name == "run":
            return _SealedReadTool.run.__get__(self, type(self))
        if name == "_arun":
            return _SealedReadTool._arun.__get__(self, type(self))
        if name == "_run":
            return _SealedReadTool._run.__get__(self, type(self))
        if name == "_filter_injected_args":
            return BaseTool._filter_injected_args.__get__(self, type(self))
        if name == "_to_args_and_kwargs":
            return BaseTool._to_args_and_kwargs.__get__(self, type(self))
        if name == "_parse_input":
            return BaseTool._parse_input.__get__(self, type(self))
        if name == "get_input_schema":
            return BaseTool.get_input_schema.__get__(self, type(self))
        return super().__getattribute__(name)

    @property
    def coroutine(self) -> Any:
        """Expose only the class-bound validated async invocation surface."""
        return _SealedReadTool._validated_coroutine.__get__(self, type(self))

    async def _validated_coroutine(self, *args: Any, **payload: Any) -> Any:
        if len(args) > 1 or (args and payload):
            raise ValueError("sealed RecallOps coroutine accepts one positional or keyword input")
        if args:
            capability = _sealed_tool_capability(self)
            _config, _expected_digest, tool_name = _sealed_capability_authority(capability)
            fields = tuple(_capability_args_schema(tool_name).model_fields)
            if len(fields) != 1:
                raise ValueError("sealed RecallOps coroutine requires one input field")
            tool_input = {fields[0]: args[0]}
        else:
            tool_input = payload
        return await _invoke_sealed_read_tool(self, tool_input, tool_call_id=None)

    def invoke(
        self,
        input: Any,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        del input, config, kwargs
        raise RuntimeError("RecallOps sealed read tools require asynchronous invocation")

    async def ainvoke(
        self,
        input: Any,
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        del config, kwargs
        return await _invoke_sealed_read_tool(self, input, tool_call_id=None)

    def run(
        self,
        tool_input: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        del tool_input, args, kwargs
        raise RuntimeError("RecallOps sealed read tools require asynchronous invocation")

    async def arun(
        self,
        tool_input: Any,
        *args: Any,
        tool_call_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        del args, kwargs
        return await _invoke_sealed_read_tool(self, tool_input, tool_call_id=tool_call_id)

    def _run(self, **payload: Any) -> Any:
        del payload
        raise RuntimeError("RecallOps sealed read tools require asynchronous invocation")

    async def _arun(self, **payload: Any) -> Any:
        return await _invoke_sealed_read_tool(self, payload, tool_call_id=None)


def specialist_catalog() -> list[SpecialistDefinition]:
    """Return the fixed roles and their least-privilege read-tool allowlists."""
    return [
        SpecialistDefinition(
            name="recall-intelligence",
            description="Extract official recall scope and citations.",
            system_prompt=RECALL_INTELLIGENCE_PROMPT,
            allowed_tool_names=["search_recalls", "get_recall", "get_product_metadata"],
            response_model_name="RecallIntelligence",
        ),
        SpecialistDefinition(
            name="product-lot-matching",
            description="Classify synthetic products and lots with field-level rationale.",
            system_prompt=PRODUCT_LOT_MATCHING_PROMPT,
            allowed_tool_names=["find_candidate_products", "match_lots"],
            response_model_name="ProductLotAssessment",
        ),
        SpecialistDefinition(
            name="traceability-reconciliation",
            description="Trace parent-linked events and reconcile affected units.",
            system_prompt=TRACEABILITY_RECONCILIATION_PROMPT,
            allowed_tool_names=[
                "trace_forward",
                "trace_backward",
                "get_inventory",
                "get_sales",
                "reconcile_units",
            ],
            response_model_name="TraceabilityAssessment",
        ),
        SpecialistDefinition(
            name="containment-communications",
            description="Draft cited containment and internal communications without writes.",
            system_prompt=CONTAINMENT_COMMUNICATIONS_PROMPT,
            allowed_tool_names=[],
            response_model_name="ContainmentProposal",
        ),
    ]


def _profile_key(model: str | BaseChatModel) -> str:
    if isinstance(model, str):
        return model
    params = model._get_ls_params()
    provider = params.get("ls_provider")
    identifier = getattr(model, "model_name", None) or getattr(model, "model", None)
    if not isinstance(provider, str) or not provider:
        raise ValueError("model does not expose a provider for a fixed Deep Agents profile")
    if isinstance(identifier, str) and identifier and ":" not in identifier:
        return f"{provider}:{identifier}"
    return provider


def _compiled_tools(graph: Any) -> dict[str, BaseTool]:
    tool_node = graph.nodes.get("tools")
    if tool_node is None or not hasattr(tool_node.bound, "_tools_by_name"):
        raise ValueError("compiled Deep Agent graph has no inspectable tool node")
    return dict(tool_node.bound._tools_by_name)


def _compiled_tool_names(graph: Any) -> list[str]:
    return sorted(_compiled_tools(graph))


def _compiled_subagent_graphs(graph: Any) -> dict[str, Any]:
    task_tool = graph.nodes["tools"].bound._tools_by_name.get("task")
    if task_tool is None or task_tool.func is None:
        raise ValueError("compiled Deep Agent graph has no delegation tool")
    graphs = inspect.getclosurevars(task_tool.func).nonlocals.get("subagent_graphs")
    if not isinstance(graphs, dict):
        raise ValueError("compiled Deep Agent delegation registry is not inspectable")
    return graphs


def _is_plain_json(value: Any) -> bool:
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_plain_json(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _is_plain_json(item) for key, item in value.items())
    return False


def _has_exact_keys(value: Any, expected: frozenset[str]) -> bool:
    if type(value) is not dict or len(value) != len(expected):
        return False
    if any(type(key) is not str for key in value):
        return False
    return all(key in value for key in expected)


def _exact_state(instance: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    state = vars(instance)
    if not _has_exact_keys(state, expected):
        raise ValueError(f"trusted RecallOps {label} has shadowed capabilities")
    return state


def _validated_settings() -> Settings:
    settings = get_settings()
    path_type = type(PROJECT_ROOT)
    if (
        type(settings) is not Settings
        or type(settings.data_dir) is not path_type
        or type(settings.operations_db_path) is not path_type
        or type(settings.source_mode) is not str
    ):
        raise ValueError("trusted RecallOps Settings require exact trusted state types")
    if settings.source_mode not in {"snapshot", "live"}:
        raise ValueError("trusted RecallOps Settings contain an invalid source mode")
    return settings


_TRACEABILITY_SERVICE_STATE_FIELDS = frozenset(
    {"data_dir", "source_mode", "dataset", "_retrieval_index"}
)


def _traceability_service_state(service: TraceabilityService) -> dict[str, Any]:
    if type(service) is not TraceabilityService:
        raise ValueError("trusted RecallOps read gateway requires exact service identities")
    return _exact_state(
        service,
        _TRACEABILITY_SERVICE_STATE_FIELDS,
        "traceability service",
    )


def _validate_traceability_service(
    service: TraceabilityService,
    *,
    settings: Settings,
    trusted_dataset: dict[str, Any],
) -> None:
    state = _traceability_service_state(service)
    path_type = type(settings.data_dir)
    if type(state["data_dir"]) is not path_type or type(state["source_mode"]) is not str:
        raise ValueError("trusted RecallOps direct gateway requires exact trusted state types")
    if state["_retrieval_index"] is not None:
        raise ValueError("trusted RecallOps read gateway forbids caller-supplied retrieval index")
    if type(state["dataset"]) is not dict or not _is_plain_json(state["dataset"]):
        raise ValueError("trusted RecallOps read gateway requires a plain validated dataset")
    if state["data_dir"] != settings.data_dir or state["source_mode"] != settings.source_mode:
        raise ValueError("trusted RecallOps read gateway configuration differs from Settings")
    if state["dataset"] != trusted_dataset:
        raise ValueError("trusted RecallOps read gateway requires the validated dataset snapshot")


def _trusted_stdio_environment(
    data_dir: Path,
    source_mode: str,
    operations_db_path: Path,
) -> tuple[tuple[str, str], ...]:
    """Return immutable entries overriding every environment variable MCP inherits."""
    return (
        ("HOME", str(PROJECT_ROOT / ".recallops-runtime" / "stdio-home")),
        ("LOGNAME", "recallops"),
        ("PATH", os.defpath),
        ("PYTHONNOUSERSITE", "1"),
        ("PYTHONPATH", ""),
        ("RECALLOPS_DATA_DIR", str(data_dir)),
        ("RECALLOPS_OPERATIONS_DB", str(operations_db_path)),
        ("RECALLOPS_SOURCE_MODE", source_mode),
        ("SHELL", "/bin/sh"),
        ("TERM", "dumb"),
        ("USER", "recallops"),
    )


def _read_config_digest(config: _ReadConfig) -> str:
    payload = json.dumps(
        {
            "transport": config.transport,
            "data_dir": str(config.data_dir),
            "source_mode": config.source_mode,
            "operations_db_path": str(config.operations_db_path),
            "python_executable": (
                str(config.python_executable) if config.python_executable is not None else None
            ),
            "cwd": str(config.cwd) if config.cwd is not None else None,
            "environment": config.environment,
            "servers": config.servers,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _make_read_config(transport: str, settings: Settings) -> _ReadConfig:
    if transport == "direct":
        provisional = _ReadConfig(
            transport="direct",
            data_dir=settings.data_dir,
            source_mode=settings.source_mode,
            operations_db_path=settings.operations_db_path,
            python_executable=None,
            cwd=None,
            environment=(),
            servers=(),
            digest="",
        )
    elif transport == "stdio":
        provisional = _ReadConfig(
            transport="stdio",
            data_dir=settings.data_dir,
            source_mode=settings.source_mode,
            operations_db_path=settings.operations_db_path,
            python_executable=_PYTHON_EXECUTABLE,
            cwd=PROJECT_ROOT,
            environment=_trusted_stdio_environment(
                settings.data_dir,
                settings.source_mode,
                settings.operations_db_path,
            ),
            servers=_STDIO_READ_SERVERS,
            digest="",
        )
    else:  # pragma: no cover - only trusted factory literals call this helper
        raise ValueError("unsupported RecallOps read transport")
    return provisional._replace(digest=_read_config_digest(provisional))


def _has_exact_string_pairs(value: Any) -> bool:
    if type(value) is not tuple:
        return False
    for pair in value:
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not str
            or type(pair[1]) is not str
        ):
            return False
    return True


def _validate_read_config(
    config: _ReadConfig,
    *,
    expected_config_digest: str,
) -> None:
    path_type = type(PROJECT_ROOT)
    if type(config) is not _ReadConfig:
        raise ValueError("trusted RecallOps requires an exact immutable read configuration")
    if (
        type(config.transport) is not str
        or type(config.data_dir) is not path_type
        or type(config.source_mode) is not str
        or type(config.operations_db_path) is not path_type
        or type(config.digest) is not str
        or not _has_exact_string_pairs(config.environment)
        or not _has_exact_string_pairs(config.servers)
    ):
        raise ValueError("trusted RecallOps immutable read configuration has invalid types")
    if (
        not config.data_dir.is_absolute()
        or not config.operations_db_path.is_absolute()
        or config.source_mode not in {"snapshot", "live"}
    ):
        raise ValueError("trusted RecallOps immutable read configuration is invalid")
    actual_digest = _read_config_digest(config)
    if not hmac.compare_digest(config.digest, actual_digest):
        raise ValueError("trusted RecallOps immutable read configuration digest mismatch")
    if type(expected_config_digest) is not str or not hmac.compare_digest(
        config.digest,
        expected_config_digest,
    ):
        raise ValueError("trusted RecallOps build-time read configuration digest mismatch")
    if config.transport == "direct":
        if (
            config.python_executable is not None
            or config.cwd is not None
            or config.environment != ()
            or config.servers != ()
        ):
            raise ValueError("trusted RecallOps direct read configuration is invalid")
    elif config.transport == "stdio":
        if type(config.python_executable) is not path_type or type(config.cwd) is not path_type:
            raise ValueError("trusted RecallOps stdio read configuration has invalid types")
        if (
            not config.python_executable.is_absolute()
            or not config.cwd.is_absolute()
            or config.python_executable != _PYTHON_EXECUTABLE
            or config.cwd != PROJECT_ROOT
            or config.servers != _STDIO_READ_SERVERS
            or config.environment
            != _trusted_stdio_environment(
                config.data_dir,
                config.source_mode,
                config.operations_db_path,
            )
        ):
            raise ValueError("trusted RecallOps stdio read configuration is invalid")
    else:
        raise ValueError("trusted RecallOps immutable read transport is invalid")


def _close_read_gateway(
    gateway: DirectGateway | StdioMCPGateway,
) -> _ReadConfig:
    if type(gateway) is DirectGateway:
        gateway_state = _exact_state(
            gateway,
            frozenset({"registry", "traceability", "operations"}),
            "direct gateway",
        )
        registry = gateway_state["registry"]
        traceability = gateway_state["traceability"]
        operations = gateway_state["operations"]
        if (
            type(registry) is not RecallRegistryService
            or type(traceability) is not TraceabilityService
            or type(operations) is not OperationsService
        ):
            raise ValueError("trusted RecallOps read gateway requires exact service identities")
        registry_state = _exact_state(
            registry,
            frozenset(
                {
                    "data_dir",
                    "source_mode",
                    "http_transport",
                    "timeout_seconds",
                    "_retrieval_index",
                }
            ),
            "registry service",
        )
        traceability_state = _traceability_service_state(traceability)
        operations_state = _exact_state(
            operations,
            frozenset(
                {
                    "storage_path",
                    "source_mode",
                    "_failure_injector",
                    "_before_cas_hook",
                    "traceability",
                }
            ),
            "operations service",
        )
        operations_traceability_state = _traceability_service_state(
            operations_state["traceability"]
        )
        settings = _validated_settings()
        path_type = type(settings.data_dir)
        timeout = registry_state["timeout_seconds"]
        if (
            type(registry_state["data_dir"]) is not path_type
            or type(registry_state["source_mode"]) is not str
            or type(timeout) not in {int, float}
            or (type(timeout) is float and not math.isfinite(timeout))
            or type(operations_state["storage_path"]) is not path_type
            or type(operations_state["source_mode"]) is not str
            or type(operations_state["traceability"]) is not TraceabilityService
            or type(traceability_state["data_dir"]) is not path_type
            or type(traceability_state["source_mode"]) is not str
            or type(operations_traceability_state["data_dir"]) is not path_type
            or type(operations_traceability_state["source_mode"]) is not str
        ):
            raise ValueError("trusted RecallOps direct gateway requires exact trusted state types")
        if registry_state["http_transport"] is not None:
            raise ValueError(
                "trusted RecallOps read gateway forbids caller-supplied HTTP transport"
            )
        if (
            operations_state["_failure_injector"] is not None
            or operations_state["_before_cas_hook"] is not None
        ):
            raise ValueError(
                "trusted RecallOps read gateway forbids caller-supplied operation hook"
            )
        if any(
            state["_retrieval_index"] is not None
            for state in (
                registry_state,
                traceability_state,
                operations_traceability_state,
            )
        ):
            raise ValueError(
                "trusted RecallOps read gateway forbids caller-supplied retrieval index"
            )
        # These loaders validate the pinned public snapshot and synthetic manifest
        # before any caller-owned service is retained by a compiled capability.
        load_recall_snapshot(data_dir=settings.data_dir)
        trusted_dataset = load_demo_dataset(settings.data_dir)
        _validate_traceability_service(
            traceability,
            settings=settings,
            trusted_dataset=trusted_dataset,
        )
        _validate_traceability_service(
            operations_state["traceability"],
            settings=settings,
            trusted_dataset=trusted_dataset,
        )
        if (
            registry_state["data_dir"] != settings.data_dir
            or registry_state["source_mode"] != settings.source_mode
            or timeout != 2.0
            or operations_state["storage_path"] != settings.operations_db_path
            or operations_state["source_mode"] != settings.source_mode
        ):
            raise ValueError("trusted RecallOps read gateway configuration differs from Settings")
        return _make_read_config("direct", settings)
    if type(gateway) is StdioMCPGateway:
        gateway_state = _exact_state(
            gateway,
            frozenset({"client"}),
            "stdio gateway",
        )
        client = gateway_state["client"]
        from langchain_mcp_adapters.callbacks import Callbacks
        from langchain_mcp_adapters.client import MultiServerMCPClient

        if type(client) is not MultiServerMCPClient:
            raise ValueError("trusted RecallOps stdio gateway requires the official MCP client")
        client_state = _exact_state(
            client,
            frozenset({"connections", "callbacks", "tool_interceptors", "tool_name_prefix"}),
            "stdio client",
        )
        connections = client_state["connections"]
        callbacks = client_state["callbacks"]
        interceptors = client_state["tool_interceptors"]
        tool_name_prefix = client_state["tool_name_prefix"]
        if (
            type(connections) is not dict
            or type(callbacks) is not Callbacks
            or type(interceptors) is not list
            or type(tool_name_prefix) is not bool
        ):
            raise ValueError(
                "trusted RecallOps stdio gateway requires exact trusted stdio state types"
            )
        if len(interceptors) != 0 or tool_name_prefix is not False:
            raise ValueError("trusted RecallOps stdio gateway forbids client interception")
        callback_state = _exact_state(
            callbacks,
            frozenset({"on_logging_message", "on_progress", "on_elicitation"}),
            "stdio callbacks",
        )
        if any(value is not None for value in callback_state.values()):
            raise ValueError("trusted RecallOps stdio callbacks must all be disabled")
        if not _has_exact_keys(
            connections,
            frozenset(server for server, _module in _STDIO_SERVERS),
        ):
            raise ValueError("trusted RecallOps stdio server identity set is incomplete")
        for server, module in _STDIO_SERVERS:
            connection = connections[server]
            if type(connection) is not dict:
                raise ValueError(
                    "trusted RecallOps stdio gateway requires exact trusted stdio state types"
                )
            if not _has_exact_keys(
                connection,
                frozenset({"transport", "command", "args"}),
            ):
                raise ValueError(f"trusted RecallOps stdio server identity mismatch: {server}")
            transport = connection["transport"]
            command = connection["command"]
            args = connection["args"]
            if (
                type(transport) is not str
                or type(command) is not str
                or type(args) is not list
                or any(type(argument) is not str for argument in args)
            ):
                raise ValueError(
                    "trusted RecallOps stdio gateway requires exact trusted stdio state types"
                )
            if transport != "stdio" or command != sys.executable or args != ["-m", module]:
                raise ValueError(f"trusted RecallOps stdio server identity mismatch: {server}")
        settings = _validated_settings()
        return _make_read_config("stdio", settings)
    raise ValueError("read_gateway must be a trusted RecallOps read gateway")


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if type(value) is dict:
        return {key: _json_value(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_json_value(item) for item in value]
    return value


def _invoke_direct_read(config: _ReadConfig, tool_name: str, payload: dict[str, Any]) -> Any:
    if tool_name in _REGISTRY_READ_TOOL_NAMES:
        registry = RecallRegistryService(
            data_dir=config.data_dir,
            source_mode=config.source_mode,
        )
        if tool_name == "search_recalls":
            return _json_value(registry.search_recalls(payload["query"]))
        if tool_name == "get_recall":
            return _json_value(registry.get_recall(payload["recall_number"]))
        metadata = registry.get_product_metadata(payload["upc"])
        return _json_value(ProductMetadata.model_validate(metadata)) if metadata else None

    traceability = TraceabilityService(
        data_dir=config.data_dir,
        source_mode=config.source_mode,
    )
    if tool_name == "find_candidate_products":
        return _json_value(
            [
                CandidateProduct.model_validate(item)
                for item in traceability.find_candidate_products(payload["predicate"])
            ]
        )
    if tool_name == "match_lots":
        return _json_value(
            [
                LotMatch.model_validate(item)
                for item in traceability.match_lots(payload["predicate"])
            ]
        )
    if tool_name == "trace_forward":
        return _json_value(
            [
                TraceEvent.model_validate(item)
                for item in traceability.trace_forward(payload["lot_id"])
            ]
        )
    if tool_name == "trace_backward":
        return _json_value(
            [
                TraceEvent.model_validate(item)
                for item in traceability.trace_backward(payload["lot_id"])
            ]
        )
    if tool_name == "get_inventory":
        return _json_value(
            [
                InventoryPosition.model_validate(item)
                for item in traceability.get_inventory(payload["lot_id"])
            ]
        )
    if tool_name == "get_sales":
        return _json_value(
            [TraceEvent.model_validate(item) for item in traceability.get_sales(payload["lot_id"])]
        )
    return _json_value(traceability.reconcile_units(payload["lot_id"]))


def _stdio_server_for_tool(config: _ReadConfig, tool_name: str) -> tuple[str, str]:
    expected_server = "registry" if tool_name in _REGISTRY_READ_TOOL_NAMES else "traceability"
    return next(pair for pair in config.servers if pair[0] == expected_server)


async def _invoke_stdio_read(
    config: _ReadConfig,
    tool_name: str,
    payload: dict[str, Any],
) -> Any:
    server, module = _stdio_server_for_tool(config, tool_name)
    connection = {
        server: {
            "transport": "stdio",
            "command": str(config.python_executable),
            "args": ["-I", "-m", module],
            "cwd": str(config.cwd),
            "env": dict(config.environment),
        }
    }
    gateway = StdioMCPGateway(connection)
    try:
        if tool_name == "search_recalls":
            return await gateway.search_recalls(payload["query"])
        if tool_name == "get_recall":
            return await gateway.get_recall(payload["recall_number"])
        if tool_name == "get_product_metadata":
            return await gateway.get_product_metadata(payload["upc"])
        if tool_name == "find_candidate_products":
            return await gateway.find_candidate_products(payload["predicate"])
        if tool_name == "match_lots":
            return await gateway.match_lots(payload["predicate"])
        if tool_name == "trace_forward":
            return await gateway.trace_forward(payload["lot_id"])
        if tool_name == "trace_backward":
            return await gateway.trace_backward(payload["lot_id"])
        if tool_name == "get_inventory":
            return await gateway.get_inventory(payload["lot_id"])
        if tool_name == "get_sales":
            return await gateway.get_sales(payload["lot_id"])
        return await gateway.reconcile_units(payload["lot_id"])
    finally:
        # MultiServerMCPClient has no persistent resource to close. Every tool
        # discovery and invocation owns an MCP session context that closes on exit.
        del gateway


async def _invoke_trusted_read(
    config: _ReadConfig,
    expected_config_digest: str,
    tool_name: str,
    payload: dict[str, Any],
) -> Any:
    _validate_read_config(
        config,
        expected_config_digest=expected_config_digest,
    )
    if type(tool_name) is not str or tool_name not in _TRUSTED_READ_TOOL_NAMES:
        raise ValueError("trusted RecallOps read capability is invalid")
    if config.transport == "direct":
        return _invoke_direct_read(config, tool_name, payload)
    return await _invoke_stdio_read(config, tool_name, payload)


def _capability_base(tool_name: str) -> type[_SealedReadCapability]:
    if tool_name == "search_recalls":
        return _SearchRecallsCapability
    if tool_name == "get_recall":
        return _GetRecallCapability
    if tool_name == "get_product_metadata":
        return _GetProductMetadataCapability
    if tool_name == "find_candidate_products":
        return _FindCandidateProductsCapability
    if tool_name == "match_lots":
        return _MatchLotsCapability
    if tool_name == "trace_forward":
        return _TraceForwardCapability
    if tool_name == "trace_backward":
        return _TraceBackwardCapability
    if tool_name == "get_inventory":
        return _GetInventoryCapability
    if tool_name == "get_sales":
        return _GetSalesCapability
    if tool_name == "reconcile_units":
        return _ReconcileUnitsCapability
    raise ValueError("trusted RecallOps read capability is invalid")


def _capability_args_schema(tool_name: str) -> type[BaseModel]:
    if tool_name == "search_recalls":
        return _SearchRecallsInput
    if tool_name == "get_recall":
        return _GetRecallInput
    if tool_name == "get_product_metadata":
        return _GetProductMetadataInput
    if tool_name in {"find_candidate_products", "match_lots"}:
        return _RecallPredicateInput
    if tool_name == "get_inventory":
        return _InventoryInput
    if tool_name in {
        "trace_forward",
        "trace_backward",
        "get_sales",
        "reconcile_units",
    }:
        return _LotInput
    raise ValueError("trusted RecallOps read capability is invalid")


_RECALL_PREDICATE_FIELDS = frozenset(
    {
        "product_terms",
        "upcs",
        "plant_codes",
        "julian_start",
        "julian_end",
        "geography",
        "hazard",
    }
)


def _detached_recall_predicate(value: Any) -> RecallPredicate:
    """Return an exact, revalidated copy without invoking caller model hooks."""
    if type(value) is RecallPredicate:
        state = vars(value)
        if not _has_exact_keys(state, _RECALL_PREDICATE_FIELDS) or not _is_plain_json(state):
            raise ValueError("trusted RecallOps predicate must contain exact plain JSON state")
        plain = json.loads(
            json.dumps(
                state,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    elif type(value) is dict:
        if not _has_exact_keys(value, _RECALL_PREDICATE_FIELDS) or not _is_plain_json(value):
            raise ValueError("trusted RecallOps predicate must be an exact plain object")
        plain = json.loads(
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        raise ValueError("trusted RecallOps predicate must be an exact RecallPredicate")
    predicate = RecallPredicate.model_validate(plain, strict=True)
    if type(predicate) is not RecallPredicate:
        raise ValueError("trusted RecallOps predicate validation returned an invalid type")
    return predicate


def _validated_read_payload(tool_name: str, payload: Any) -> dict[str, Any]:
    """Strictly validate one tool payload before services, clients, or comparisons."""
    if type(tool_name) is not str or tool_name not in _TRUSTED_READ_TOOL_NAMES:
        raise ValueError("trusted RecallOps read capability is invalid")
    if type(payload) is not dict or any(type(key) is not str for key in payload):
        raise ValueError("trusted RecallOps tool input must be an exact plain object")
    schema = _capability_args_schema(tool_name)
    fields = frozenset(schema.model_fields)
    if not set(payload) <= fields:
        raise ValueError("trusted RecallOps tool input contains unexpected fields")
    normalized: dict[str, Any] = {}
    for field_name, value in payload.items():
        if field_name == "predicate":
            normalized[field_name] = _detached_recall_predicate(value)
        elif field_name == "lot_id" and tool_name == "get_inventory" and value is None:
            normalized[field_name] = None
        elif type(value) is str:
            normalized[field_name] = value
        else:
            raise ValueError(f"trusted RecallOps {field_name} input must be an exact string")
    validated = schema.model_validate(normalized, strict=True)
    validated_payload = {
        field_name: getattr(validated, field_name) for field_name in schema.model_fields
    }
    for value in validated_payload.values():
        if type(value) not in {str, type(None), RecallPredicate}:
            raise ValueError("trusted RecallOps tool input contains an invalid value type")
    return validated_payload


def _capability_description(tool_name: str) -> str:
    if tool_name == "search_recalls":
        return "Search official recall registry evidence."
    if tool_name == "get_recall":
        return "Get one official recall record by recall number."
    if tool_name == "get_product_metadata":
        return "Get official product metadata by UPC."
    if tool_name == "find_candidate_products":
        return "Find synthetic retailer product candidates for a recall predicate."
    if tool_name == "match_lots":
        return "Match synthetic retailer lots to a recall predicate."
    if tool_name == "trace_forward":
        return "Trace a synthetic lot forward through the facility network."
    if tool_name == "trace_backward":
        return "Trace a synthetic lot backward to its receiving root."
    if tool_name == "get_inventory":
        return "Read synthetic retailer inventory positions."
    if tool_name == "get_sales":
        return "Read synthetic retailer sale events for a lot."
    if tool_name == "reconcile_units":
        return "Read evidence-backed synthetic unit reconciliation for a lot."
    raise ValueError("trusted RecallOps read capability is invalid")


def _sealed_capability_authority(
    capability: _SealedReadCapability,
) -> tuple[_ReadConfig, str, str]:
    capability_type = type(capability)
    if type(capability_type) is not _SealedCapabilityMeta:
        raise ValueError("trusted RecallOps read capability is not sealed")
    class_state = vars(capability_type)
    config = class_state.get("_config")
    expected_digest = class_state.get("_expected_digest")
    tool_name = class_state.get("_tool_name")
    if (
        type(config) is not _ReadConfig
        or type(expected_digest) is not str
        or type(tool_name) is not str
        or capability_type.__bases__ != (_capability_base(tool_name),)
    ):
        raise ValueError("trusted RecallOps sealed capability authority is invalid")
    _validate_read_config(config, expected_config_digest=expected_digest)
    return config, expected_digest, tool_name


def _make_sealed_capability(
    config: _ReadConfig,
    tool_name: str,
) -> _SealedReadCapability:
    base = _capability_base(tool_name)
    # Encode/decode forces an independent immutable scalar authority rather than
    # deriving the expected value from the config during each invocation.
    expected_digest = config.digest.encode("ascii").decode("ascii")
    capability_type = _SealedCapabilityMeta(
        f"_Bound{''.join(part.title() for part in tool_name.split('_'))}Capability",
        (base,),
        {
            "__module__": __name__,
            "__slots__": (),
            "_config": config,
            "_expected_digest": expected_digest,
            "_tool_name": tool_name,
        },
    )
    capability = capability_type()
    _sealed_capability_authority(capability)
    return capability


def _sealed_tool_capability(tool: BaseTool) -> _SealedReadCapability:
    tool_type = type(tool)
    if type(tool_type) is not _SealedToolMeta or tool_type.__bases__ != (_SealedReadTool,):
        raise ValueError("trusted RecallOps read tool is not sealed")
    capability = vars(tool_type).get("_capability")
    schema = vars(tool_type).get("_trusted_args_schema")
    description = vars(tool_type).get("_trusted_description")
    if not isinstance(capability, _SealedReadCapability):
        raise ValueError("trusted RecallOps read tool capability is invalid")
    _config, _expected_digest, tool_name = _sealed_capability_authority(capability)
    if (
        schema is not _capability_args_schema(tool_name)
        or type(description) is not str
        or description != _capability_description(tool_name)
    ):
        raise ValueError("trusted RecallOps read tool metadata is invalid")
    return capability


def _validated_sealed_tool_input(
    tool: BaseTool,
    tool_input: Any,
    *,
    tool_call_id: str | None,
) -> tuple[_SealedReadCapability, dict[str, Any], str | None, str]:
    """Validate one invocation without consulting mutable tool-instance state."""
    capability = _sealed_tool_capability(tool)
    _config, _expected_digest, tool_name = _sealed_capability_authority(capability)
    resolved_tool_call_id = tool_call_id
    payload: Any = tool_input
    if type(tool_input) is dict:
        if any(type(key) is not str for key in tool_input):
            raise ValueError("trusted RecallOps tool input keys must be exact strings")
        marker = tool_input.get("type")
        if marker is not None and type(marker) is not str:
            raise ValueError("trusted RecallOps tool-call type must be an exact string")
        if marker == "tool_call":
            if set(tool_input) != {"name", "args", "id", "type"}:
                raise ValueError("trusted RecallOps tool call has an invalid schema")
            name = tool_input["name"]
            arguments = tool_input["args"]
            call_id = tool_input["id"]
            if (
                type(name) is not str
                or name != tool_name
                or type(arguments) is not dict
                or type(call_id) is not str
                or not call_id
                or any(type(key) is not str for key in arguments)
            ):
                raise ValueError("trusted RecallOps tool call has invalid typed fields")
            payload = dict(arguments)
            resolved_tool_call_id = call_id
    if resolved_tool_call_id is not None and type(resolved_tool_call_id) is not str:
        raise ValueError("trusted RecallOps tool-call id must be an exact string")

    schema = _capability_args_schema(tool_name)
    if type(payload) is str:
        fields = tuple(schema.model_fields)
        if len(fields) != 1:
            raise ValueError("trusted RecallOps string input requires one schema field")
        payload = {fields[0]: payload}
    validated_payload = _validated_read_payload(tool_name, payload)
    return capability, validated_payload, resolved_tool_call_id, tool_name


async def _invoke_sealed_read_tool(
    tool: BaseTool,
    tool_input: Any,
    *,
    tool_call_id: str | None,
) -> Any:
    """Invoke a sealed read capability with only a fresh inert callback manager."""
    capability, payload, resolved_tool_call_id, tool_name = _validated_sealed_tool_input(
        tool,
        tool_input,
        tool_call_id=tool_call_id,
    )
    callback_manager = AsyncCallbackManager(handlers=[])
    run_manager = await callback_manager.on_tool_start(
        {
            "name": tool_name,
            "description": _capability_description(tool_name),
        },
        "sealed RecallOps read invocation",
        inputs=None,
        tool_call_id=resolved_tool_call_id,
    )
    try:
        content = await capability(**payload)
    except (Exception, KeyboardInterrupt) as error:
        await run_manager.on_tool_error(error, tool_call_id=resolved_tool_call_id)
        raise
    output = _format_output(
        content,
        None,
        resolved_tool_call_id,
        tool_name,
        "success",
    )
    await run_manager.on_tool_end(output, name=tool_name)
    return output


def _sealed_tool(
    capability: _SealedReadCapability,
) -> _SealedReadTool:
    config, expected_digest, tool_name = _sealed_capability_authority(capability)
    call_method = type(capability).__call__
    read_method = _SealedReadCapability._read
    tool_methods = (
        _SealedReadTool.invoke,
        _SealedReadTool.ainvoke,
        _SealedReadTool.run,
        _SealedReadTool.arun,
        _SealedReadTool._validated_coroutine,
        _SealedReadTool._run,
        _SealedReadTool._arun,
    )
    if (
        call_method.__closure__ is not None
        or read_method.__closure__ is not None
        or any(method.__closure__ is not None for method in tool_methods)
    ):
        raise ValueError("trusted RecallOps capability contains a closure")
    authority_defaults = (
        *(call_method.__defaults__ or ()),
        *(read_method.__defaults__ or ()),
        *(value for method in tool_methods for value in (method.__defaults__ or ())),
        *(value for method in tool_methods for value in (method.__kwdefaults__ or {}).values()),
    )
    if any(
        isinstance(value, _ReadConfig)
        or (type(value) is str and hmac.compare_digest(value, expected_digest))
        for value in authority_defaults
    ):
        raise ValueError("trusted RecallOps capability contains authority defaults")
    tool_type = _SealedToolMeta(
        f"_Bound{''.join(part.title() for part in tool_name.split('_'))}Tool",
        (_SealedReadTool,),
        {
            "__module__": __name__,
            "_capability": capability,
            "_trusted_args_schema": _capability_args_schema(tool_name),
            "_trusted_description": _capability_description(tool_name),
        },
    )
    tool = tool_type(
        name=tool_name,
        description=_capability_description(tool_name),
        args_schema=_capability_args_schema(tool_name),
    )
    if (
        _sealed_tool_capability(tool) is not capability
        or "_capability" in tool.__dict__
        or "coroutine" in tool.__dict__
        or "func" in tool.__dict__
        or config.digest != expected_digest
    ):
        raise ValueError("trusted RecallOps read tool wrapper is unsafe")
    return tool


def _trusted_read_tools(
    gateway: DirectGateway | StdioMCPGateway | None,
) -> tuple[dict[str, BaseTool], dict[str, str]]:
    if gateway is None:
        return {}, {}
    read_config = _close_read_gateway(gateway)
    capabilities = tuple(
        _make_sealed_capability(read_config, name) for name in sorted(_TRUSTED_READ_TOOL_NAMES)
    )
    tools = {
        type(capability).__dict__["_tool_name"]: _sealed_tool(capability)
        for capability in capabilities
    }
    if read_config.transport == "direct":
        identities = {
            name: (
                f"direct:reconstructed:sealed:config-sha256="
                f"{_sealed_capability_authority(_sealed_tool_capability(tools[name]))[1]}:"
                f"{'RecallRegistryService' if name in _REGISTRY_READ_TOOL_NAMES else 'TraceabilityService'}.{name}"
            )
            for name in tools
        }
    else:
        identities = {
            name: (
                f"stdio:reconstructed:sealed:"
                f"{'registry' if name in _REGISTRY_READ_TOOL_NAMES else 'traceability'}:"
                f"config-sha256={_sealed_capability_authority(_sealed_tool_capability(tools[name]))[1]}:{name}"
            )
            for name in tools
        }
    return tools, identities


def build_deep_supervisor(
    *,
    model: str | BaseChatModel,
    read_gateway: DirectGateway | StdioMCPGateway | None = None,
) -> DeepSupervisor:
    """Build a real Deep Agents graph without invoking the model or any provider.

    The explicit outer LangGraph remains operational authority. This optional
    reasoning graph receives read tools only; no Operations MCP write can be
    delegated or called from the supervisor.
    """
    catalog = specialist_catalog()
    allowed_read_names = {name for definition in catalog for name in definition.allowed_tool_names}
    tools_by_name, trusted_identities = _trusted_read_tools(read_gateway)
    if set(tools_by_name) - allowed_read_names:
        raise ValueError("trusted capability registry exceeds specialist read allowlists")

    # v0.7 makes planning opt-in and the general-purpose subagent opt-out through
    # model profiles. Registering the exact model key leaves only the four fixed roles.
    register_harness_profile(
        _profile_key(model),
        HarnessProfile(
            excluded_tools=_FILESYSTEM_WRITE_TOOL_NAMES,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    read_only_filesystem = FilesystemMiddleware(tools=["read_file", "ls"])
    subagent_filesystems: dict[str, FilesystemMiddleware] = {}
    subagents: list[dict[str, Any]] = []
    for definition in catalog:
        specialist_filesystem = FilesystemMiddleware(tools=["read_file", "ls"])
        subagent_filesystems[definition.name] = specialist_filesystem
        subagents.append(
            {
                "name": definition.name,
                "description": definition.description,
                "system_prompt": definition.system_prompt,
                "model": model,
                "tools": [
                    tools_by_name[name]
                    for name in definition.allowed_tool_names
                    if name in tools_by_name
                ],
                # Declarative subagents do not inherit the parent's filesystem restriction.
                "middleware": [specialist_filesystem],
                "response_format": _RESPONSE_MODELS[definition.name],
            }
        )
    delegation_guard = DelegationGuardMiddleware()
    task_limiter = ToolCallLimitMiddleware(
        tool_name="task",
        thread_limit=4,
        run_limit=4,
        exit_behavior="error",
    )
    todo_middleware = TodoListMiddleware()
    graph = create_deep_agent(
        model=model,
        tools=[],
        system_prompt=SUPERVISOR_PROMPT,
        middleware=[
            delegation_guard,
            task_limiter,
            todo_middleware,
            read_only_filesystem,
        ],
        subagents=subagents,
        name="recallops-supervisor",
    )
    if set(graph.nodes) != _PARENT_GRAPH_NODES:
        raise ValueError(f"compiled middleware surface is unsafe: {sorted(graph.nodes)}")
    middleware_identities = {
        "DelegationGuardMiddleware.after_model": delegation_guard,
        "ToolCallLimitMiddleware[task].after_model": task_limiter,
        "TodoListMiddleware.after_model": todo_middleware,
    }
    for node_name, middleware_instance in middleware_identities.items():
        bound = getattr(graph.nodes[node_name].bound, "func", None)
        if getattr(bound, "__self__", None) is not middleware_instance:
            raise ValueError(f"compiled middleware identity is unsafe: {node_name}")
    parent_tools = _compiled_tools(graph)
    parent_tool_names = sorted(parent_tools)
    if set(parent_tool_names) != _PARENT_TOOL_NAMES:
        raise ValueError(f"compiled parent tool surface is unsafe: {parent_tool_names}")
    filesystem_tools = {tool.name: tool for tool in read_only_filesystem.tools}
    for name, tool in filesystem_tools.items():
        if parent_tools.get(name) is not tool:
            raise ValueError(f"compiled parent capability {name!r} is untrusted")
    subagent_graphs = _compiled_subagent_graphs(graph)
    if set(subagent_graphs) != {item.name for item in catalog}:
        raise ValueError("compiled delegation registry differs from the fixed specialist catalog")
    subagent_tool_names: dict[str, list[str]] = {}
    capability_manifest: dict[str, str] = {}
    for definition in catalog:
        subgraph = subagent_graphs[definition.name]
        if set(subgraph.nodes) != _SUBAGENT_GRAPH_NODES:
            raise ValueError(
                f"compiled middleware surface is unsafe: {definition.name} {sorted(subgraph.nodes)}"
            )
        actual_tools = _compiled_tools(subgraph)
        subagent_tool_names[definition.name] = sorted(actual_tools)
        expected = {
            "ls",
            "read_file",
            *(name for name in definition.allowed_tool_names if name in tools_by_name),
        }
        actual = set(actual_tools)
        if actual != expected:
            raise ValueError(f"compiled {definition.name} tool surface is unsafe: {sorted(actual)}")
        specialist_fs_tools = {
            tool.name: tool for tool in subagent_filesystems[definition.name].tools
        }
        for name in ("ls", "read_file"):
            if actual_tools.get(name) is not specialist_fs_tools[name]:
                raise ValueError(f"compiled {definition.name} capability {name!r} is untrusted")
        for name in definition.allowed_tool_names:
            if name not in tools_by_name:
                continue
            if actual_tools.get(name) is not tools_by_name[name]:
                raise ValueError(f"untrusted compiled capability {name!r}")
            capability_manifest[name] = trusted_identities[name]
    all_actual_tools = set(parent_tool_names)
    for names in subagent_tool_names.values():
        all_actual_tools.update(names)
    operational_write_tools = sorted(all_actual_tools & OPERATIONAL_WRITE_TOOL_NAMES)
    if operational_write_tools:
        raise ValueError(
            f"compiled supervisor exposes operational writes: {operational_write_tools}"
        )
    exposed_read_tool_names = sorted(all_actual_tools & allowed_read_names)
    return DeepSupervisor(
        graph=graph,
        specialist_names=[item.name for item in catalog],
        planning_tool_name=("write_todos" if "write_todos" in parent_tool_names else ""),
        delegation_tool_name=("task" if "task" in parent_tool_names else ""),
        operational_write_tool_names=operational_write_tools,
        exposed_read_tool_names=exposed_read_tool_names,
        parent_tool_names=parent_tool_names,
        subagent_tool_names=subagent_tool_names,
        capability_manifest=dict(sorted(capability_manifest.items())),
    )
