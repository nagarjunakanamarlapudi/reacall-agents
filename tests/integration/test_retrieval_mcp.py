import os
import sys
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import ValidationError

from recallops.mcp.gateway import DirectGateway, StdioMCPGateway
from recallops.retrieval.agentic import AgenticRetriever
from recallops.retrieval.models import HybridSearchResponse


def _connections() -> dict[str, dict[str, object]]:
    environment = {
        **os.environ,
        "RECALLOPS_DATA_DIR": str(Path("data").resolve()),
        "RECALLOPS_SOURCE_MODE": "snapshot",
    }

    def connection(module: str) -> dict[str, object]:
        return {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", module],
            "env": environment,
        }

    return {
        "registry": connection("recallops.mcp.recall_registry_server"),
        "traceability": connection("recallops.mcp.traceability_server"),
    }


@pytest.mark.asyncio
async def test_hybrid_tools_have_bounded_typed_discovery_schemas() -> None:
    tools = {tool.name: tool for tool in await MultiServerMCPClient(_connections()).get_tools()}

    assert {"search_regulatory_evidence", "search_operational_evidence"} <= set(tools)
    for name in ("search_regulatory_evidence", "search_operational_evidence"):
        schema = tools[name].args_schema
        assert schema["properties"]["top_k"]["minimum"] == 1
        assert schema["properties"]["top_k"]["maximum"] == 20
        assert schema["properties"]["query"]["minLength"] == 1
        assert schema["properties"]["record_types"]["type"] == "array"
        assert "query" in schema["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "query", "expected_source", "expected_id"),
    [
        (
            "search_regulatory_evidence",
            "H-1230-2026 possible Salmonella",
            "official",
            "H-1230-2026",
        ),
        (
            "search_operational_evidence",
            "LOT-BG-042-03",
            "synthetic",
            "LOT-BG-042-03",
        ),
    ],
)
async def test_direct_and_real_stdio_hybrid_search_have_exact_result_parity(
    method: str,
    query: str,
    expected_source: str,
    expected_id: str,
) -> None:
    direct = DirectGateway()
    stdio = StdioMCPGateway(_connections())
    kwargs = {"query": query, "top_k": 6, "record_types": ()}

    direct_result = HybridSearchResponse.model_validate(await getattr(direct, method)(**kwargs))
    stdio_result = HybridSearchResponse.model_validate(await getattr(stdio, method)(**kwargs))

    assert direct_result == stdio_result
    assert direct_result.source_filter == expected_source
    assert {item.document.source_class for item in direct_result.results} == {expected_source}
    assert expected_id in {item.document.record_id for item in direct_result.results}


@pytest.mark.asyncio
async def test_agentic_retriever_consumes_the_common_direct_gateway_contract() -> None:
    retriever = AgenticRetriever(DirectGateway())
    result = await retriever.retrieve("What does FDA Class I mean for LOT-BG-042-03?")

    assert result.coverage_satisfied is True
    assert {item.tool_name for item in result.tool_trace} == {
        "search_regulatory_evidence",
        "search_operational_evidence",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [0, 21, True, 1.0])
async def test_direct_hybrid_gateway_rejects_invalid_top_k(top_k: object) -> None:
    with pytest.raises((ValidationError, ValueError)):
        await DirectGateway().search_regulatory_evidence("recall", top_k=top_k, record_types=())
