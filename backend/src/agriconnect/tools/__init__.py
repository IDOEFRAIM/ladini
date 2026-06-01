from .health import HealthDoctorTool
from .crop import BurkinaCropTool
from .soil import SoilDoctorTool
from .sentinelle import SentinelleTool
from .shared_math import SahelianCropProfile, CropProfile, SoilType
from .formation import FormationTool
from .formation_advisor import FormationAdvisor
from .market import AgrimarketTool
from .market_agent_tools import MarketAgentTools

# v3  Async tools (multi-schema)
try:
    from .marketplace_v3 import MarketplaceToolV3
except Exception:
    MarketplaceToolV3 = None

try:
    from .intelligence import IntelligenceTool
except Exception:
    IntelligenceTool = None

try:
    from .marketplace_legacy import MarketplaceTool as MarketplaceToolLegacy
except Exception:
    MarketplaceToolLegacy = None

__all__ = [
    "HealthDoctorTool",
    "BurkinaCropTool",
    "SoilDoctorTool",
    "SentinelleTool",
    "SahelianCropProfile",
    "CropProfile",
    "SoilType",
    "FormationTool",
    "FormationAdvisor",
    "AgrimarketTool",
    "MarketAgentTools",
    # v3  Async tools
    "MarketplaceToolV3",
    "IntelligenceTool",
    "MarketplaceToolLegacy"
]
