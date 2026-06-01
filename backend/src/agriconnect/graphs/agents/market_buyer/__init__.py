"""MarketBuyer — agent ACHETEUR du marché AgriConnect.

Mirror architectural du `market_coach` (PRODUCER). Tous les nœuds vivent
dans `agriconnect.services.market` ; cet agent se contente d'assembler le
graphe en mode `role='BUYER'` via la factory commune.
"""

__all__ = ["graph"]


def __getattr__(name):
    if name == "graph":
        from .graph import get_agent_graph
        return get_agent_graph
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
