"""MarketBuyer — wiring du graphe (façade stable des imports).

Reproduit le contrat du `market_coach.graph` mais pointe sur l'adapter
spécialisé pour le rôle ACHETEUR.
"""
from __future__ import annotations

from agriconnect.graphs.agents.market_buyer.adapter import (
    MarketBuyer,
    get_agent_graph,
)

__all__ = ["get_agent_graph", "MarketBuyer"]
