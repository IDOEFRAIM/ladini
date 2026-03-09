from .health import HealthDoctorTool
from .market import AgrimarketTool
from .crop import BurkinaCropTool
from .meteo import MeteoAdvisorTool
from .flood_risk import FloodRiskTool
from .soil import SoilDoctorTool
from .subvention import SubventionTool
from .sentinelle import SentinelleTool
from .shared_math import SahelianCropProfile, CropProfile, SoilType

# v3 — Async tools (multi-schema)
from .marketplace_v3 import MarketplaceToolV3
from .intelligence import IntelligenceTool

__all__ = [
    # Legacy tools (per-agent)
    "HealthDoctorTool",
    "AgrimarketTool",
    "BurkinaCropTool",
    "MeteoAdvisorTool",
    "FloodRiskTool",
    "SoilDoctorTool",
    "SubventionTool",
    "SentinelleTool",
    "SahelianCropProfile",
    "CropProfile",
    "SoilType",
    # v3 — Async tools
    "MarketplaceToolV3",
    "IntelligenceTool",
]
