import logging
from typing import Dict, Any, List, Optional, Tuple
from .crop import BurkinaCropTool
from .shared_math import SahelAgroMath

logger = logging.getLogger("FormationAdvisor")

class FormationAdvisor:
    """
    Module expert pour la génération de conseils agronomiques structurés (Canvas Technique).
    Remplace les réponses génériques par des fiches techniques paramétrées (INERA).
    Respecte le pilier : "Passer du texte au paramétrage".
    """
    
    def __init__(self):
        self.crop_tool = BurkinaCropTool()

    def generate_technical_diagnosis(self, crop: str, zone: str, area_ha: float = 1.0) -> Dict[str, Any]:
        """
        Génère un diagnostic complet sous forme de Canvas Technique.
        
        Retourne un dictionnaire structuré contenant :
        - Semence (Variété, Cycle)
        - Semis (Densité, Écartements, Dates)
        - Fertilisation (Plan chiffré kg/ha)
        - Protection (Ravageurs majeurs)
        - Économie (Rendement cible, Coût intrants estimé)
        - Alertes (Gardes-fous)
        """
        # 1. Récupération Profil + Validation "Seed Catalog" (from DB)
        profile = self.crop_tool._get_profile_from_db(crop, zone)
        
        if not profile:
            return {
                "error": True, 
                "message": f"Désolé, la culture '{crop}' n'est pas encore calibrée dans notre base INERA."
            }

        # 2. "Pre-flight Check" Logic (Simulation, normally from user input)
        # Ici on construit les questions à poser si elles manquent
        required_checks = profile.pre_flight_checks or []

        # 3. Calculs Agronomiques (Densité & Intrants)
        sowing_cfg = profile.sowing_config or {"inter_row": 80, "inter_plant": 40, "seeds_pocket": 2}
        density_ha = SahelAgroMath.calculate_sowing_density_ha(
            sowing_cfg["inter_row"], 
            sowing_cfg["inter_plant"], 
            sowing_cfg["seeds_pocket"]
        )
        
        # Garde-fou Agronomique : Densité
        alerts = []
        if crop.lower() == "maïs" and density_ha < 30000:
             alerts.append(f"⚠️ Alerte Densité : {density_ha} plants/ha est trop faible pour du Maïs performant (Cible > 40,000). Vérifiez vos écartements (Actuel: {sowing_cfg['inter_row']}x{sowing_cfg['inter_plant']}).")
        
        # 4. Fertilisation Précise (Pas de "environ")
        fert_plan = []
        total_npk = 0
        total_uree = 0
        
        if profile.fertilizer_plan:
            for step in profile.fertilizer_plan:
                dose_total = step.get("dose_kg_ha", 0) * area_ha
                if "NPK" in step["type"]: total_npk += dose_total
                elif "Urée" in step["type"]: total_uree += dose_total
                
                fert_plan.append({
                    "moment": step["stage"],
                    "produit": step["type"],
                    "dose_ha": f"{step['dose_kg_ha']} kg/ha",
                    "dose_user": f"{int(dose_total)} kg pour {area_ha} ha",
                    "mode": step.get("mode", "Épandage")
                })

        # 5. Rendement & Économie
        yield_min, yield_max = profile.yield_potential or (0,0)
        potential_tonnage = (yield_min * area_ha, yield_max * area_ha)

        # 6. Protection (Spécifique)
        pests = profile.key_pests or ["Ravageurs généraux"]
        
        # Construction du Canvas Technique
        canvas = {
            "meta": {
                "culture": profile.name,
                "variete_recommandee": f"{profile.varieties.get(zone, ['Standard'])[0]} (Zone {zone})",
                "cycle": f"{profile.cycle_days} jours",
                "scientific_name": profile.scientific_name
            },
            "semis": {
                "ecartement": f"{sowing_cfg['inter_row']} cm x {sowing_cfg['inter_plant']} cm",
                "densite_theorique": f"{density_ha:,} plants/ha".replace(",", " "),
                "profondeur": f"{profile.depth_cm} cm"
            },
            "fertilisation_detailee": fert_plan,
            "synthese_intrants": {
                "NPK_total": f"{total_npk} kg ({int(total_npk/50)} sacs de 50kg)",
                "Uree_total": f"{total_uree} kg ({int(total_uree/50)} sacs de 50kg)",
                "Fumure_orga": f"{profile.organic_matter_min_tha * area_ha} tonnes"
            },
            "protection_critique": {
                "ravageur_majeur_1": pests[0] if len(pests) > 0 else "N/A",
                "ravageur_majeur_2": pests[1] if len(pests) > 1 else "N/A",
                "maladies": profile.key_diseases
            },
            "estimation_economique": {
                "rendement_cible_ha": f"{yield_min} à {yield_max} t/ha",
                "recolte_attendue": f"{potential_tonnage[0]:.1f} à {potential_tonnage[1]:.1f} tonnes"
            },
            "gardes_fous": alerts,
            "diagnostic_questions": required_checks
        }
        
        return canvas

    def format_as_markdown(self, canvas: Dict[str, Any]) -> str:
        """Transforme le dictionnaire en réponse lisible pour l'Agent."""
        if canvas.get("error"):
            return f"❌ {canvas['message']}"

        meta = canvas["meta"]
        semis = canvas["semis"]
        eco = canvas["estimation_economique"]
        prot = canvas["protection_critique"]
        
        md = f"""
# 🚜 FICHE TECHNIQUE : {meta['culture'].upper()} 
**Variété recommandée ({meta.get('zone', 'Zone ?')})** : {meta['variete_recommandee']}
**Cycle** : {meta['cycle']} | **Science** : *{meta['scientific_name']}*

## 1. 📏 OPECTIF SEMIS (Précision)
| Paramètre | Valeur Cible |
|-----------|--------------|
| Écartement | **{semis['ecartement']}** |
| Densité | **{semis['densite_theorique']}** |
| Profondeur | {semis['profondeur']} |

> ⚠️ *Note : {canvas['gardes_fous'][0] if canvas['gardes_fous'] else 'Densité conforme aux normes INERA.'}*

## 2. 💊 PLAN DE FERTILISATION (Date à date)
"""
        for step in canvas["fertilisation_detailee"]:
            md += f"- **{step['moment']}** : Apporter **{step['dose_user']}** de {step['produit']} ({step['mode']}).\n"
            
        md += f"""
### 🛒 Liste de courses
- NPK : {canvas['synthese_intrants']['NPK_total']}
- Urée : {canvas['synthese_intrants']['Uree_total']}
- Organique : {canvas['synthese_intrants']['Fumure_orga']}

## 3. 🛡️ PROTECTION CRITIQUE (Vigilance Rouge)
Ne laissez pas ces ravageurs détruire votre investissement :
1. **{prot['ravageur_majeur_1']}**
2. **{prot['ravageur_majeur_2']}**

## 4. 💰 OBJECTIFS
Si le protocole est respecté, visez un rendement de **{eco['rendement_cible_ha']}**.
Soit une récolte totale estimée à : **{eco['recolte_attendue']}**.

---
*Avant de valider ce plan, confirmez-vous les points suivants ?*
"""
        for q in canvas["diagnostic_questions"]:
            md += f"- [ ] {q}\n"
            
        return md
