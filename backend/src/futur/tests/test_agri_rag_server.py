import asyncio
import json

import pytest


def test_search_agronomy_docs_returns_payload_shape():
    from agriconnect.protocols.mcp.servers.rag_server import AgriRAGMCPServer

    server = AgriRAGMCPServer()
    res = server.call_tool_sync("search_agronomy_docs", {"query": "test query"})
    assert res.get("ok") is True
    data = res.get("data")
    assert isinstance(data, dict)
    assert data.get("query") == "test query"
    assert isinstance(data.get("documents"), list)
    assert isinstance(data.get("total_found"), int)
    assert data.get("total_found") == len(data.get("documents", []))


def test_search_past_interactions_returns_empty_list_when_no_retriever():
    from agriconnect.protocols.mcp.servers.rag_server import AgriRAGMCPServer

    server = AgriRAGMCPServer()
    res = server.call_tool_sync("search_past_interactions", {"user_id": "u1", "query": "foo"})
    assert res.get("ok") is True
    data = res.get("data")
    assert isinstance(data, list)
    assert data == []


def test_rag_server_status_async_callable():
    from agriconnect.protocols.mcp.servers.rag_server import rag_server_status

    # run the async resource and parse its JSON result
    result = asyncio.run(rag_server_status())
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed.get("status") == "ok"
    assert isinstance(parsed.get("retriever_loaded"), bool)
