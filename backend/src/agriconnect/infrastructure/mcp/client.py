from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

try:
    from fastmcp import Client
    from fastmcp.client.elicitation import ElicitResult
except ImportError:
    Client = None
    ElicitResult = None

from agriconnect.infrastructure.mcp.security import ShieldHub

logger = logging.getLogger(__name__)


class AgriMCPClient:
    """MCP client SDK for local/remote MCP servers via FastMCP client transport."""

    def __init__(self, server_script_path: str):
        self.server_script_path = server_script_path
        self.exit_stack = AsyncExitStack()
        self.client: Optional[Client] = None
        self._tools_cache: List[Dict[str, Any]] = []

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        if not Client:
            raise ImportError("fastmcp is required to use AgriMCPClient. Please install it.")

        logger.info("Connecting to MCP server: %s", self.server_script_path)
        self.client = Client(
            self.server_script_path,
            elicitation_handler=self._handle_elicitation,
            progress_handler=self._handle_progress,
            message_handler=self._handle_message,
        )
        await self.exit_stack.enter_async_context(self.client)
        await self.refresh_tools()

    async def close(self):
        await self.exit_stack.aclose()
        self.client = None

    async def refresh_tools(self) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client not connected.")

        tools_response = await self.client.list_tools()
        self._tools_cache = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                },
            }
            for tool in tools_response
        ]
        return self._tools_cache

    def get_tools_for_langchain(self) -> List[Dict[str, Any]]:
        return self._tools_cache

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self.client:
            raise RuntimeError("Client not connected.")

        result = await self.client.call_tool(tool_name, arguments)
        if hasattr(result, "content"):
            content = result.content
            if isinstance(content, list):
                text_parts = [c.text for c in content if hasattr(c, "text")]
                return "\n".join(text_parts)
            return content
        return result

    async def _handle_elicitation(self, message: str, response_type: type, params, context):
        logger.warning("Server requested elicitation (headless mode): %s", message)
        if ElicitResult:
            return ElicitResult(action="decline")
        return None

    async def _handle_progress(self, progress: float, total: float | None, message: str | None) -> None:
        if total:
            logger.debug("MCP Progress: %s/%s - %s", progress, total, message)
        else:
            logger.debug("MCP Progress: %s - %s", progress, message)

    async def _handle_message(self, message):
        if hasattr(message, "root") and hasattr(message.root, "method"):
            method = message.root.method
            if method == "notifications/tools/list_changed":
                await self.refresh_tools()


class UnifiedMCPClient:
    """Single entrypoint for MCP tool calls with shield enforcement."""

    def __init__(self, session_id: str = "unknown") -> None:
        self._hub = ShieldHub(session_id=session_id)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        return await self._hub.call(tool_name, arguments or {})

    async def list_tools(self) -> dict[str, list[dict]]:
        return await self._hub.list_tools()
