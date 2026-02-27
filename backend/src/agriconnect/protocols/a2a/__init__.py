"""
A2A (Agent-to-Agent) — Protocole de communication inter-agents AgriConnect 2.0
================================================================================

PHILOSOPHIE : Les agents ne sont plus "stick" à l'orchestrateur.
Ils peuvent communiquer entre eux (et avec des agents externes) via
un protocole standardisé de discovery, messaging et handshake.

Cas d'usage :
  - Marketplace : matching offre/demande entre agents producteurs et acheteurs
  - Scoring : l'agent de scoring interroge l'agent de profil
  - Partenaires : agents bancaires, transporteurs, coopératives, SONAGESS
"""

from .registry import A2ARegistry, AgentCard, AgentDomain, AgentStatus
from .messaging import (
  A2AMessage, A2AChannel, MessageType, HandshakeStatus,
  MessageBroker, InMemoryBroker, RedisBroker, IdempotencyStore, HandshakeStore,
)
__all__ = [
    "A2ARegistry",
    "AgentCard",
    "AgentDomain",
    "AgentStatus",
    "A2AMessage",
    "A2AChannel",
    "MessageType",
    "HandshakeStatus",

    # v2 additions
    "MessageBroker",
    "InMemoryBroker",
    "RedisBroker",
    "IdempotencyStore",
    "HandshakeStore",
    "A2ADiscovery",
]

def __getattr__(name: str):
    """Lazy-load heavyweight symbols to avoid import-time cycles."""
    if name == "A2ADiscovery":
        from .discovery import A2ADiscovery as _A2ADiscovery

        return _A2ADiscovery
    raise AttributeError(f"module {__name__} has no attribute {name}")
