import asyncio
import logging
import sys
from agriconnect.protocols.mcp.client import AgriMCPClient

async def main():
    logging.basicConfig(level=logging.INFO)
    server_script = "C:/Users/LENOVO T14s/Documents/projet/AgriConnect/backend/src/agriconnect/protocols/mcp/test_server_minimal.py"

    async with AgriMCPClient(server_script) as client:
        tools = await client.refresh_tools()
        print("Available tools:")
        for t in tools:
            print(t)

        print('\nCalling echo...')
        res = await client.call_tool("echo", {"message": "hello minimal"})
        print('\nResponse:')
        print(res)

if __name__ == '__main__':
    asyncio.run(main())
