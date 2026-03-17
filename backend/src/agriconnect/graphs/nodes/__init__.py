"""
Agents — Agents IA spécialisés AgriConnect.

Chaque agent est un sous-graphe LangGraph autonome,
appelé par l'orchestrateur (MessageResponseFlow / ReportFlow).
"""

from .sentinelle import ClimateSentinel
from .formation import FormationCoach
from .market import MarketCoach
from .soil import AgriSoilAgent
from .plant_doctor import PlantHealthDoctor

# v3 — Async agents (multi-schema)
from .marketplace_v3 import MarketplaceAgentV3

# Les agents ci-dessous dépendent de modules optionnels (services.google, etc.)
# Ils sont importés à la demande pour ne pas bloquer le startup.

__all__ = [
    "ClimateSentinel",
    "FormationCoach",
    "MarketCoach",
    "AgriSoilAgent",
    "PlantHealthDoctor",
    # v3
    "MarketplaceAgentV3",
]