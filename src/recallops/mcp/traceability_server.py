"""Traceability MCP stdio server (read-only synthetic digital twin)."""

from fastmcp import FastMCP

from recallops.models import (
    CandidateProduct,
    InventoryPosition,
    LotMatch,
    RecallPredicate,
    Reconciliation,
    TraceEvent,
)
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


@mcp.resource("recallops://policy/synthetic-boundary")
def synthetic_boundary() -> str:
    return "SYNTHETIC — ACADEMIC DEMO. Northstar records are not connected to the public recall."


if __name__ == "__main__":
    mcp.run(transport="stdio")
