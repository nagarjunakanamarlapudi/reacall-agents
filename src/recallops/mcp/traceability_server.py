"""Traceability MCP stdio server (read-only synthetic digital twin)."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field, StrictInt

from recallops.models import (
    CandidateProduct,
    InventoryPosition,
    LotMatch,
    RecallPredicate,
    Reconciliation,
    TraceEvent,
)
from recallops.retrieval.models import HybridSearchResponse
from recallops.services.traceability import TraceabilityService

mcp = FastMCP("Traceability MCP", instructions="Read-only Northstar synthetic digital twin.")
service = TraceabilityService()


@mcp.tool()
def find_candidate_products(predicate: RecallPredicate) -> list[CandidateProduct]:
    return [
        CandidateProduct.model_validate(item) for item in service.find_candidate_products(predicate)
    ]


@mcp.tool()
def match_lots(predicate: RecallPredicate) -> list[LotMatch]:
    return [LotMatch.model_validate(item) for item in service.match_lots(predicate)]


@mcp.tool()
def trace_forward(lot_id: str) -> list[TraceEvent]:
    return [TraceEvent.model_validate(item) for item in service.trace_forward(lot_id)]


@mcp.tool()
def trace_backward(lot_id: str) -> list[TraceEvent]:
    return [TraceEvent.model_validate(item) for item in service.trace_backward(lot_id)]


@mcp.tool()
def get_inventory(lot_id: str | None = None) -> list[InventoryPosition]:
    return [InventoryPosition.model_validate(item) for item in service.get_inventory(lot_id)]


@mcp.tool()
def get_sales(lot_id: str) -> list[TraceEvent]:
    return [TraceEvent.model_validate(item) for item in service.get_sales(lot_id)]


@mcp.tool()
def reconcile_units(lot_id: str) -> Reconciliation:
    return service.reconcile_units(lot_id)


@mcp.tool()
def search_operational_evidence(
    query: Annotated[str, Field(min_length=1)],
    top_k: Annotated[StrictInt, Field(ge=1, le=20)] = 8,
    record_types: tuple[str, ...] = (),
) -> HybridSearchResponse:
    """Run bounded BM25 plus local-LSA search over synthetic operational evidence only."""

    return service.hybrid_search(query, top_k=top_k, record_types=record_types)


@mcp.resource("recallops://policy/synthetic-boundary")
def synthetic_boundary() -> str:
    return "SYNTHETIC — ACADEMIC DEMO. Northstar records are not connected to the public recall."


if __name__ == "__main__":
    mcp.run(transport="stdio")
