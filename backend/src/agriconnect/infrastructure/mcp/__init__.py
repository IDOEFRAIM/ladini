from __future__ import annotations

from typing import Any

__all__ = [
    "AgriDBMCPServer",
    "AgriMCPClient",
    "MCPContextServer",
    "MCPProvider",
    "MCPServerApp",
    "MCPToolSpec",
    "ShieldHub",
    "UnifiedMCPClient",
    "mcp",
    "runtime",
]


def __getattr__(name: str) -> Any:
    if name in {"MCPProvider", "MCPServerApp", "MCPToolSpec"}:
        from agriconnect.infrastructure.mcp.base import MCPProvider, MCPServerApp, MCPToolSpec

        return {"MCPProvider": MCPProvider, "MCPServerApp": MCPServerApp, "MCPToolSpec": MCPToolSpec}[name]
    if name in {"AgriMCPClient", "UnifiedMCPClient"}:
        from agriconnect.infrastructure.mcp.client import AgriMCPClient, UnifiedMCPClient

        return {"AgriMCPClient": AgriMCPClient, "UnifiedMCPClient": UnifiedMCPClient}[name]
    if name == "MCPContextServer":
        from agriconnect.infrastructure.mcp.context import MCPContextServer

        return MCPContextServer
    if name in {"AgriDBMCPServer", "mcp", "runtime"}:
        from agriconnect.infrastructure.mcp.runtime import AgriDBMCPServer, mcp, runtime

        return {"AgriDBMCPServer": AgriDBMCPServer, "mcp": mcp, "runtime": runtime}[name]
    if name == "ShieldHub":
        from agriconnect.infrastructure.mcp.security import ShieldHub

        return ShieldHub
    raise AttributeError(name)
