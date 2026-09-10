from __future__ import annotations

from typing import Any

__all__ = [
    "AgriDBMCPServer",
    "AgriMCPClient",
    "MCPContextServer",
    "MCPProvider",
    "MCPServerApp",
    "MCPToolSpec",
    "MCPTransportConfig",
    "mcp",
    "runtime",
]


def __getattr__(name: str) -> Any:
    if name in {"MCPProvider", "MCPServerApp", "MCPToolSpec"}:
        from ladini.infrastructure.mcp.base import (
            MCPProvider,
            MCPServerApp,
            MCPToolSpec,
        )

        return {
            "MCPProvider": MCPProvider,
            "MCPServerApp": MCPServerApp,
            "MCPToolSpec": MCPToolSpec,
        }[name]
    if name in {"AgriMCPClient", "MCPTransportConfig"}:
        from ladini.infrastructure.mcp.client import (
            AgriMCPClient,
            MCPTransportConfig,
        )

        return {
            "AgriMCPClient": AgriMCPClient,
            "MCPTransportConfig": MCPTransportConfig,
        }[name]
    if name == "MCPContextServer":
        from ladini.infrastructure.mcp.context import MCPContextServer

        return MCPContextServer
    if name in {"AgriDBMCPServer", "mcp", "runtime"}:
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer, mcp, runtime

        return {"AgriDBMCPServer": AgriDBMCPServer, "mcp": mcp, "runtime": runtime}[
            name
        ]
    raise AttributeError(name)
