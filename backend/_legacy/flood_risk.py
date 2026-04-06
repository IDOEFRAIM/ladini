import logging
from datetime import datetime
from typing import Dict, List, Any

logger = logging.getLogger("FloodRiskTool")

class FloodRiskTool:
    """
    Outil d'évaluation des risques d'inondation (FANFAR / AgConnect simulation).
    """
    def __init__(self, data_path: str = "data/floods/fanfar_latest.json"):
        self.data_path = data_path
        self.risk_levels = {1: "Faible", 2: "Modéré", 3: "Élevé", 4: "Critique"}

    def check_flood_risk(self, location: str, lat: float, lon: float) -> Dict[str, Any]:
        """
        Simule une vérification de risque d'inondation basée sur la saison et la localisation.
        Dans un système réel, interrogerait une API ou la base de données FANFAR.
        """
        current_month = datetime.now().month
        is_rainy_season = 6 <= current_month <= 9
        base_risk = "Faible"
        advice = "Aucun risque majeur d'inondation détecté."
        
        # Logique simplifiée de simulation
        if is_rainy_season:
            base_risk = "Modéré"
            advice = "Saison des pluies : surveillez les bas-fonds."
            
        return {
            "location": location,
            "risk_level": base_risk,
            "is_rainy_season": is_rainy_season,
            "alert_message": advice,
            "timestamp": datetime.now().isoformat(),
            "source": "FANFAR / AgConnect"
        }

    def get_prevention_advice(self, risk_level: str) -> List[str]:
        """Retourne des conseils de prévention selon le niveau de risque."""
        advice_map = {
            "Faible": ["Curage des caniveaux.", "Surveillance normale."],
            "Modéré": ["Sécuriser les stocks.", "Créer des saignées d'évacuation."],
            "Élevé": ["ÉVACUATION des zones basses.", "Sacs de sable requis."],
            "Critique": ["DANGER DE MORT.", "Rejoindre les points de rassemblement."]
        }
        return advice_map.get(risk_level, ["Restez vigilant."])
