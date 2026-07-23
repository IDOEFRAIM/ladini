"""Example MCP client usage for local testing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agriconnect.infrastructure.mcp.client import AgriMCPClient


async def main() -> None:
    server_script = Path(__file__).resolve().parents[2] / "src" / "agriconnect" / "protocols" / "mcp" / "servers" / "rag_server.py"
    async with AgriMCPClient(str(server_script)) as client:
        result = await client.call_tool(
            "search_agronomy_docs",
            {"query": "type de riz", "level": "debutant", "top_k": 3},
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
