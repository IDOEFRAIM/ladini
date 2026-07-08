"""Abstractions for executing tools behind MarketCoach actions.

This module decouples the action registry and handlers from the concrete
backend used to execute tools (MCP, REST, local, mocks, ...).

The default implementation, ``MCPToolProvider``, delegates to the
existing ``MarketRuntime.call_db`` API to preserve behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class ToolProvider(Protocol):
    """Abstract provider capable of executing a named tool.

    Implementations may route to MCP, REST, local functions, mocks, etc.
    The interface is intentionally minimal to avoid coupling.
    """

    async def execute(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - interface
        """Execute *tool_name* with *args* and return a JSON-like result."""


@dataclass
class MCPToolProvider:
    """ToolProvider backed by the existing MarketRuntime DB MCP client.

    This is a thin adapter around ``MarketRuntime.call_db`` so that the
    executor can depend on ``ToolProvider`` instead of a concrete MCP
    client, while keeping the current behaviour unchanged.
    """

    runtime: Any

    async def execute(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        # MarketRuntime.call_db already returns a JSON-like structure.
        return await self.runtime.call_db(tool_name, **args)


__all__ = ["ToolProvider", "MCPToolProvider"]
