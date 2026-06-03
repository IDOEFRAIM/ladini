import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable

from fastapi import Request
from langgraph.checkpoint.redis import AsyncRedisSaver

logger = logging.getLogger("AgriConnect.Dependencies")
REDIS_URL = os.getenv("REDIS", "redis://localhost:6379/0")

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
async def get_checkpointer() -> AsyncGenerator[AsyncRedisSaver, None]:
    """Gestionnaire Redis basé sur le contexte natif d'AsyncRedisSaver.

    Si l'instance Redis ne supporte pas les commandes RediSearch (FT.*),
    on revient automatiquement sur un MemorySaver en mémoire pour ne pas
    bloquer l'API en développement.
    """

    try:
        async with AsyncRedisSaver.from_conn_string(REDIS_URL) as checkpointer:
            yield checkpointer
            return
    except Exception as exc:  # pragma: no cover - fallback path
        from redis.exceptions import ResponseError

        if isinstance(exc, ResponseError) and "FT." in str(exc):
            logger.warning(
                "Redis ne supporte pas RediSearch (absence de FT).* : fallback MemorySaver (%s)",
                exc,
            )
        else:
            logger.warning("Checkpointer Redis indisponible, fallback MemorySaver (%s)", exc)

        from langgraph.checkpoint.memory import MemorySaver

        memory_saver = MemorySaver()
        yield memory_saver

# --- Fonctions d'accès aux graphes via l'état de l'application ---

async def get_producer_graph(request: Request) -> Any:
    """Récupère le graphe producteur compilé dans app.state."""
    return request.app.state.producer_graph

async def get_buyer_graph(request: Request) -> Any:
    """Récupère le graphe acheteur compilé dans app.state."""
    return request.app.state.buyer_graph