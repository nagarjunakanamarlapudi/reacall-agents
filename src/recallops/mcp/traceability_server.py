"""Traceability MCP stdio server (read-only synthetic digital twin)."""

from fastmcp import FastMCP

from recallops.mcp.common import jsonable
from recallops.models import RecallPredicate
from recallops.services.traceability import TraceabilityService

mcp = FastMCP("Traceability MCP", instructions="Read-only Northstar synthetic digital twin.")
service = TraceabilityService()


def _predicate(payload: dict) -> RecallPredicate:
    return RecallPredicate.model_validate(payload)


@mcp.tool()
def find_candidate_products(predicate: dict) -> list[dict]:
    return service.find_candidate_products(_predicate(predicate))


@mcp.tool()
def match_lots(predicate: dict) -> list[dict]:
    return service.match_lots(_predicate(predicate))


@mcp.tool()
def trace_forward(lot_id: str) -> list[dict]:
    return service.trace_forward(lot_id)


@mcp.tool()
def trace_backward(lot_id: str) -> list[dict]:
    return service.trace_backward(lot_id)


@mcp.tool()
def get_inventory(lot_id: str | None = None) -> list[dict]:
    return service.get_inventory(lot_id)


@mcp.tool()
def get_sales(lot_id: str) -> list[dict]:
    return service.get_sales(lot_id)


@mcp.tool()
def reconcile_units(lot_id: str) -> dict:
    return jsonable(service.reconcile_units(lot_id))


@mcp.resource("recallops://policy/synthetic-boundary")
def synthetic_boundary() -> str:
    return "SYNTHETIC — ACADEMIC DEMO. Northstar records are not connected to the public recall."


if __name__ == "__main__":
    mcp.run(transport="stdio")
