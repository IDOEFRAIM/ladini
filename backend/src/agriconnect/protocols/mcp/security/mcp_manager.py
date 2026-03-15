from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Protocol

from .mcp_registry import MCPToolRegistry, get_registry
from .schemas import MCPServerKind

logger = logging.getLogger("MCP.Manager")


class AsyncMCPBackend(Protocol):
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        ...

    async def list_tools(self) -> list[dict[str, Any]]:
        ...


class RAGInProcessBackend:
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        from agriconnect.protocols.mcp.servers.agri_rag_server import (
            search_agronomy_docs,
            search_past_interactions,
            rag_server_status,
        )

        tool_map = {
            "search_agronomy_docs": search_agronomy_docs,
            "search_past_interactions": search_past_interactions,
            "rag://status": rag_server_status,
        }
        fn = tool_map.get(name)
        if fn is None:
            raise ValueError(f"Unknown RAG tool: {name}")
        raw = await fn(**arguments)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {"result": raw}
        return raw

    async def list_tools(self) -> list[dict[str, Any]]:
        from agriconnect.protocols.mcp.servers.agri_rag_server import AgriRAGMCPServer

        try:
            return AgriRAGMCPServer().list_tools()
        except Exception:
            logger.exception("RAG tool discovery failed")
            return []


class DBInProcessBackend:
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        import agriconnect.protocols.mcp.tools.db_tools as db_tools

        tool_map = getattr(db_tools, "TOOL_MAP", None)
        if not isinstance(tool_map, dict):
            raise RuntimeError("DB TOOL_MAP is not available")
        fn = tool_map.get(name)
        if fn is None:
            raise ValueError(f"Unknown DB tool: {name}")
        raw = await fn(**arguments)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {"result": raw}
        return raw

    async def list_tools(self) -> list[dict[str, Any]]:
        from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer

        try:
            return AgriDBMCPServer.list_tools()
        except Exception:
            logger.exception("DB tool discovery failed")
            return []


class MCPManager:
    """Async router/manager for MCP backends with timeout/retry support."""

    def __init__(self, registry: MCPToolRegistry | None = None) -> None:
        self.registry = registry or get_registry()
        self._backends: dict[MCPServerKind, AsyncMCPBackend] = {
            MCPServerKind.RAG: RAGInProcessBackend(),
            MCPServerKind.DB: DBInProcessBackend(),
        }
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            rag_tools = await self._backends[MCPServerKind.RAG].list_tools()
            db_tools = await self._backends[MCPServerKind.DB].list_tools()
            self.registry.sync_discovered_tools(MCPServerKind.RAG, rag_tools)
            self.registry.sync_discovered_tools(MCPServerKind.DB, db_tools)
            self.registry.apply_overrides()
            self._ready = True

    def backend_for_server(self, server: MCPServerKind) -> AsyncMCPBackend:
        return self._backends[server]

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        await self.ensure_ready()
        arguments = arguments or {}
        meta = self.registry.get_tool(tool_name)
        if meta is None:
            raise ValueError(f"Unknown tool '{tool_name}' (not discovered/registered)")

        backend = self._backends[meta.server]
        last_error: Exception | None = None
        attempts = 1 + max(0, int(meta.retries))

        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(
                    backend.call_tool(tool_name, arguments),
                    timeout=float(meta.timeout_seconds),
                )
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = min(2.0, 0.2 * (2 ** (attempt - 1)))
                logger.warning(
                    "MCP call retry | tool=%s | attempt=%s/%s | err=%s",
                    tool_name,
                    attempt,
                    attempts,
                    exc,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"MCP call failed for '{tool_name}': {last_error}")

    async def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        await self.ensure_ready()
        return {
            "rag_tools": self.registry.list_tools(MCPServerKind.RAG),
            "db_tools": self.registry.list_tools(MCPServerKind.DB),
        }
