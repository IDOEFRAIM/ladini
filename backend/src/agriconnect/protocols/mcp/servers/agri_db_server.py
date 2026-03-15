"""
AgriConnect Database MCP Server (FastMCP).
==========================================
Version épurée : Se concentre uniquement sur le cycle de vie FastMCP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import warnings
import uuid
from typing import Optional

# ── Configuration de l'environnement ──────────────────────────────────────
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TQDM_DISABLE", "True")
warnings.filterwarnings("ignore", message=".*validate_default.*")

logger = logging.getLogger("agriconnect.mcp.db_server")

# ── Instance FastMCP Unique ───────────────────────────────────────────────
# On importe 'mcp' depuis app.py qui est notre registre central.
from agriconnect.protocols.mcp.app import mcp

# ── Imports des Tools ─────────────────────────────────────────────────────
# L'importation suffit à enregistrer les fonctions décorées par @mcp.tool()
try:
    import agriconnect.protocols.mcp.tools.db_tools
except Exception:
    logger.exception("Erreur critique lors du chargement des outils DB")

# ── Infrastructure et Runtime ─────────────────────────────────────────────
from agriconnect.protocols.mcp.infrastructure import runtime

# ── Middleware de Logging (Background) ────────────────────────────────────

def _fire_and_forget_log(user_id: Optional[str], query_json: str, response_json: str):
    """Lance le log de conversation en arrière-plan via le service de base de données."""
    async def _log_task():
        try:
            # Utilise l'instance db du runtime déjà initialisée
            # Ensure user_id is a valid UUID string; generate placeholder
            # UUID when the provided value is not valid.
            try:
                uuid.UUID(str(user_id))
                user_uuid = str(user_id)
            except Exception:
                user_uuid = str(uuid.uuid4())

            await runtime.db.log_conversation(
                user_uuid,
                query_json,
                response_json,
                agent_type="agri_db_full_access",
            )
        except Exception:
            logger.debug("Échec discret du log auto (non-bloquant)")

    asyncio.create_task(_log_task())

# ── Point d'entrée (Main) ─────────────────────────────────────────────────

def start_server():
    """Initialise le Runtime (DB + Connexion) et lance le serveur MCP.

    Uses synchronous lifecycle helpers to avoid nesting event loops; this
    allows `mcp.run()` to start its own event loop or backend transport.
    """
    logger.info("🚀 Démarrage du serveur AgriConnect Database MCP...")
    try:
        runtime.start()
        try:
            mcp.run(transport="sse",host="localhost", port=8003)
        finally:
            runtime.stop()
    except Exception as e:
        logger.error(f"❌ Erreur fatale au démarrage : {e}")
        raise

if __name__ == "__main__":
    try:
        start_server()
    except KeyboardInterrupt:
        logger.info("Serveur arrêté par l'utilisateur.")