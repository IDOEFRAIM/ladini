"""ShieldHub — central MCP gateway with async manager + dynamic registry."""
from __future__ import annotations

from typing import Any, Dict

from agriconnect.protocols.mcp.security.client_base import MCPPermissionClient
from agriconnect.protocols.mcp.security.client_app import MCPSessionManager
from agriconnect.protocols.mcp.security.constants import PermissionScope
from agriconnect.protocols.mcp.security.mcp_manager import MCPManager
from agriconnect.protocols.mcp.security.mcp_registry import MCPToolRegistry, get_registry


class _ManagerBackendAdapter:
    """Adapter exposing the interface expected by MCPPermissionClient."""

    def __init__(self, manager: MCPManager, registry: MCPToolRegistry) -> None:
        self._manager = manager
        self._registry = registry

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        return await self._manager.call_tool(name, arguments)

    def list_tools(self) -> list[dict]:
        return self._registry.list_tools()


class ShieldHub:
    """Central hub used by API routes to call MCP tools safely."""

    def __init__(self, session_id: str = "unknown") -> None:
        self.registry = get_registry()
        self.manager = MCPManager(registry=self.registry)

        adapter = _ManagerBackendAdapter(self.manager, self.registry)
        self._shield = MCPPermissionClient(
            backend=adapter,
            session_id=session_id,
            registry=self.registry,
        )
        self.session = MCPSessionManager(host=self._shield, session_id=session_id)

    async def call(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        await self.manager.ensure_ready()
        meta = self.registry.get_tool(tool_name)
        if meta is None:
            raise ValueError(f"Unknown MCP tool: {tool_name}")

        if meta.scope == PermissionScope.DB_READ_ONLY:
            return await self.session.safe_read(tool_name, arguments)
        return await self.session.execute(tool_name, arguments)

    async def list_tools(self) -> dict[str, list[dict]]:
        return await self.manager.list_tools()
