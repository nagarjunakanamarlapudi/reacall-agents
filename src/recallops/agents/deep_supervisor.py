"""Optional live Deep Agents supervisor with fixed, read-only specialist scopes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.middleware import FilesystemMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from recallops.agents.prompts import (
    CONTAINMENT_COMMUNICATIONS_PROMPT,
    PRODUCT_LOT_MATCHING_PROMPT,
    RECALL_INTELLIGENCE_PROMPT,
    SUPERVISOR_PROMPT,
    TRACEABILITY_RECONCILIATION_PROMPT,
)

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


class SpecialistDefinition(BaseModel):
    name: str
    description: str
    system_prompt: str
    allowed_tool_names: list[str]


@dataclass(frozen=True)
class DeepSupervisor:
    """The compiled graph plus an explicit, UI-friendly safety manifest."""

    graph: Any
    specialist_names: list[str]
    planning_tool_name: str
    delegation_tool_name: str
    operational_write_tool_names: list[str]
    exposed_read_tool_names: list[str]


def specialist_catalog() -> list[SpecialistDefinition]:
    """Return the fixed roles and their least-privilege read-tool allowlists."""
    return [
        SpecialistDefinition(
            name="recall-intelligence",
            description="Extract official recall scope and citations.",
            system_prompt=RECALL_INTELLIGENCE_PROMPT,
            allowed_tool_names=["search_recalls", "get_recall", "get_product_metadata"],
        ),
        SpecialistDefinition(
            name="product-lot-matching",
            description="Classify synthetic products and lots with field-level rationale.",
            system_prompt=PRODUCT_LOT_MATCHING_PROMPT,
            allowed_tool_names=["find_candidate_products", "match_lots"],
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
        ),
        SpecialistDefinition(
            name="containment-communications",
            description="Draft cited containment and internal communications without writes.",
            system_prompt=CONTAINMENT_COMMUNICATIONS_PROMPT,
            allowed_tool_names=[],
        ),
    ]


def _tool_name(tool: BaseTool | Callable[..., Any] | dict[str, Any]) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    if isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str):
            return name
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
        raise ValueError("tool dictionary lacks a name")
    name = getattr(tool, "__name__", None)
    if not isinstance(name, str):
        raise ValueError("tool callable lacks a stable name")
    return name


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


def build_deep_supervisor(
    *,
    model: str | BaseChatModel,
    read_tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] = (),
    middleware: Sequence[Any] = (),
) -> DeepSupervisor:
    """Build a real Deep Agents graph without invoking the model or any provider.

    The explicit outer LangGraph remains operational authority. This optional
    reasoning graph receives read tools only; no Operations MCP write can be
    delegated or called from the supervisor.
    """
    tools_by_name: dict[str, BaseTool | Callable[..., Any] | dict[str, Any]] = {}
    for read_tool in read_tools:
        name = _tool_name(read_tool)
        if name in OPERATIONAL_WRITE_TOOL_NAMES:
            raise ValueError(f"operational write tool {name!r} is forbidden in the supervisor")
        if name in tools_by_name:
            raise ValueError(f"duplicate read tool {name!r}")
        tools_by_name[name] = read_tool

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
    subagents: list[dict[str, Any]] = []
    for definition in specialist_catalog():
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
                "middleware": [FilesystemMiddleware(tools=["read_file", "ls"])],
            }
        )
    graph = create_deep_agent(
        model=model,
        tools=[],
        system_prompt=SUPERVISOR_PROMPT,
        middleware=[*middleware, TodoListMiddleware(), read_only_filesystem],
        subagents=subagents,
        name="recallops-supervisor",
    )
    return DeepSupervisor(
        graph=graph,
        specialist_names=[item.name for item in specialist_catalog()],
        planning_tool_name="write_todos",
        delegation_tool_name="task",
        operational_write_tool_names=[],
        exposed_read_tool_names=sorted(tools_by_name),
    )
