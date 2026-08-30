"""Recall Registry MCP stdio server (read-only)."""

from fastmcp import FastMCP

from recallops.mcp.common import jsonable
from recallops.services.recall_registry import RecallRegistryService

mcp = FastMCP("Recall Registry MCP", instructions="Read-only frozen openFDA recall registry.")
service = RecallRegistryService()


@mcp.tool()
def search_recalls(query: str) -> list[dict]:
    """Search the pinned official recall notice."""
    return [jsonable(record) for record in service.search_recalls(query)]


@mcp.tool()
def get_recall(recall_number: str) -> dict | None:
    """Get an official frozen recall record by recall number."""
    record = service.get_recall(recall_number)
    return jsonable(record) if record else None


@mcp.tool()
def get_product_metadata(upc: str) -> dict | None:
    """Return recall-source UPC metadata when the UPC appears in the public notice."""
    return service.get_product_metadata(upc)


@mcp.resource("recallops://policy/provenance")
def provenance_policy() -> str:
    return "Official openFDA snapshot only; checksum validation is mandatory."


if __name__ == "__main__":
    mcp.run(transport="stdio")
