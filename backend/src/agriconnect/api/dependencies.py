import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable

from fastapi import Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger("AgriConnect.Dependencies")

# SQLite est maintenant la source de vérité pour la persistance
SQLITE_DB_PATH = "agriconnect_memory.db"

def _resolve_runtime_builder() -> Callable[[], Any]:
    """Import paresseux pour éviter les cycles avec nodes/build_graph."""
    from agriconnect.graphs.agents.market_coach.nodes import build_runtime
    return build_runtime

async def get_mc_runtime() -> Any:
    """Initialise le runtime partagé des agents (lazy import)."""
    builder = _resolve_runtime_builder()
    runtime = builder()
    return runtime

@asynccontextmanager
async def get_checkpointer() -> AsyncGenerator[AsyncSqliteSaver, None]:
    """Gestionnaire SQLite asynchrone pour la persistance du graphe."""
    
    try:
        # AsyncSqliteSaver crée/ouvre automatiquement le fichier .db
        async with AsyncSqliteSaver.from_conn_string(SQLITE_DB_PATH) as checkpointer:
            yield checkpointer
    except Exception as exc:
        logger.error("Erreur critique d'accès au checkpointer SQLite : %s. Fallback MemorySaver.", exc)
        # En dernier recours, on utilise MemorySaver pour éviter un crash total
        yield MemorySaver()

# --- Fonctions d'accès aux graphes via l'état de l'application ---

async def get_producer_graph(request: Request) -> Any:
    """Récupère le graphe producteur compilé dans app.state."""
    return request.app.state.producer_graph

async def get_buyer_graph(request: Request) -> Any:
    """Récupère le graphe acheteur compilé dans app.state."""
    return request.app.state.buyer_graph