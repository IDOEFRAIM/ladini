import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Tuple, List, Any

class SoilType(Enum):
    SABLEUX = "sableux"
    ARGILEUX = "argileux"
    LIMONNEUX = "limonneux"
    FERRUGINEUX = "ferrugineux"
    STANDARD = "standard"

@dataclass(frozen=True)
class CropProfile:
    name: str
    t_base: float
    t_max_optimal: float
    kc: Dict[str, float]
    cycle_days: int
    drought_sensitive: bool

@dataclass
class SahelianCropProfile:
    name: str
    varieties: Dict[str, List[str]]
    cycle_days: int
    seeding_density: str  # Kept for backward compatibility, usage deprecated
    depth_cm: int
    organic_matter_min_tha: float
    mineral_fertilizer: Dict[str, str]
    water_strategy: str
    # V2 Enhancements for "Technical Canvas"
    scientific_name: str = ""
    sowing_config: Dict[str, float] = None # {inter_row_cm: 75, inter_plant_cm: 25, seeds_pocket: 2}
    fertilizer_plan: List[Dict[str, Any]] = None # [{stage: "Semis", type: "NPK", kg_ha: 150}]
    yield_potential: Tuple[float, float] = (0.0, 0.0) # (min, max) t/ha
    key_pests: List[str] = None
    key_diseases: List[str] = None
    pre_flight_checks: List[str] = None # Questions to ask

class SahelAgroMath:
    GSC = 0.0820
    
    @staticmethod
    def calculate_sowing_density_ha(inter_row_cm: float, inter_plant_cm: float, seeds_per_pocket: int = 1) -> int:
        """Calcule la densité de population (plants/ha) en fonction des écartements."""
        if inter_row_cm <= 0 or inter_plant_cm <= 0:
            return 0
        
        # Surface occupée par un poquet en m²
        area_per_pocket_m2 = (inter_row_cm / 100.0) * (inter_plant_cm / 100.0)
        pockets_per_ha = 10000.0 / area_per_pocket_m2
        plants_per_ha = pockets_per_ha * seeds_per_pocket
        
        return int(plants_per_ha)

    @staticmethod
    def calculate_hargreaves_et0(t_min: float, t_max: float, lat: float, doy: int) -> float:
        """Calcule l'évapotranspiration de référence (ET0)."""
        phi = math.radians(lat)
        dr = 1 + 0.033 * math.cos(2 * math.pi * doy / 365.0)
        delta = 0.409 * math.sin(2 * math.pi * doy / 365.0 - 1.39)
        x = -math.tan(phi) * math.tan(delta)
        omega_s = math.acos(max(-1.0, min(1.0, x)))
        ra = (24 * 60 / math.pi) * 0.0820 * dr * (
            omega_s * math.sin(phi) * math.sin(delta) +
            math.cos(phi) * math.cos(delta) * math.sin(omega_s)
        )
        t_mean = (t_max + t_min) / 2
        et0 = 0.0023 * 0.408 * ra * (t_mean + 17.8) * math.sqrt(max(0, t_max - t_min))
        return round(et0, 2)

    @staticmethod
    def calculate_delta_t(temp: float, rh: float) -> Tuple[float, str]:
        """Calcule le Delta T pour l'efficacité de la pulvérisation."""
        if rh < 0 or rh > 100:
             return 0.0, "ERREUR_RH"
             
        tw = (temp * math.atan(0.151977 * math.sqrt(rh + 8.313659)) + 
              math.atan(temp + rh) - math.atan(rh - 1.676331) + 
              0.00391838 * (rh**1.5) * math.atan(0.023101 * rh) - 4.686035)
        delta_t = round(temp - tw, 1)
        
        if 2 <= delta_t <= 8: advice = "OPTIMAL"
        elif delta_t > 10: advice = "DANGER_EVAPORATION"
        else: advice = "RISQUE_LESSIVAGE"
        return delta_t, advice
