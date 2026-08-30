"""Recall Registry MCP stdio server (read-only)."""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field, StrictInt

from recallops.models import ProductMetadata, RecallRecord
from recallops.retrieval.models import HybridSearchResponse
from recallops.services.recall_registry import RecallRegistryService

mcp = FastMCP("Recall Registry MCP", instructions="Read-only frozen openFDA recall registry.")
service = RecallRegistryService()


@mcp.tool()
def search_recalls(query: str) -> list[RecallRecord]:
    """Search the pinned official recall notice."""
    return service.search_recalls(query)


@mcp.tool()
def get_recall(recall_number: str) -> RecallRecord | None:
    """Get an official frozen recall record by recall number."""
    record = service.get_recall(recall_number)
    return record


@mcp.tool()
def get_product_metadata(upc: str) -> ProductMetadata | None:
    """Return recall-source UPC metadata when the UPC appears in the public notice."""
    metadata = service.get_product_metadata(upc)
    return ProductMetadata.model_validate(metadata) if metadata else None


@mcp.tool()
def search_regulatory_evidence(
    query: Annotated[str, Field(min_length=1)],
    top_k: Annotated[StrictInt, Field(ge=1, le=20)] = 8,
    record_types: tuple[str, ...] = (),
) -> HybridSearchResponse:
    """Run bounded BM25 plus local-LSA search over official evidence only."""

    return service.hybrid_search(query, top_k=top_k, record_types=record_types)


@mcp.resource("recallops://policy/provenance")
def provenance_policy() -> str:
    return "Official openFDA snapshot only; checksum validation is mandatory."


if __name__ == "__main__":
    mcp.run(transport="stdio")
