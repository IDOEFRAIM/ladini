import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Tuple, List, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

class SowingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inter_row: float = Field(gt=0)
    inter_plant: float = Field(gt=0)
    seeds_pocket: int = Field(gt=0)

    @field_validator("inter_row", "inter_plant", mode="before")
    @classmethod
    def _validate_spacing_numeric_positive(cls, value: Any) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("Sowing spacing values must be positive")
        return parsed

    @field_validator("seeds_pocket", mode="before")
    @classmethod
    def _validate_seeds_positive(cls, value: Any) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("seeds_pocket must be a positive integer")
        return parsed


class FertilizerStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_order: int = Field(ge=0)
    stage: str = Field(min_length=1)
    type: str = Field(min_length=1)
    dose_kg_ha: float = Field(ge=0)
    mode: str = "Épandage"

    @field_validator("stage", "type", "mode")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class SahelianCropProfile(BaseModel):
    """Strict crop contract for deterministic advisory calculations."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    varieties: Dict[str, List[str]] = Field(default_factory=dict)
    cycle_days: int = Field(gt=0)
    seeding_density: str = "N/A"  # Kept for backward compatibility, usage deprecated
    depth_cm: float = Field(gt=0)
    organic_matter_min_tha: float = Field(ge=0)
    mineral_fertilizer: Dict[str, str] = Field(default_factory=dict)
    water_strategy: str = ""
    scientific_name: str = ""
    sowing_config: SowingConfig
    fertilizer_plan: List[FertilizerStep] = Field(default_factory=list)
    base_temperature_c: float = 10.0
    max_temperature_c: float = 35.0
    expected_gdd: float = 1200.0
    water_needs_mm_per_cycle: float = 400.0
    kc_stages: List[Dict[str, Any]] = Field(default_factory=list)
    critical_stops_drought: List[str] = Field(default_factory=list)
    nutrient_requirements_per_ton: Dict[str, float] = Field(default_factory=dict)
    salinity_tolerance_ec: float = 3.0
    phenological_stages: List[Dict[str, Any]] = Field(default_factory=list)
    yield_potential: Tuple[float, float] = (0.0, 0.0)
    key_pests: List[str] = Field(default_factory=list)
    key_diseases: List[str] = Field(default_factory=list)
    pre_flight_checks: List[str] = Field(default_factory=list)

    @field_validator("cycle_days", mode="before")
    @classmethod
    def _validate_cycle_days_positive(cls, value: Any) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("cycle_days must be a positive integer")
        return parsed

    @field_validator("name", "scientific_name", "water_strategy")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("yield_potential")
    @classmethod
    def _validate_yield_range(cls, value: Tuple[float, float]) -> Tuple[float, float]:
        min_y, max_y = float(value[0]), float(value[1])
        if min_y < 0 or max_y < 0:
            raise ValueError("yield_potential must be non-negative")
        if min_y > max_y:
            raise ValueError("yield_potential min cannot exceed max")
        return (min_y, max_y)

    @field_validator("varieties")
    @classmethod
    def _validate_varieties(cls, value: Dict[str, List[str]]) -> Dict[str, List[str]]:
        cleaned: Dict[str, List[str]] = {}
        for zone, names in value.items():
            zone_key = str(zone).strip()
            if not zone_key:
                continue
            cleaned_names = [str(n).strip() for n in names if str(n).strip()]
            if cleaned_names:
                cleaned[zone_key] = cleaned_names
        return cleaned

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
