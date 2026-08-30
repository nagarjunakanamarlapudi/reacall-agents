"""Optional live Deep Agents supervisor with fixed, read-only specialist scopes."""

from __future__ import annotations

import inspect
import math
import sys
from dataclasses import dataclass
from typing import Annotated, Any, NotRequired

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
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

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
from recallops.models import RecallPredicate
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
_STDIO_SERVERS = {
    "registry": "recallops.mcp.recall_registry_server",
    "traceability": "recallops.mcp.traceability_server",
    "operations": "recallops.mcp.operations_server",
}
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


@dataclass(frozen=True)
class _ClosedDirectReads:
    """Reconstructed services with no reference to caller-owned executable state."""

    registry: RecallRegistryService
    traceability: TraceabilityService


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
        return all(
            type(key) is str and _is_plain_json(item) for key, item in value.items()
        )
    return False


def _validate_traceability_service(
    service: TraceabilityService,
    *,
    settings: Settings,
    trusted_dataset: dict[str, Any],
) -> None:
    if type(service) is not TraceabilityService:
        raise ValueError("trusted RecallOps read gateway requires exact service identities")
    if set(vars(service)) != {"data_dir", "source_mode", "dataset"}:
        raise ValueError("trusted RecallOps traceability service has shadowed capabilities")
    if service.data_dir != settings.data_dir or service.source_mode != settings.source_mode:
        raise ValueError("trusted RecallOps read gateway configuration differs from Settings")
    if type(service.dataset) is not dict or not _is_plain_json(service.dataset):
        raise ValueError("trusted RecallOps read gateway requires a plain validated dataset")
    if service.dataset != trusted_dataset:
        raise ValueError("trusted RecallOps read gateway requires the validated dataset snapshot")


def _close_read_gateway(
    gateway: DirectGateway | StdioMCPGateway,
) -> tuple[
    str,
    _ClosedDirectReads | StdioMCPGateway,
    type[DirectGateway] | type[StdioMCPGateway],
]:
    if type(gateway) is DirectGateway:
        if set(vars(gateway)) != {"registry", "traceability", "operations"}:
            raise ValueError("trusted RecallOps direct gateway has shadowed capabilities")
        if (
            type(gateway.registry) is not RecallRegistryService
            or type(gateway.traceability) is not TraceabilityService
            or type(gateway.operations) is not OperationsService
        ):
            raise ValueError("trusted RecallOps read gateway requires exact service identities")
        registry = gateway.registry
        if registry.http_transport is not None:
            raise ValueError("trusted RecallOps read gateway forbids caller-supplied HTTP transport")
        if set(vars(registry)) != {
            "data_dir",
            "source_mode",
            "http_transport",
            "timeout_seconds",
        }:
            raise ValueError("trusted RecallOps registry service has shadowed capabilities")
        operations = gateway.operations
        if operations._failure_injector is not None or operations._before_cas_hook is not None:
            raise ValueError("trusted RecallOps read gateway forbids caller-supplied operation hook")
        if set(vars(operations)) != {
            "storage_path",
            "source_mode",
            "_failure_injector",
            "_before_cas_hook",
            "traceability",
        }:
            raise ValueError("trusted RecallOps operations service has shadowed capabilities")

        settings = get_settings()
        if (
            registry.data_dir != settings.data_dir
            or registry.source_mode != settings.source_mode
            or type(registry.timeout_seconds) not in {int, float}
            or registry.timeout_seconds != 2.0
            or operations.source_mode != settings.source_mode
        ):
            raise ValueError("trusted RecallOps read gateway configuration differs from Settings")
        # These loaders validate the pinned public snapshot and synthetic manifest
        # before any caller-owned service is retained by a compiled capability.
        load_recall_snapshot(data_dir=settings.data_dir)
        trusted_dataset = load_demo_dataset(settings.data_dir)
        _validate_traceability_service(
            gateway.traceability,
            settings=settings,
            trusted_dataset=trusted_dataset,
        )
        _validate_traceability_service(
            operations.traceability,
            settings=settings,
            trusted_dataset=trusted_dataset,
        )
        closed = _ClosedDirectReads(
            registry=RecallRegistryService(
                data_dir=settings.data_dir,
                source_mode=settings.source_mode,
            ),
            traceability=TraceabilityService(
                data_dir=settings.data_dir,
                source_mode=settings.source_mode,
            ),
        )
        return "direct", closed, DirectGateway
    if type(gateway) is StdioMCPGateway:
        client = gateway.client
        from langchain_mcp_adapters.client import MultiServerMCPClient

        if type(client) is not MultiServerMCPClient:
            raise ValueError("trusted RecallOps stdio gateway requires the official MCP client")
        if client.tool_interceptors or client.tool_name_prefix is not False:
            raise ValueError("trusted RecallOps stdio gateway forbids client interception")
        if set(client.connections) != set(_STDIO_SERVERS):
            raise ValueError("trusted RecallOps stdio server identity set is incomplete")
        for server, module in _STDIO_SERVERS.items():
            connection = client.connections[server]
            if (
                set(connection) != {"transport", "command", "args"}
                or connection.get("transport") != "stdio"
                or connection.get("command") != sys.executable
                or connection.get("args") != ["-m", module]
            ):
                raise ValueError(f"trusted RecallOps stdio server identity mismatch: {server}")
        if set(vars(gateway)) != {"client"}:
            raise ValueError("trusted RecallOps stdio gateway has shadowed capabilities")
        closed = StdioMCPGateway()
        return "stdio", closed, StdioMCPGateway
    raise ValueError("read_gateway must be a trusted RecallOps read gateway")


