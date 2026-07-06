import logging
from typing import Any

from fastapi import Request

logger = logging.getLogger("AgriConnect.Dependencies")

# --- Fonctions d'accès aux graphes via l'état de l'application ---

async def get_producer_graph(request: Request) -> Any:
    """Récupère le graphe producteur compilé dans app.state."""
    return request.app.state.producer_graph

async def get_buyer_graph(request: Request) -> Any:
    """Récupère le graphe acheteur compilé dans app.state."""
    return request.app.state.buyer_graph