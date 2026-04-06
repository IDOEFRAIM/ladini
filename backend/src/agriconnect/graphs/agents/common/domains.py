"""Domain contract shared by all agents for handoff signaling."""

from enum import Enum


class AgentDomain(str, Enum):
    FORMATION = "formation"
    MARKET = "market_expert"
    SENTINELLE = "climate_sentinel"
    PLANT_DOCTOR = "plant_pathology"
    MARKETPLACE = "marketplace"


__all__ = ["AgentDomain"]
