"""AgriConnect Market — package modulaire des nœuds et flux LangGraph.

Architecture :
  - shared_core         : helpers, constantes, 8 nœuds universels
  - interpreter_routing : NLU + planificateur (factory role-aware)
  - producer_flow       : Context Resolver et résolveurs Producteur
  - buyer_flow          : Context Resolver et résolveurs Acheteur
  - graph_builder       : factory `build_graph(role)` qui assemble le StateGraph
  - security            : `SecurityService` (modération anti-scam)

Ce package est consommé par l'agent :
  - `graphs/agents/market_coach/` (PRODUCER)
"""

from .security import SecurityService

__all__ = ["build_graph", "SecurityService"]


def __getattr__(name):
    """Lazy import de `build_graph` pour éviter le cycle :
    services.market.__init__ → graph_builder → market_coach.utils → services.market.security
    """
    if name == "build_graph":
        from .graph_builder import build_graph
        return build_graph
    raise AttributeError(f"module 'agriconnect.services.market' has no attribute {name!r}")
