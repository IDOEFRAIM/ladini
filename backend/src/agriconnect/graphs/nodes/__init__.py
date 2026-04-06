"""
Agents  Agents IA sp�cialis�s AgriConnect.

Chaque agent est un sous-graphe LangGraph autonome,
appel� par l'orchestrateur (MessageResponseFlow / ReportFlow).
"""

from agriconnect.graphs.agents.sentinelle.graph import ClimateSentinel
from agriconnect.graphs.agents.formation.graph import FormationCoach
from .soil import AgriSoilAgent
from .plant_doctor import PlantHealthDoctor
from agriconnect.graphs.agents.market_coach.graph import MarketCoach

# v3  Async agents (multi-schema)
from agriconnect.graphs.agents.marketplace_v3.graph import MarketplaceAgentV3
from .marketplace_background import MarketplaceBackgroundAgent

__all__ = [
    "ClimateSentinel",
    "FormationCoach",
    "AgriSoilAgent",
    "PlantHealthDoctor",
    # v3
    "MarketplaceAgentV3",
    "MarketplaceBackgroundAgent",
    "MarketCoach",
]
