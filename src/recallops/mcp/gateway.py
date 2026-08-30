"""Direct and real-stdio adapters with the same asynchronous tool surface."""

from __future__ import annotations

import json
import sys
from typing import Any, Protocol

from langchain_mcp_adapters.client import MultiServerMCPClient

from recallops.models import (
    ApprovalDecision,
    CandidateProduct,
    InventoryPosition,
    LotMatch,
    ProductMetadata,
    RecallPredicate,
    TraceEvent,
)
from recallops.services.operations import OperationsService
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService


class Gateway(Protocol):
    """Async JSON-compatible contract shared by direct and stdio adapters."""

    async def search_recalls(self, query: str) -> list[dict[str, Any]]: ...
    async def get_recall(self, recall_number: str) -> dict[str, Any] | None: ...
    async def get_product_metadata(self, upc: str) -> dict[str, Any] | None: ...
    async def find_candidate_products(self, predicate: RecallPredicate) -> list[dict[str, Any]]: ...
    async def match_lots(self, predicate: RecallPredicate) -> list[dict[str, Any]]: ...
    async def trace_forward(self, lot_id: str) -> list[dict[str, Any]]: ...
    async def trace_backward(self, lot_id: str) -> list[dict[str, Any]]: ...
    async def get_inventory(self, lot_id: str | None = None) -> list[dict[str, Any]]: ...
    async def get_sales(self, lot_id: str) -> list[dict[str, Any]]: ...
    async def reconcile_units(self, lot_id: str) -> dict[str, Any]: ...
    async def create_case(self, **kwargs: Any) -> dict[str, Any]: ...
    async def apply_inventory_hold(self, **kwargs: Any) -> dict[str, Any]: ...
    async def create_facility_tasks(self, **kwargs: Any) -> dict[str, Any]: ...
    async def record_acknowledgment(self, **kwargs: Any) -> dict[str, Any]: ...
    async def record_disposition(self, **kwargs: Any) -> dict[str, Any]: ...
    async def close_case(self, **kwargs: Any) -> dict[str, Any]: ...


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


class DirectGateway:
    def __init__(
        self,
        registry: RecallRegistryService | None = None,
        traceability: TraceabilityService | None = None,
        operations: OperationsService | None = None,
    ) -> None:
        self.registry = registry or RecallRegistryService()
        self.traceability = traceability or TraceabilityService()
        self.operations = operations or OperationsService()

    async def search_recalls(self, query: str):
        return _json(self.registry.search_recalls(query))

    async def get_recall(self, recall_number: str):
        return _json(self.registry.get_recall(recall_number))

    async def get_product_metadata(self, upc: str):
        value = self.registry.get_product_metadata(upc)
        return _json(ProductMetadata.model_validate(value)) if value else None

    async def find_candidate_products(self, predicate: RecallPredicate):
        return _json(
            [
                CandidateProduct.model_validate(item)
                for item in self.traceability.find_candidate_products(predicate)
            ]
        )

    async def match_lots(self, predicate: RecallPredicate):
        return _json(
            [LotMatch.model_validate(item) for item in self.traceability.match_lots(predicate)]
        )

    async def trace_forward(self, lot_id: str):
        return _json(
            [TraceEvent.model_validate(item) for item in self.traceability.trace_forward(lot_id)]
        )

    async def trace_backward(self, lot_id: str):
        return _json(
            [TraceEvent.model_validate(item) for item in self.traceability.trace_backward(lot_id)]
        )

    async def get_inventory(self, lot_id: str | None = None):
        return _json(
            [
                InventoryPosition.model_validate(item)
                for item in self.traceability.get_inventory(lot_id)
            ]
        )

    async def get_sales(self, lot_id: str):
        return _json(
            [TraceEvent.model_validate(item) for item in self.traceability.get_sales(lot_id)]
        )

    async def reconcile_units(self, lot_id: str):
        return _json(self.traceability.reconcile_units(lot_id))

    async def create_case(self, **kwargs: Any):
        return _json(self.operations.create_case(**kwargs))

    async def apply_inventory_hold(self, **kwargs: Any):
        return _json(self.operations.apply_inventory_hold(**kwargs))

    async def create_facility_tasks(self, **kwargs: Any):
        return _json(self.operations.create_facility_tasks(**kwargs))

    async def record_acknowledgment(self, **kwargs: Any):
        return _json(self.operations.record_acknowledgment(**kwargs))

    async def record_disposition(self, **kwargs: Any):
        return _json(self.operations.record_disposition(**kwargs))

    async def close_case(self, **kwargs: Any):
        return _json(self.operations.close_case(**kwargs))


