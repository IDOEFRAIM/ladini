"""Base class for all AgriConnect async MCP micro-servers.

Design rules shared by every server:
  - All tool handlers are *async* (asyncio-native).
  - Each handler enforces a 5-second default timeout.
  - Handlers return typed Pydantic models (no raw dicts).
  - No prose: servers are data-only; formatting is the agent's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

from pydantic import BaseModel

logger = logging.getLogger("MCP.BaseServer")

# ────────────────────────────── Shared models ──────────────────────────────

TOOL_TIMEOUT_SECONDS: int = 5


class MCPError(BaseModel):
    code: str
    message: str
    detail: Optional[str] = None


class MCPToolResult(BaseModel):
    """Wrapper returned by every tool call."""
    ok: bool
    data: Optional[Any] = None
    error: Optional[MCPError] = None
    duration_ms: Optional[float] = None


# ────────────────────────────── Base server ────────────────────────────────

class AsyncMCPServer:
    """Minimal async MCP server base-class.

    Sub-class this and decorate methods with ``@self.tool(name, description, schema)``
    inside ``_register_tools()``.  The ``call_tool`` coroutine dispatches to the
    right handler, measures latency and enforces a timeout.
    """

    name: str = "base"

    def __init__(self) -> None:
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._register_tools()
        logger.info("🔌 %s MCP Server ready (%d tools)", self.name, len(self._tools))

    # ── Registration API ──────────────────────────────────────────────────

    def register(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[..., Coroutine[Any, Any, BaseModel]],
        timeout: int = TOOL_TIMEOUT_SECONDS,
    ) -> None:
        """Register an async tool handler."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "handler": handler,
            "timeout": timeout,
        }

    def _register_tools(self) -> None:
        """Override in sub-classes to call ``self.register(...)``."""

    # ── MCP interface ─────────────────────────────────────────────────────

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
            }
            for t in self._tools.values()
        ]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> MCPToolResult:
        """Dispatch to a handler with timeout enforcement."""
        tool = self._tools.get(name)
        if not tool:
            return MCPToolResult(ok=False, error=MCPError(code="TOOL_NOT_FOUND", message=f"Unknown tool: {name}"))

        handler = tool["handler"]
        timeout = tool.get("timeout", TOOL_TIMEOUT_SECONDS)
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(handler(arguments), timeout=timeout)
            return MCPToolResult(ok=True, data=result, duration_ms=round((time.monotonic() - t0) * 1000, 1))
        except asyncio.TimeoutError:
            logger.warning("⏱ Tool %s timed out after %ss", name, timeout)
            return MCPToolResult(
                ok=False,
                error=MCPError(code="TIMEOUT", message=f"Tool '{name}' exceeded {timeout}s timeout"),
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )
        except Exception as exc:
            logger.exception("Tool %s raised: %s", name, exc)
            return MCPToolResult(
                ok=False,
                error=MCPError(code="TOOL_ERROR", message=str(exc)),
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )

    # ── Convenience sync wrapper (for callers that can't await) ───────────

    def call_tool_sync(self, name: str, arguments: Dict[str, Any]) -> dict:
        """Blocking wrapper around ``call_tool`` for legacy sync callers."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        result = loop.run_until_complete(self.call_tool(name, arguments))
        return json.loads(result.model_dump_json())
