"""
MCP Servers — Sous-package "Système Nerveux" AgriConnect 2.0
=============================================================

All servers use the FastMCP decorator pattern (``@mcp.tool()``,
``@mcp.resource()``, ``@mcp.prompt()``).

Serveurs :
  - mcp_db      : Base de données complète (Tools)        → get_user_profile(), create_order()…
  - mcp_rag     : Base de connaissances agronomiques (Tools) → search_agronomy_docs()
  - mcp_weather : Données météo et alertes (Tools)       → get_weather(), get_alerts()
  - mcp_context : Context Optimizer (Resources + Tools)
  - mcp_units   : Conversion d'unités locales (Tools)    → convert_to_kg()

Shim classes (MCPDatabaseServer, MCPRagServer, MCPWeatherServer) provide
backward-compatible ``call_tool()`` for in-process callers.
"""

try:
  from .mcp_db import MCPDatabaseServer
except Exception:
  try:
    # Prefer package-local server shim when running as a module path
    from .servers.agri_db_server import AgriDBMCPServer as MCPDatabaseServer
  except Exception:
    # Fallback to absolute import for installed package layout
    from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer as MCPDatabaseServer

try:
    from .client import AgriMCPClient
except Exception:
    AgriMCPClient = None

try:
  from .mcp_rag import MCPRagServer
except Exception:
  from .servers.agri_rag_server import AgriRAGMCPServer as MCPRagServer

try:
  from .mcp_weather import MCPWeatherServer
except Exception:
  from .servers.weather_server import WeatherMCPServer as MCPWeatherServer

try:
  from .mcp_context import MCPContextServer
except Exception:
  MCPContextServer = None

try:
  from .mcp_units import UnitsMCPServer
except Exception:
  UnitsMCPServer = None

__all__ = [
  "MCPDatabaseServer",
  "MCPRagServer",
  "MCPWeatherServer",
  "MCPContextServer",
  "UnitsMCPServer",
]
# Simple module-level singletons to avoid multiple instantiations across the app
_MCP_SINGLETONS = {
  "db": None,
  "rag": None,
  "weather": None,
  "context": None,
  "units": None,
}

def get_mcp_db_server(session_factory=None):
  global _MCP_SINGLETONS
  if _MCP_SINGLETONS["db"] is None:
    _MCP_SINGLETONS["db"] = MCPDatabaseServer(session_factory)
  return _MCP_SINGLETONS["db"]

def get_mcp_rag_server():
  global _MCP_SINGLETONS
  if _MCP_SINGLETONS["rag"] is None:
    _MCP_SINGLETONS["rag"] = MCPRagServer()
  return _MCP_SINGLETONS["rag"]

def get_mcp_weather_server(llm_client=None):
  global _MCP_SINGLETONS
  if _MCP_SINGLETONS["weather"] is None:
    _MCP_SINGLETONS["weather"] = MCPWeatherServer(llm_client=llm_client)
  return _MCP_SINGLETONS["weather"]

def get_mcp_context_server(session_factory=None, context_optimizer=None, llm_client=None):
  global _MCP_SINGLETONS
  if _MCP_SINGLETONS["context"] is None:
    # Create with context_optimizer when available, else fall back to session_factory
    if context_optimizer:
      _MCP_SINGLETONS["context"] = MCPContextServer(context_optimizer)
    else:
      _MCP_SINGLETONS["context"] = MCPContextServer(session_factory=session_factory, llm_client=llm_client)
  return _MCP_SINGLETONS["context"]

def get_mcp_units_server():
  global _MCP_SINGLETONS
  if _MCP_SINGLETONS["units"] is None:
    _MCP_SINGLETONS["units"] = UnitsMCPServer()
  return _MCP_SINGLETONS["units"]
