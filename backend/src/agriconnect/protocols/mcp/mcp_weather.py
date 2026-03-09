"""Shim exposing ``MCPWeatherServer`` expected by tests / agents.

Delegates to the FastMCP-based WeatherMCPServer compat wrapper.
"""
from agriconnect.protocols.mcp.servers.weather_server import WeatherMCPServer


class MCPWeatherServer:
    def __init__(self, *args, **kwargs):
        self._server = WeatherMCPServer(*args, **kwargs)

    def list_tools(self):
        return self._server.list_tools()

    def call_tool(self, name: str, arguments: dict):
        return self._server.call_tool_sync(name, arguments)

    def __getattr__(self, item):
        return getattr(self._server, item)
