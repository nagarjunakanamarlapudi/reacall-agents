import sys
from uuid import uuid4

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient


def _connection(module: str) -> dict[str, object]:
    return {"transport": "stdio", "command": sys.executable, "args": ["-m", module]}


@pytest.mark.asyncio
async def test_registry_stdio_server_discovers_and_invokes_read_tool() -> None:
    client = MultiServerMCPClient({"registry": _connection("recallops.mcp.recall_registry_server")})
    tools = await client.get_tools()
    tool = next(item for item in tools if item.name == "search_recalls")
    response = await tool.ainvoke({"query": "Salmonella"})

    assert "H-1230-2026" in str(response)


@pytest.mark.asyncio
async def test_operations_stdio_server_discovers_and_invokes_approved_simulated_write() -> None:
    client = MultiServerMCPClient({"operations": _connection("recallops.mcp.operations_server")})
    tools = await client.get_tools()
    tool = next(item for item in tools if item.name == "create_case")
    request_id = uuid4().hex
    response = await tool.ainvoke(
        {
            "case_id": f"CASE-STDIO-{request_id}",
            "recall_number": "H-1230-2026",
            "decision": "approve",
            "actor": "food-safety-manager",
            "justification": "integration test approval",
            "expected_case_version": 0,
            "idempotency_key": f"stdio-create-{request_id}",
        }
    )

    assert "simulated" in str(response)
