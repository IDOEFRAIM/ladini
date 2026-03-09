"""FastMCP micro-servers — one process per domain.

Each server is a standalone asyncio service using ``fastmcp.FastMCP``
decorators (``@mcp.tool()``, ``@mcp.resource()``).  Agents communicate
via typed Pydantic payloads; no prose generation happens here.

Quick start (each server can run independently):
    python -m agriconnect.protocols.mcp.servers.weather_server
    python -m agriconnect.protocols.mcp.servers.agri_rag_server
    python -m agriconnect.protocols.mcp.servers.agri_data_server
    python -m agriconnect.protocols.mcp.servers.agri_db_server
    python -m agriconnect.protocols.mcp.servers.market_server
    python -m agriconnect.protocols.mcp.servers.identity_server

Backward-compatible compat classes (``WeatherMCPServer``, etc.) are
provided in each module for in-process ``call_tool_sync()`` calls.
"""

from .weather_server import WeatherMCPServer
from .agri_rag_server import AgriRAGMCPServer
from .agri_data_server import AgriDataMCPServer
try:
    from .agri_db_server import AgriDBMCPServer
except Exception:
    from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer
from .market_server import MarketMCPServer
from .identity_server import IdentityMCPServer

__all__ = [
    "WeatherMCPServer",
    "AgriRAGMCPServer",
    "AgriDataMCPServer",
    "AgriDBMCPServer",
    "MarketMCPServer",
    "IdentityMCPServer",
]
