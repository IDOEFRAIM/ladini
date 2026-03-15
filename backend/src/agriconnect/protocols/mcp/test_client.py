import asyncio
import sys
import logging

from agriconnect.protocols.mcp.client import AgriMCPClient


async def main():
    logging.basicConfig(level=logging.INFO)
    # Use the server script path (Python file) so fastmcp infers a PythonStdioTransport
    server_script = "C:/Users/LENOVO T14s/Documents/projet/AgriConnect/backend/src/agriconnect/protocols/mcp/servers/agri_rag_server.py"

    async with AgriMCPClient(server_script) as client:
        tools = await client.refresh_tools()
        print("Available tools:")
        for t in tools:
            print(t)

        # Call a sample tool
        print('\nCalling search_agronomy_docs...')
        res = await client.call_tool("search_agronomy_docs", {"query": "irrigation best practices", "level": "debutant", "top_k": 2})
        print('\nResponse:')
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
