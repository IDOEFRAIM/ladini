"""Async FastMCP micro-servers — one process per domain.

Each server is a standalone asyncio service that speaks the MCP wire
protocol over stdio or HTTP.  Agents communicate with them exclusively
through typed Pydantic payloads; no prose generation happens here.

Quick start (each server can run independently):
    python -m agriconnect.protocols.mcp.servers.weather_server
    python -m agriconnect.protocols.mcp.servers.agri_data_server
    python -m agriconnect.protocols.mcp.servers.market_server
    python -m agriconnect.protocols.mcp.servers.identity_server
"""

from .weather_server import WeatherMCPServer
from .agri_data_server import AgriDataMCPServer
from .market_server import MarketMCPServer
from .identity_server import IdentityMCPServer

__all__ = [
    "WeatherMCPServer",
    "AgriDataMCPServer",
    "MarketMCPServer",
    "IdentityMCPServer",
]
