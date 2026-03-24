import logging
from typing import Dict, List, Any, Optional
from .shared_math import SahelianCropProfile, CropProfile

logger = logging.getLogger("BurkinaCropTool")

import logging
from typing import Dict, List, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import select

from .shared_math import SahelianCropProfile, CropProfile
from agriconnect.tools.db_handler import get_db
from agriconnect.services.knowledge_models import CropKnowledge

logger = logging.getLogger("BurkinaCropTool")

class BurkinaCropTool:
    """
    Outil spécialisé contenant la base de connaissance technique
    des cultures au Burkina Faso (INERA).
    VERSION HYBRIDE : Données en BDD (PostgreSQL/JSONB) + Logique en Python.
    """
    def __init__(self):
        # Profils agronomiques pour calculs (Legacy hardcoded for math logic only if needed)
        self.MATH_PROFILES = {
            "maïs": CropProfile("Maïs", 10, 35, {'ini': 0.3, 'mid': 1.2, 'end': 0.6}, 90, True),
            "niébé": CropProfile("Niébé", 12, 36, {'ini': 0.4, 'mid': 1.0, 'end': 0.35}, 70, False),
            "sorgho": CropProfile("Sorgho", 10, 40, {'ini': 0.3, 'mid': 1.1, 'end': 0.55}, 110, False)
        }

    def _get_profile_from_db(self, crop: str, zone: str) -> Optional[SahelianCropProfile]:
        """Récupère le profil technique depuis la base de données."""
        db = get_db()
        if not db:
            logger.warning("DB unavailable for crop profile fetch.")
            return None
            
        session: Session = db.SessionLocal()
        try:
            # Recherche flexible : Crop + Zone
            # On cherche une entrée qui correspond à la culture et à la zone
            stmt = select(CropKnowledge).where(
                CropKnowledge.crop_name.ilike(crop),
                CropKnowledge.zone_category.ilike(zone)
            )
            result = session.execute(stmt).scalars().first()
            
            if not result:
                # Fallback : Recherche par culture uniquement (profil générique ?)
                 stmt = select(CropKnowledge).where(CropKnowledge.crop_name.ilike(crop))
                 result = session.execute(stmt).scalars().first()
            
            if not result:
                return None

            data = result.technical_sheet
            
            # Reconstruction de l'objet SahelianCropProfile depuis le JSON
            # Note: Le JSON en BDD est plat et adapté au Canvas Technique
            # On mappe les champs pour compatibilité
            
            return SahelianCropProfile(
                name=data.get("name", crop),
                varieties={result.zone_category: [result.variety]} if result.variety else {},
                cycle_days=data.get("cycle_days", 90),
                seeding_density="N/A", # Deprecated
                depth_cm=data.get("depth_cm", 5),
                organic_matter_min_tha=data.get("organic_matter_min_tha", 0.0),
                mineral_fertilizer={}, # Deprecated, use fertilizer_plan
                water_strategy=data.get("water_strategy", ""),
                scientific_name=data.get("scientific_name", ""),
                sowing_config=data.get("sowing_config"),
                fertilizer_plan=data.get("fertilizer_plan"),
                yield_potential=tuple(data.get("yield_potential", [0, 0])),
                key_pests=data.get("key_pests", []),
                key_diseases=data.get("key_diseases", []),
                pre_flight_checks=data.get("pre_flight_checks", [])
            )
            
        except Exception as e:
            logger.error(f"Error fetching crop profile: {e}")
            return None
        finally:
            session.close()

    def get_technical_sheet(self, crop: str, zone: str) -> str:
        """Récupère la fiche technique pour une culture donnée (Via DB)."""
        p = self._get_profile_from_db(crop, zone)
        if not p: return f"Culture '{crop}' non répertoriée en base INERA."
        
        # Adaptation de l'affichage legacy si besoin, ou renvoi vers Advisor
        # Pour l'instant on garde le format texte simple
        vars_zone = p.varieties.get(zone.capitalize(), [p.name])
        inera_seed = f"INERA-{crop[:3].upper()}-Hybrid"
        
        return (
            f"📍 **FICHE TECHNIQUE : {p.name.upper()} ({zone.upper()})**\n"
            f"--- \n"
            f"🧬 **Variété :** {', '.join(vars_zone)}\n"
            f"⏱️ **Cycle :** {p.cycle_days} jours\n"
            f"📏 **Semis :** {p.sowing_config.get('inter_row')}cm x {p.sowing_config.get('inter_plant')}cm\n"
            f"💩 **Fumure Orga :** {p.organic_matter_min_tha} t/ha\n"
            f"💧 **Eau :** {p.water_strategy}"
        )

    def calculate_inputs(self, crop: str, surface_ha: float) -> Dict[str, Any]:
        """Calcule les intrants nécessaires (Via DB logic)."""
        # On suppose une zone par défaut ou on fait une moyenne si zone inconnue
        # Pour simplifier, on prend le premier profil trouvé
        p = self._get_profile_from_db(crop, "Centre") 
        if not p: return {}
        
        inputs = {}
        if p.fertilizer_plan:
            for item in p.fertilizer_plan:
                # item: {type: "NPK", dose_kg_ha: 150, ...}
                product_type = item.get("type", "Engrais")
                dose = item.get("dose_kg_ha", 0) * surface_ha
                # On regroupe par famille simplifié si besoin
                key = f"{product_type}_kg"
                inputs[key] = inputs.get(key, 0) + dose
                
        # Conversion primitive sacs (si on reconnait NPK/Urée)
        return inputs
    
    def get_math_profile(self, crop: str) -> Optional[CropProfile]:
        return self.MATH_PROFILES.get(crop.lower())
