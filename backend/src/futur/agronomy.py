from typing import Dict, Any, List
from pydantic import BaseModel, Field

class CropStageConfig(BaseModel):
    stage_name: str
    gdd_threshold: float  # GDD cumulés nécessaires pour atteindre ce stade
    kc: float             # Coefficient cultural (FAO-56) pour les besoins en eau
    susceptible_diseases: List[str]  # Maladies critiques à ce stade spécifique

class AgronomyEngine:
    
    # Référentiel agronomique d'élite (Exemple basé sur la Tomate de marché ouest-africain)
    # Permet de mapper dynamiquement le cycle de vie de la culture
    TOMATO_STAGES = [
        CropStageConfig(stage_name="INITIAL", gdd_threshold=0.0, kc=0.6, susceptible_diseases=["pythium"]),
        CropStageConfig(stage_name="DEVELOPMENT", gdd_threshold=200.0, kc=0.8, susceptible_diseases=["alternariose"]),
        CropStageConfig(stage_name="MID_SEASON", gdd_threshold=600.0, kc=1.15, susceptible_diseases=["mildiou", "bacteriose"]),
        CropStageConfig(stage_name="LATE_SEASON", gdd_threshold=1100.0, kc=0.8, susceptible_diseases=["oidium", "alternariose"])
    ]

    @classmethod
    def determine_crop_stage(cls, accumulated_gdd: float) -> CropStageConfig:
        """
        Détermine dynamiquement le stade phénologique exact selon les GDD cumulés.
        """
        current_stage = cls.TOMATO_STAGES[0]
        for stage in cls.TOMATO_STAGES:
            if accumulated_gdd >= stage.gdd_threshold:
                current_stage = stage
            else:
                break
        return current_stage

    @staticmethod
    def calculate_gdd(t_max: float, t_min: float, t_base: float = 10.0, t_cutoff: float = 35.0) -> float:
        """
        Calcule les Degrés-Jours Cumulés (GDD) avec double limitation (Base et Cutoff).
        Formule standardisée pour éviter la surestimation lors des pics thermiques sahéliens.
        """
        adjusted_max = min(t_max, t_cutoff)
        adjusted_min = max(t_min, t_base)
        
        # Si la moyenne ajustée descend sous la base, la croissance est nulle
        gdd = ((adjusted_max + adjusted_min) / 2.0) - t_base
        return max(gdd, 0.0)

    @classmethod
    def evaluate_weather_hazards(
        cls, 
        day_data: Any, 
        accumulated_gdd: float, 
        max_wind_allowed: float = 19.0
    ) -> Dict[str, Any]:
        """
        Génère le JSONB d'aide à la décision contextuel pour WhatsApp.
        Intègre désormais le besoin en eau réel de la culture (ETc).
        """
        current_stage = cls.determine_crop_stage(accumulated_gdd)
        
        # 💧 Calcul de l'ETc (Besoins réels de la plante) selon la norme FAO-56
        # et0_evapotranspiration_mm provient de ton collecteur Open-Meteo corrigé
        etc_mm = day_data.et0_evapotranspiration_mm * current_stage.kc
        water_deficit_etc_mm = max(etc_mm - day_data.precipitation_mm, 0.0)

        triggers = {
            "pesticide_treatment_allowed": True,
            "fertilization_allowed": True,
            "irrigation_urgency": "NORMAL",
            "crop_stage_detected": current_stage.stage_name,
            "etc_requirement_mm": round(etc_mm, 2),
            "net_water_deficit_mm": round(water_deficit_etc_mm, 2),
            "reasons": []
        }
        
        # 1. Analyse du Vent (Anti-dérive phytosanitaire)
        if day_data.wind_speed_kmh > max_wind_allowed:
            triggers["pesticide_treatment_allowed"] = False
            triggers["reasons"].append(f"Vent trop fort ({day_data.wind_speed_kmh} km/h). Risque de dérive du produit.")
            
        # 2. Analyse de la Pluie & Lessivage
        if day_data.precipitation_mm > 15.0:
            triggers["fertilization_allowed"] = False
            triggers["reasons"].append(f"Forte pluie ({day_data.precipitation_mm} mm). Risque de lessivage immédiat de l'azote.")
        elif day_data.precipitation_mm > 2.0 and day_data.precipitation_probability > 70:
            triggers["fertilization_allowed"] = False
            triggers["reasons"].append("Averse imminente détectée. Reportez l'apport d'engrais de surface.")

        # 3. Urgence Irrigation basée sur le couplage Évapotranspiration / Humidité du sol
        # Si le déficit hydrique de la plante est fort OU que le sol s'assèche (Volumétrique < 15%)
        if water_deficit_etc_mm > 4.0 or day_data.soil_moisture_3_to_9cm < 0.150:
            triggers["irrigation_urgency"] = "HIGH"
            
        if day_data.temp_max > 38.0:
            triggers["irrigation_urgency"] = "CRITICAL"
            triggers["reasons"].append("Canicule (>38°C). Risque d'avortement des fleurs (coulure) et flétrissement.")

        # 4. Stress de non-nouaison (Nuits tropicales chaudes)
        if day_data.temp_min > 24.0 and current_stage.stage_name == "MID_SEASON":
            triggers["reasons"].append("Nuit lourde (>24°C) en pleine phase de floraison. Risque de mauvaise nouaison des fruits.")

        return triggers

    @classmethod
    def calculate_disease_risks(cls, day_data: Any, accumulated_gdd: float) -> Dict[str, str]:
        """
        Calculateur épidémiologique contextuel. 
        Le niveau de risque est pondéré par la sensibilité de la plante à son stade actuel.
        """
        current_stage = cls.determine_crop_stage(accumulated_gdd)
        risks = {"mildiou": "LOW", "alternariose": "LOW", "oidium": "LOW"}
        
        # --- Modélisation Mildiou ---
        if day_data.humidity_percent > 85.0 and (18.0 <= day_data.temp_mean <= 24.0):
            base_mildiou = "HIGH"
        elif day_data.humidity_percent > 70.0 and (15.0 <= day_data.temp_mean <= 27.0):
            base_mildiou = "MEDIUM"
        else:
            base_mildiou = "LOW"
            
        # --- Modélisation Oïdium ---
        if day_data.humidity_percent < 55.0 and day_data.temp_max > 30.0:
            base_oidium = "HIGH"
        else:
            base_oidium = "LOW"

        # --- Modélisation Alternariose (Alternance humidité/sécheresse sur vieilles feuilles)
        if day_data.humidity_percent > 75.0 and day_data.temp_mean > 25.0:
            base_alternaria = "HIGH"
        else:
            base_alternaria = "LOW"

        # 🧠 APPLICATION DU FILTRE BIOLOGIQUE (Stade de développement)
        # Si la maladie n'est pas une menace à ce stade, on rétrograde le risque pour éviter de spammer le paysan
        risks["mildiou"] = base_mildiou if "mildiou" in current_stage.susceptible_diseases else "LOW"
        risks["oidium"] = base_oidium if "oidium" in current_stage.susceptible_diseases else "LOW"
        risks["alternariose"] = base_alternaria if "alternariose" in current_stage.susceptible_diseases else "LOW"
        
        return risks