def _trusted_read_tools(
    gateway: DirectGateway | StdioMCPGateway | None,
) -> tuple[dict[str, BaseTool], dict[str, str]]:
    if gateway is None:
        return {}, {}
    transport, closed_gateway, gateway_type = _close_read_gateway(gateway)

    async def search_recalls(query: str) -> Any:
        """Search official recall registry evidence."""
        return await gateway_type.search_recalls(closed_gateway, query)

    async def get_recall(recall_number: str) -> Any:
        """Get one official recall record by recall number."""
        return await gateway_type.get_recall(closed_gateway, recall_number)

    async def get_product_metadata(upc: str) -> Any:
        """Get official product metadata by UPC."""
        return await gateway_type.get_product_metadata(closed_gateway, upc)

    async def find_candidate_products(predicate: RecallPredicate) -> Any:
        """Find synthetic retailer product candidates for a recall predicate."""
        return await gateway_type.find_candidate_products(closed_gateway, predicate)

    async def match_lots(predicate: RecallPredicate) -> Any:
        """Match synthetic retailer lots to a recall predicate."""
        return await gateway_type.match_lots(closed_gateway, predicate)

    async def trace_forward(lot_id: str) -> Any:
        """Trace a synthetic lot forward through the facility network."""
        return await gateway_type.trace_forward(closed_gateway, lot_id)

    async def trace_backward(lot_id: str) -> Any:
        """Trace a synthetic lot backward to its receiving root."""
        return await gateway_type.trace_backward(closed_gateway, lot_id)

    async def get_inventory(lot_id: str | None = None) -> Any:
        """Read synthetic retailer inventory positions."""
        return await gateway_type.get_inventory(closed_gateway, lot_id)

    async def get_sales(lot_id: str) -> Any:
        """Read synthetic retailer sale events for a lot."""
        return await gateway_type.get_sales(closed_gateway, lot_id)

    async def reconcile_units(lot_id: str) -> Any:
        """Read evidence-backed synthetic unit reconciliation for a lot."""
        return await gateway_type.reconcile_units(closed_gateway, lot_id)

    functions = {
        function.__name__: function
        for function in (
            search_recalls,
            get_recall,
            get_product_metadata,
            find_candidate_products,
            match_lots,
            trace_forward,
            trace_backward,
            get_inventory,
            get_sales,
            reconcile_units,
        )
    }
    tools = {
        name: StructuredTool.from_function(
            coroutine=function,
            name=name,
            description=inspect.getdoc(function) or f"Trusted RecallOps {name} capability.",
        )
        for name, function in functions.items()
    }
    if transport == "direct":
        identities = {
            name: (
                f"direct:reconstructed:{'RecallRegistryService' if name in {'search_recalls', 'get_recall', 'get_product_metadata'} else 'TraceabilityService'}.{name}"
            )
            for name in tools
        }
    else:
        identities = {
            name: (
                f"stdio:reconstructed:{'registry' if name in {'search_recalls', 'get_recall', 'get_product_metadata'} else 'traceability'}:stdio:"
                f"{_STDIO_SERVERS['registry' if name in {'search_recalls', 'get_recall', 'get_product_metadata'} else 'traceability']}:{name}"
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
                raise ValueError(
                    f"compiled {definition.name} capability {name!r} is untrusted"
                )
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