class StdioMCPGateway:
    def __init__(self, connections: dict[str, dict[str, object]] | None = None) -> None:
        self.client = MultiServerMCPClient(
            connections
            or {
                "registry": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "recallops.mcp.recall_registry_server"],
                },
                "traceability": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "recallops.mcp.traceability_server"],
                },
                "operations": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "recallops.mcp.operations_server"],
                },
            }
        )

    async def _call(self, server: str, name: str, payload: dict[str, Any]) -> Any:
        tool = next(
            item for item in await self.client.get_tools(server_name=server) if item.name == name
        )
        result = await tool.ainvoke(payload)
        content = result.content if hasattr(result, "content") else result
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = content[0].get("text")
            if isinstance(text, str):
                return json.loads(text)
        if isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content
        return content

    async def search_recalls(self, query: str):
        return await self._call("registry", "search_recalls", {"query": query})

    async def get_recall(self, recall_number: str):
        return await self._call("registry", "get_recall", {"recall_number": recall_number})

    async def get_product_metadata(self, upc: str):
        return await self._call("registry", "get_product_metadata", {"upc": upc})

    async def find_candidate_products(self, predicate: RecallPredicate):
        return await self._call(
            "traceability", "find_candidate_products", {"predicate": predicate.model_dump()}
        )

    async def match_lots(self, predicate: RecallPredicate):
        return await self._call("traceability", "match_lots", {"predicate": predicate.model_dump()})

    async def trace_forward(self, lot_id: str):
        return await self._call("traceability", "trace_forward", {"lot_id": lot_id})

    async def trace_backward(self, lot_id: str):
        return await self._call("traceability", "trace_backward", {"lot_id": lot_id})

    async def get_inventory(self, lot_id: str | None = None):
        return await self._call("traceability", "get_inventory", {"lot_id": lot_id})

    async def get_sales(self, lot_id: str):
        return await self._call("traceability", "get_sales", {"lot_id": lot_id})

    async def reconcile_units(self, lot_id: str):
        return await self._call("traceability", "reconcile_units", {"lot_id": lot_id})

    async def create_case(self, *, approval: ApprovalDecision, **kwargs: Any):
        return await self._write("create_case", approval, kwargs)

    async def apply_inventory_hold(self, *, approval: ApprovalDecision, **kwargs: Any):
        return await self._write("apply_inventory_hold", approval, kwargs)

    async def create_facility_tasks(self, *, approval: ApprovalDecision, **kwargs: Any):
        return await self._write("create_facility_tasks", approval, kwargs)

    async def record_acknowledgment(self, *, approval: ApprovalDecision, **kwargs: Any):
        return await self._write("record_acknowledgment", approval, kwargs)

    async def record_disposition(self, *, approval: ApprovalDecision, **kwargs: Any):
        return await self._write("record_disposition", approval, kwargs)

    async def close_case(self, *, approval: ApprovalDecision, **kwargs: Any):
        return await self._write("close_case", approval, kwargs)

    async def _write(self, name: str, approval: ApprovalDecision, kwargs: dict[str, Any]):
        return await self._call(
            "operations",
            name,
            {
                **_json(kwargs),
                "decision": approval.decision,
                "actor": approval.actor,
                "justification": approval.justification,
                "approved_at": approval.approved_at.isoformat(),
                "action_ids": approval.action_ids,
            },
        )
