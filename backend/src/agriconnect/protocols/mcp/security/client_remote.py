"""MCPRemoteClient — HTTP client to call the MCP ShieldBridge endpoints.

Usage:
    client = MCPRemoteClient(base_url="http://localhost:8000", session_id="agent_1")
    await client.call_tool("search_agronomy_docs", {"query":"riz"})
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("MCP.RemoteClient")


class MCPRemoteClient:
    def __init__(self, base_url: str = "http://localhost:8000", session_id: str = "agent") -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def call_tool(self, tool: str, args: Dict[str, Any]) -> Any:
        """Call MCP tool via HTTP bridge. Returns the 'result' field on success."""
        client = await self._get_client()
        url = f"{self.base_url}/api/v1/mcp/call"
        headers = {"X-Session-Id": self.session_id}
        payload = {"tool": tool, "args": args}
        # Simple retry loop
        for attempt in range(3):
            try:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
                if data.get("ok"):
                    return data.get("result")
                raise RuntimeError(f"MCP call error: {data}")
            except Exception as e:
                logger.warning("MCP call attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(0.2 * (attempt + 1))
        raise RuntimeError("MCP call failed after retries")

    async def list_tools(self) -> Dict[str, Any]:
        client = await self._get_client()
        url = f"{self.base_url}/api/v1/mcp/tools"
        r = await client.get(url)
        r.raise_for_status()
        return r.json()
