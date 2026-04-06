import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger("SubventionTool")

@dataclass
class Subvention:
    category: str  # Engrais, Semence, Materiel
    item: str
    subsidized_price: int
    market_price_est: int
    condition: str
    location: str

class SubventionTool:
    """
    Outil de suivi des subventions agricoles de l'État (Burkina Faso).
    """
    def __init__(self):
        # Base de connaissance simulée des subventions actives (Campagne 2024/2025)
        self._SUBSIDIES = [
            Subvention("Engrais", "NPK 14-23-14", 12000, 22000, "Membre coopérative, Max 10 sacs", "National"),
            Subvention("Engrais", "Urée", 13500, 25000, "Membre coopérative, Max 5 sacs", "National"),
            Subvention("Semence", "Maïs Hybride Bondofa", 1500, 4000, "Producteur enregistré", "Boucle du Mouhoun"),
            Subvention("Materiel", "Charrue à traction asine", 45000, 75000, "Jeune agriculteur (<35 ans)", "Centre-Nord"),
            Subvention("Irrigation", "Kit goutte-à-goutte (500m²)", 25000, 60000, "Femmes maraîchères", "Toutes régions")
        ]

    def check_eligibility(self, user_profile: str) -> str:
        """Vérifie l'éligibilité générale basée sur le profil utilisateur."""
        profile = user_profile.lower()
        if "coopérative" in profile or "gipd" in profile:
            return "ELIGIBLE_MAJOR"
        if "femme" in profile or "jeune" in profile:
            return "ELIGIBLE_PRIORITY"
        return "STANDARD"

    def get_available_subsidies(self, region: str = "National", category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Liste les subventions disponibles pour une région donnée."""
        matches = []
        for sub in self._SUBSIDIES:
            # Filtre géographique : Si la subvention est nationale ou correspond à la région
            geo_match = sub.location == "National" or sub.location.lower() == region.lower()
            
            # Filtre catégorie
            cat_match = True
            if category:
                cat_match = category.lower() in sub.category.lower()

            if geo_match and cat_match:
                saving = sub.market_price_est - sub.subsidized_price
                matches.append({
                    "produit": sub.item,
                    "prix_subventionne": f"{sub.subsidized_price} FCFA",
                    "economie": f"{saving} FCFA (-{int(saving/sub.market_price_est*100)}%)",
                    "condition": sub.condition,
                    "zone": sub.location
                })
        return matches

    def get_procedure(self, subsidy_item: str) -> str:
        """Donne la procédure pour obtenir une subvention spécifique."""
        item = subsidy_item.lower()
        if "engrais" in item:
            return (
                "PROCÉDURE ENGRAIS :\n"
                "1. Enregistrez-vous auprès de la Chambre d'Agriculture régionale.\n"
                "2. Payez la caution à la banque agricole (BAGRI) ou via mobile money.\n"
                "3. Présentez le reçu au magasinier de la Direction Régionale."
            )
        elif "semence" in item:
            return "PROCÉDURE SEMENCES : Contactez directement l'agent INERA ou votre animateur agricole local."
        return "Rapprochez-vous de la Direction Régionale de l'Agriculture."
