"""Unified MCP Server — AgriConnect Production Entry Point.

Fully optimized for FastMCP, natively supporting both Stdio and SSE transports.
All business log capabilities are discovered dynamically.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from typing import Any, Dict

from fastmcp import FastMCP
from agriconnect.protocols.mcp.h import EXPOSED_METHODS, db_service

# ---------------------------------------------------------------------------
# CONFIGURATION DU LOGGING (Strictement redirigé vers STDERR)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,  # OBLIGATOIRE : Évite de polluer stdout utilisé par JSON-RPC
)
logger = logging.getLogger("AgriConnect.MCP.Server")

# ---------------------------------------------------------------------------
# INSTANCE UNIQUE FASTMCP (Lazy Singleton)
# ---------------------------------------------------------------------------
_mcp_instance: FastMCP | None = None


def get_mcp() -> FastMCP:
    """Récupère ou initialise l'instance unique du serveur FastMCP."""
    global _mcp_instance
    if _mcp_instance is None:
        _mcp_instance = _build_mcp_server()
    return _mcp_instance


def _build_mcp_server() -> FastMCP:
    """Construit le serveur et enregistre dynamiquement toutes les capacités."""
    mcp = FastMCP(
        "AgriConnect Unified MCP Server",
        version="2026.1.0",
    )

    # 1. AUTO-REGISTRATION DES OUTILS DE BASE DE DONNÉES
    # FastMCP inspecte nativement les signatures, types et docstrings
    for method_name in EXPOSED_METHODS:
        method = getattr(db_service, method_name, None)
        if not method:
            logger.warning("Méthode déclarée introuvable dans le service: %s", method_name)
            continue

        # Extraction de la première ligne de la docstring pour la clarté de l'IA
        doc = inspect.getdoc(method) or method_name
        clean_desc = doc.split("\n")[0]

        # Enregistrement direct de la méthode du service
        mcp.tool(name=method_name, description=clean_desc)(method)
        logger.info("🚀 Outil MCP enregistré avec succès : '%s'", method_name)

    # 2. ENREGISTREMENT DES AUTRES SERVICES (Agronomie, Météo, Unités)
    _register_additional_modules(mcp)

    # 3. RESSOURCES DE DIAGNOSTIC (HEALTH CHECK)
    @mcp.resource("mcp://health")
    async def mcp_health() -> str:
        """Retourne l'état de santé global du serveur MCP et de ses dépendances."""
        db_ok = db_service is not None
        return json.dumps(
            {
                "status": "ok" if db_ok else "degraded",
                "server": "AgriConnect Unified MCP Server",
                "database_service": "connected" if db_ok else "disconnected",
                "total_tools_loaded": len(EXPOSED_METHODS),
            },
            ensure_ascii=False,
        )

    @mcp.resource("mcp://db/status")
    async def db_status_resource() -> str:
        """Fournit un aperçu de la configuration d'infrastructure de données."""
        from agriconnect.core.database import get_sessionmaker
        sm = get_sessionmaker()
        return json.dumps(
            {
                "sessionmaker_active": sm is not None,
                "monitored_endpoints": len(EXPOSED_METHODS),
            },
            ensure_ascii=False,
        )

    return mcp


def _register_additional_modules(mcp: FastMCP) -> None:
    """Découvre et monte les autres modules métiers sur l'instance FastMCP unique."""
    try:
        from agriconnect.protocols.mcp.tools.agronomy import AgronomyTools
        from agriconnect.protocols.mcp.tools.weather import WeatherProvider
        from agriconnect.protocols.mcp.tools.units import UnitsTools

        # Instanciation des providers secondaires
        additional_providers = [AgronomyTools(), WeatherProvider(), UnitsTools()]

        for provider in additional_providers:
            # Inspection des méthodes publiques de chaque sous-module
            for attr_name in dir(provider):
                if attr_name.startswith("_"):
                    continue
                func = getattr(provider, attr_name)
                if callable(func) and (inspect.iscoroutinefunction(func) or inspect.isfunction(func)):
                    doc = inspect.getdoc(func) or attr_name
                    mcp.tool(name=attr_name, description=doc.split("\n")[0])(func)
                    logger.info("📦 Module externe connecté -> Outil enregistré : '%s'", attr_name)
                    
    except ImportError as e:
        logger.error("❌ Échec du chargement des modules d'outils complémentaires : %s", str(e))


# ---------------------------------------------------------------------------
# APPLICATION RUNNER (AUTO-DETECTION TRANSPORT)
# ---------------------------------------------------------------------------
def run() -> None:
    """Démarre le serveur MCP en configurant dynamiquement le canal de transport."""
    mcp = get_mcp()
    
    # Détection de l'environnement pour le choix intelligent du transport par défaut
    env_name = (os.getenv("APP_ENV") or os.getenv("ENV") or "development").strip().lower()
    default_transport = "sse" if env_name in {"prod", "production", "staging"} else "stdio"
    
    transport = os.getenv("MCP_TRANSPORT", default_transport).lower()
    host = os.getenv("MCP_HOST", "0.0.0.0" if transport == "sse" else None)
    port = os.getenv("MCP_PORT")

    run_kwargs: Dict[str, Any] = {"transport": transport}
    
    if transport == "sse":
        run_kwargs["host"] = host
        run_kwargs["port"] = int(port) if port else 8765
        logger.info("Démarrage du serveur MCP en mode [SSE] sur http://%s:%s", run_kwargs["host"], run_kwargs["port"])
    else:
        logger.info("Démarrage du serveur MCP en mode [STDIO] (Liaison IPC directe)")

    # Exécution native via l'engine FastMCP
    mcp.run(**run_kwargs)


if __name__ == "__main__":
    import inspect
    run()