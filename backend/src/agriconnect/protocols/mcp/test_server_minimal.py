from fastmcp import FastMCP

mcp = FastMCP("AgriConnect Minimal Test Server")

@mcp.tool()
async def echo(message: str) -> str:
    return message

if __name__ == '__main__':
    print("Starting minimal MCP server")
    mcp.run()
