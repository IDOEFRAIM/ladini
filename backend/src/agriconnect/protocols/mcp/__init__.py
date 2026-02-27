"""
MCP Servers — Sous-package "Système Nerveux" AgriConnect 2.0
=============================================================

Expose les données et services internes comme des ressources/outils MCP standardisés.

Serveurs :
  - mcp_db      : Profils agriculteurs (Resources)       → agri://profile/{user_id}
  - mcp_rag     : Base de connaissances agronomiques (Tools) → search_agronomy_docs()
  - mcp_weather : Données météo et alertes (Tools)       → get_weather(), get_alerts()
  - mcp_context : Context Optimizer en MCP Host (Resources + Tools)

Avantage : Les agents ne font plus d'appels directs SQL/API.
Si on change de base, de fournisseur météo ou de vector DB, les agents ne changent PAS.
"""

from .servers.agri_db_server import AgriDBMCPServer as MCPDatabaseServer
from .servers.agri_rag_server import AgriRAGMCPServer as MCPRagServer
from .servers.weather_server import WeatherMCPServer as MCPWeatherServer
# Context MCP server may be optional / implemented elsewhere. Import safely to avoid
# circular imports during package initialization. If not available, expose None.
try:
    from .servers.context_server import ContextMCPServer as MCPContextServer
except Exception:
    MCPContextServer = None

__all__ = [
  "MCPDatabaseServer",
  "MCPRagServer",
  "MCPWeatherServer",
  "MCPContextServer"
]
