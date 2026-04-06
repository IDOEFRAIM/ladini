import logging
import math
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from .shared_math import SahelAgroMath, SoilType, CropProfile

logger = logging.getLogger("MeteoAdvisorTool")

class MeteoAdvisorTool:
    """
    Outil d'analyse agrométéorologique (ET0, Delta T, Bilan hydrique).
    Utilise les données partagées.
    """
    def __init__(self):
        # Profils simplifiés pour les calculs météo rapides
        self.crops = {
            "maïs": CropProfile("Maïs", 10, 35, {'ini': 0.3, 'mid': 1.2, 'end': 0.6}, 90, True),
            "mil": CropProfile("Mil", 12, 42, {'ini': 0.3, 'mid': 1.0, 'end': 0.5}, 100, False),
            "niébé": CropProfile("Niébé", 12, 36, {'ini': 0.4, 'mid': 1.0, 'end': 0.35}, 75, False)
        }

    def _compute_confiance(self, distance_km: float, inc_rain: float) -> tuple:
        if distance_km < 10 and inc_rain < 0.2:
            return ("🟢", "Confiance élevée")
        elif distance_km < 20:
            return ("🟠", "Confiance moyenne")
        else:
            return ("🔴", "Confiance faible (Station éloignée)")

    def get_daily_diagnosis(self, crop_key: str, soil: SoilType, 
                            t_min: float, t_max: float, rh: float, 
                            precip: float, doy: int, lat: float,
                            distance_km: float = 0.0) -> dict:
        crop = self.crops.get(crop_key.lower())
        if not crop: return {"error": "Culture non reconnue"}
        
        et0 = SahelAgroMath.calculate_hargreaves_et0(t_min, t_max, lat, doy)
        etc = et0 * crop.kc['mid']
        pe = self._calculate_pe(precip, soil)
        water_balance = round(pe - etc, 2)
        heat_alert = t_max > crop.t_max_optimal
        delta_t, spray_status = SahelAgroMath.calculate_delta_t(t_max, rh)

        inc_rain = 0.0
        needs_ground_check = False
        recos = []

        if distance_km >= 20:
            inc_rain = 0.5
            needs_ground_check = True
            recos.append("Distance importante : Appliquer la procédure de vérification manuelle de l'humidité.")
        elif distance_km >= 10:
            inc_rain = 0.3

        emoji, conf_txt = self._compute_confiance(distance_km, inc_rain)
        
        return {
            "culture": crop.name,
            "etc": etc,
            "bilan": water_balance,
            "conseil_irrigation": "NECESSAIRE" if water_balance < -3 else "SURVEILLANCE",
            "alerte_chaleur": heat_alert,
            "pulverisation": spray_status,
            "delta_t": delta_t,
            "distance": distance_km,
            "recommandations": recos,
            "emoji": emoji,
            "confiance": conf_txt,
            "check_terrain": needs_ground_check,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M")
        }

    def _calculate_pe(self, rain: float, soil: SoilType) -> float:
        coeffs = {SoilType.SABLEUX: 0.5, SoilType.ARGILEUX: 0.7, SoilType.STANDARD: 0.6}
        # Si soil est une string, tenter de convertir, sinon defaut
        if isinstance(soil, str):
            try:
                soil = SoilType(soil)
            except ValueError:
                soil = SoilType.STANDARD
        
        return round(rain * coeffs.get(soil, 0.6), 2) if rain > 5 else 0.0
