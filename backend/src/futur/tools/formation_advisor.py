import logging
from typing import Dict, Any

from .crop import BurkinaCropTool
from .shared_math import SahelAgroMath
from .crop import CropProfileNotFoundError, CropProfileValidationError, CropProfileBackendUnavailableError

logger = logging.getLogger("FormationAdvisor")

class FormationAdvisor:
    def __init__(self):
        self.crop_tool = BurkinaCropTool()

    async def generate_technical_diagnosis(self, crop: str, zone: str, area_ha: float = 1.0) -> Dict[str, Any]:
        logger.info("[FormationAdvisor] start diagnosis crop=%s zone=%s area_ha=%s", crop, zone, area_ha)

        try:
            area_ha = float(area_ha)
        except Exception:
            area_ha = 1.0
        if area_ha <= 0:
            area_ha = 1.0

        try:
            profile = await self.crop_tool._fetch_profile(crop, zone)
        except CropProfileNotFoundError:
            return {
                "status": "UNAVAILABLE",
                "error": True,
                "message": f"Désolé, la culture '{crop}' n'est pas encore calibrée dans notre base.",
            }
        except CropProfileValidationError:
            return {
                "status": "UNAVAILABLE",
                "error": True,
                "message": "Le profil technique récupéré est invalide. Merci de contacter le support.",
            }
        except CropProfileBackendUnavailableError:
            return {
                "status": "UNAVAILABLE",
                "error": True,
                "message": "La base technique n'est pas initialisée sur cet environnement.",
            }
        except Exception:
            return {
                "status": "UNAVAILABLE",
                "error": True,
                "message": "Le service de calibration est temporairement indisponible.",
            }

        required_checks = profile.pre_flight_checks or []

        try:
            sowing_cfg = profile.sowing_config
            density_ha = SahelAgroMath.calculate_sowing_density_ha(
                float(sowing_cfg.inter_row),
                float(sowing_cfg.inter_plant),
                int(sowing_cfg.seeds_pocket),
            )
        except Exception:
            return {
                "status": "UNAVAILABLE",
                "error": True,
                "message": "Impossible de calculer la densité de semis avec les paramètres actuels.",
            }
        
        alerts = []
        crop_key = str(profile.name).strip().lower()
        if crop_key in {"maïs", "mais"} and density_ha < 30000:
            alerts.append(
                f"⚠️ Alerte Densité : {density_ha} plants/ha est trop faible pour du Maïs (Cible > 40 000). "
                f"Vérifiez vos écartements ({sowing_cfg.inter_row}x{sowing_cfg.inter_plant})."
            )

        if float(profile.salinity_tolerance_ec) < 1.5:
            alerts.append(f"⚠️ Culture très sensible à la salinité (Tolérance: {profile.salinity_tolerance_ec} dS/m). Éviter les eaux saumâtres.")

        fert_plan = []
        total_npk = 0
        total_uree = 0
        
        if profile.fertilizer_plan:
            for step in profile.fertilizer_plan:
                try:
                    dose_kg_ha = float(step.dose_kg_ha)
                    dose_total = dose_kg_ha * float(area_ha)
                except Exception:
                    continue

                if "NPK" in step.type:
                    total_npk += dose_total
                elif "Urée" in step.type or "Uree" in step.type:
                    total_uree += dose_total

                fert_plan.append({
                    "moment": step.stage,
                    "produit": step.type,
                    "dose_ha": f"{dose_kg_ha} kg/ha",
                    "dose_user": f"{int(dose_total)} kg pour {area_ha} ha",
                    "mode": step.mode or "Épandage"
                })

        try:
            yield_min, yield_max = profile.yield_potential or (0.0, 0.0)
            potential_tonnage = (float(yield_min) * float(area_ha), float(yield_max) * float(area_ha))
        except Exception:
            yield_min, yield_max = 0.0, 0.0
            potential_tonnage = (0.0, 0.0)

        pests = profile.key_pests or []
        diseases = profile.key_diseases or []
        pheno_stages = profile.phenological_stages or []
        nutrients = profile.nutrient_requirements_per_ton or {}

        canvas = {
            "meta": {
                "culture": profile.name,
                "variete_recommandee": f"{profile.varieties.get(zone, [profile.name])[0]}",
                "zone": zone,
                "cycle": f"{profile.cycle_days} jours",
                "scientific_name": profile.scientific_name,
                "temperatures": f"Base {profile.base_temperature_c}°C / Max {profile.max_temperature_c}°C",
                "besoin_thermique": f"{profile.expected_gdd} GDD"
            },
            "semis": {
                "ecartement": f"{sowing_cfg.inter_row} cm x {sowing_cfg.inter_plant} cm",
                "densite_theorique": f"{density_ha:,} plants/ha".replace(",", " "),
                "profondeur_racinaire": f"{profile.depth_cm} cm",
                "graines_poquet": sowing_cfg.seeds_pocket
            },
            "eau_climat": {
                "besoin_global": f"{profile.water_needs_mm_per_cycle} mm/cycle",
                "strategie": profile.water_strategy,
                "stades_critiques_secheresse": profile.critical_stops_drought or []
            },
            "phenologie": pheno_stages,
            "fertilisation_detailee": fert_plan,
            "exportations_nutritives": {
                "N": nutrients.get("N", 0),
                "P2O5": nutrients.get("P2O5", 0),
                "K2O": nutrients.get("K2O", 0)
            },
            "synthese_intrants": {
                "NPK_total": f"{total_npk} kg ({int(total_npk/50)} sacs de 50kg)",
                "Uree_total": f"{total_uree} kg ({int(total_uree/50)} sacs de 50kg)",
                "Fumure_orga": f"{float(profile.organic_matter_min_tha) * float(area_ha)} tonnes"
            },
            "protection_critique": {
                "ravageurs": pests,
                "maladies": diseases
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
        if canvas.get("error"):
            return f"❌ {canvas['message']}"

        meta = canvas["meta"]
        semis = canvas["semis"]
        eau = canvas["eau_climat"]
        eco = canvas["estimation_economique"]
        prot = canvas["protection_critique"]
        nut = canvas["exportations_nutritives"]
        
        md = f"""
# 📋 FICHE TECHNIQUE INTÉGRALE : {meta['culture'].upper()} 
**Variété** : {meta['variete_recommandee']} (Zone {meta['zone']}) | **Cycle** : {meta['cycle']}
**Nom Scientifique** : *{meta['scientific_name']}*
**Exigences Thermiques** : {meta['temperatures']} | **Besoins Cumulés** : {meta['besoin_thermique']}

---

## 1. 📏 PARAMÈTRES D'IMPLANTATION
| Paramètre | Spécification Cible |
|-----------|---------------------|
| Écartements | **{semis['ecartement']}** |
| Graines / Poquet | **{semis['graines_poquet']}** |
| Densité Spatiale | **{semis['densite_theorique']}** |
| Profondeur Racinaire| **{semis['profondeur_racinaire']}** |

"""
        if canvas['gardes_fous']:
            for alerte in canvas['gardes_fous']:
                md += f"> {alerte}\n"

        md += f"""
---

## 2. 💧 GESTION HYDRIQUE & STRESS
* **Besoins globaux** : {eau['besoin_global']}
* **Stratégie d'irrigation** : {eau['strategie']}
* **Périodes critiques (Tolérance zéro à la sécheresse)** : {', '.join(eau['stades_critiques_secheresse']) if eau['stades_critiques_secheresse'] else 'Non spécifié'}

---

## 3. 🌱 STADES PHÉNOLOGIQUES CLÉS
"""
        if canvas["phenologie"]:
            for stage in canvas["phenologie"]:
                md += f"- **BBCH {stage.get('bbch', '?')}** : {stage.get('name', 'Stade')} (Objectif GDD : {stage.get('gdd_target', '?')})\n"
        else:
            md += "- Données phénologiques non disponibles pour cette culture.\n"

        md += f"""
---

## 4. 💊 NUTRITION & FERTILISATION
**Exportations par tonne récoltée** : N: {nut['N']}kg | P2O5: {nut['P2O5']}kg | K2O: {nut['K2O']}kg

**Plan de Fumure Détaillé :**
"""
        if canvas["fertilisation_detailee"]:
            for step in canvas["fertilisation_detailee"]:
                md += f"- **{step['moment']}** : Apporter **{step['dose_user']}** de {step['produit']} ({step['mode']}).\n"
        else:
            md += "- Aucun plan minéral spécifique pré-enregistré.\n"
            
        md += f"""
**🛒 Synthèse des Achats (Intrants Minéraux et Organiques) :**
* NPK : **{canvas['synthese_intrants']['NPK_total']}**
* Urée : **{canvas['synthese_intrants']['Uree_total']}**
* Matière Organique (Fond de base) : **{canvas['synthese_intrants']['Fumure_orga']}**

---

## 5. 🛡️ VEILLE PHYTOSANITAIRE
**Ravageurs Majeurs à surveiller :**
"""
        for ravageur in prot['ravageurs']:
            md += f"- 🐛 {ravageur}\n"
            
        md += "\n**Maladies Fréquentes :**\n"
        for maladie in prot['maladies']:
            md += f"- 🦠 {maladie}\n"

        md += f"""
---

## 6. 💰 PROJECTIONS ÉCONOMIQUES
* **Rendement Espéré** : {eco['rendement_cible_ha']}
* **Volume Global de Récolte Estimé** : **{eco['recolte_attendue']}**

---

## 🛑 CHECK-LIST AVANT LANCEMENT
"""
        if canvas["diagnostic_questions"]:
            for q in canvas["diagnostic_questions"]:
                md += f"- [ ] {q}\n"
        else:
            md += "- [ ] Sol préparé selon les normes de la culture.\n"
            
        return md