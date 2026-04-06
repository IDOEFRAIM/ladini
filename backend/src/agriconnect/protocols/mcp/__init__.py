from __future__ import annotations

import asyncio
import json
from typing import Any


class MCPRagServer:
    """Compatibility wrapper exposing legacy RAG methods over AgronomyTools."""

    def __init__(self, *args, **kwargs):
        from agriconnect.protocols.mcp.tools.agronomy import AgronomyTools

        self._provider = AgronomyTools(*args, **kwargs)

    async def search(self, query: str, limit: int = 3):
        raw = await self._provider.search_agronomy_docs(query=query, top_k=limit)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed.get("documents", []) if isinstance(parsed, dict) else []

    async def search_past_interactions(self, user_id: str, query: str, top_k: int = 3):
        return await self._provider.search_past_interactions(user_id=user_id, query=query, top_k=top_k)

    def list_tools(self):
        return [{"name": spec.name, "description": spec.description} for spec in self._provider.get_tools()]

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        handlers = {
            "search_agronomy_docs": self._provider.search_agronomy_docs,
            "search_past_interactions": self._provider.search_past_interactions,
        }
        fn = handlers.get(name)
        if fn is None:
            return {"ok": False, "error": f"Tool {name} inconnu"}

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            raw = loop.run_until_complete(fn(**arguments))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass

        return {"ok": True, "data": json.loads(raw) if isinstance(raw, str) else raw}


class MCPDatabaseServer:
    """Lazy DB server proxy to avoid heavy imports at module import time."""

    def __init__(self, *args, **kwargs) -> None:
        _ = args, kwargs
        self._inner = None

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        _ = name, arguments
        return {"ok": False, "error": "DB runtime unavailable in lightweight compatibility mode"}


def get_mcp_db_server(session_factory=None):
    _ = session_factory
    return MCPDatabaseServer()


def get_mcp_rag_server():
    return MCPRagServer()


def get_mcp_weather_server(llm_client=None):
    from agriconnect.protocols.mcp.tools.weather import MCPWeatherServer

    return MCPWeatherServer(llm_client=llm_client)


def get_mcp_context_server(session_factory=None, context_optimizer=None, llm_client=None):
    from agriconnect.infrastructure.mcp.context import MCPContextServer

    if context_optimizer:
        return MCPContextServer(context_optimizer)
    return MCPContextServer(session_factory=session_factory, llm_client=llm_client)


def get_mcp_units_server():
    from agriconnect.protocols.mcp.tools.units import UnitsMCPServer

    return UnitsMCPServer()


__all__ = [
    "MCPDatabaseServer",
    "MCPRagServer",
    "MCPWeatherServer",
    "MCPContextServer",
    "UnitsMCPServer",
    "get_mcp_db_server",
    "get_mcp_rag_server",
    "get_mcp_weather_server",
    "get_mcp_context_server",
    "get_mcp_units_server",
]


def __getattr__(name: str) -> Any:
    if name == "MCPContextServer":
        from agriconnect.infrastructure.mcp.context import MCPContextServer

        return MCPContextServer
    if name == "MCPWeatherServer":
        from agriconnect.protocols.mcp.tools.weather import MCPWeatherServer

        return MCPWeatherServer
    if name == "UnitsMCPServer":
        from agriconnect.protocols.mcp.tools.units import UnitsMCPServer

        return UnitsMCPServer
    raise AttributeError(name)
